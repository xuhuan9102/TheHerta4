import json
import re
import struct
from collections import defaultdict
from pathlib import Path

from ...blueprint.export_helper import BlueprintExportHelper
from ...common.apmi_capture import (
    APMICapture, plan_texcoord_overrides, render_layout_captures, skin_group_count, texcoord_layout,
)
from ...common.apmi_skeleton import (
    MATRIX_STRIDE, REPLACED_PARTS_PROPERTY, SKELETON_PROPERTY, build_skeletons, capture_folder, selected_metadata,
)
from ...common.export_section_registry import draw_ib_context
from ...common.global_config import GlobalConfig
from ...common.global_properties import GlobalProterties
from ...common.ini_variable_validator import find_runtime_ini
from ...common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from ...common.m_ini_helper import M_IniHelper
from ...common.object_prefix_helper import ObjectPrefixHelper, coerce_index
from ...common.submesh_metadata import SubmeshMetadataResolver
from ...common.ttl_protocol import build_transparency_draw_lines, require_apmi_ttl_core
from ...utils.obj_utils import ObjUtils
from ...utils.timer_utils import TimerUtils
from .unity import ExportUnity


_SKIN_BINDINGS = ("cs-t0", "cs-t1", "cs-cb0", "cs-u0")
_PS_SLOT = re.compile(r"ps-t(0|[1-9]\d{0,2})", re.ASCII)


class ExportAPMI(ExportUnity):
    def __init__(self, blueprint_model):
        game_types = {}
        validated = set()
        self._skeleton_ids = {}
        self._bone_ids = defaultdict(set)
        self._replaced_parts = defaultdict(set)
        for draw_call in blueprint_model.ordered_draw_obj_data_model_list:
            identity = draw_call.get_workspace_unique_str()
            obj = ObjUtils.get_obj_by_name(draw_call.get_blender_obj_name())
            if obj is None or (identity, obj.name) in validated:
                continue
            if identity not in game_types:
                game_types[identity] = SubmeshMetadataResolver.resolve(identity).d3d11_game_type
            self._bone_ids[identity].update(self._validate_object(obj, game_types[identity]))
            skeleton_id = str(obj.get(SKELETON_PROPERTY, ""))
            if skeleton_id and not GlobalProterties.import_merged_vgmap():
                raise ValueError(f"APMI：{obj.name} 使用融合顶点组。请启用融合导出，或按局部顶点组重新导入。")
            if self._skeleton_ids.setdefault(identity, skeleton_id) != skeleton_id:
                raise ValueError(f"APMI：{identity} 混用了不同骨架的顶点组。")
            replaced = obj.get(REPLACED_PARTS_PROPERTY)
            if replaced is not None:
                sources = json.loads(replaced) if isinstance(replaced, str) else None
                if not skeleton_id or not isinstance(sources, list) or not all(isinstance(key, str) for key in sources):
                    raise ValueError(f"APMI：{obj.name} 的合并来源记录无效。")
                self._replaced_parts[identity].update(sources)
            validated.add((identity, obj.name))
        super().__init__(blueprint_model)

    @staticmethod
    def _validate_object(obj, game_type):
        if "Blend" not in game_type.CategoryStrideDict:
            raise ValueError(f"APMI：{obj.name} 的 GameType 缺少 Blend，请使用含 Blend 的 GameType 重新导入。")
        imported_type = obj.get("3DMigoto:GameTypeName", "")
        if imported_type and imported_type != game_type.GameTypeName:
            raise ValueError(
                f"APMI：{obj.name} 导入时使用 {imported_type}，工作空间当前选用 {game_type.GameTypeName}。"
                "请使用同一数据类型重新导入后再导出。"
            )
        colors = {e.ElementName for e in game_type.D3D11ElementList if e.SemanticName.startswith("COLOR")}
        uv_elements = [e for e in game_type.D3D11ElementList if e.SemanticName.startswith("TEXCOORD")]
        uvs = {e.ElementName + ".xy" for e in uv_elements}
        uvs.update(e.ElementName + ".zw" for e in uv_elements if "A" in e.Format.split("_", 1)[0])
        missing = uvs.difference(obj.data.uv_layers.keys()) | colors.difference(obj.data.color_attributes.keys())
        if missing:
            raise ValueError(f"APMI：{obj.name} 缺少 GameType 要求的属性 {', '.join(sorted(missing))}。")
        used_bones = set()
        for index in {loop.vertex_index for loop in obj.data.loops}:
            weights = [group for group in obj.data.vertices[index].groups if group.weight > 0]
            if not weights:
                raise ValueError(f"APMI：{obj.name} 有绘制顶点缺少有效顶点组权重，请重新导入或转移原始权重后再导出。")
            used_bones.update(group.group for group in weights)
        bone_names = [obj.vertex_groups[index].name for index in used_bones]
        if any(not name.isascii() or not name.isdecimal() for name in bone_names):
            raise ValueError(f"APMI：{obj.name} 有非数字骨骼组参与绑定。")
        return set(map(int, bone_names))

    def export(self):
        output_dir = GlobalConfig.path_generate_mod_folder()
        runtime_ini = find_runtime_ini(output_dir, GlobalConfig.current_game_migoto_folder)
        workspace = GlobalConfig.path_workspace_folder()
        capture = APMICapture(capture_folder(workspace))
        render_captures = render_layout_captures(workspace)
        skeleton_ids_needed = set(self._skeleton_ids.values()) - {""}
        skeletons_by_id = defaultdict(dict)
        if skeleton_ids_needed:
            source_metadata = list(selected_metadata(workspace))
            for key, source in build_skeletons(capture, source_metadata, workspace).items():
                skeletons_by_id[source[0].identity][key] = source
            if skeleton_ids_needed - skeletons_by_id.keys():
                for key, source in build_skeletons(capture, source_metadata, workspace, use_pose_samples=False).items():
                    skeletons_by_id[source[0].identity][key] = source
        core_files = ["APMI_Skinning.ini"]
        if skeletons_by_id:
            core_files.extend(("APMI_MergedSkeleton.ini", "APMI_ImportPalette.hlsl"))
        if not runtime_ini:
            raise ValueError("APMI：找不到加载器 d3dx.ini，请在 SSMT 中校正 APMI 的加载器目录。")
        for name in core_files:
            if not (Path(runtime_ini).parent / "Core" / name).is_file():
                template = Path(__file__).resolve().parents[2] / "resources" / name
                raise ValueError(f"APMI：缺少加载器 Core/{name}，请安装插件附带的 {template}。")
        if any(drawcall.transparency_info for drawcall in self.blueprint_model.ordered_draw_obj_data_model_list):
            require_apmi_ttl_core(runtime_ini)
        models = {model.draw_ib: model for model in self.drawib_model_list}
        plans = []
        merged_jobs = defaultdict(list)
        needed_slots = defaultdict(set)
        replaced_draws = {}
        output_hashes = set()
        render_shaders = set()
        for draw_ib, model in models.items():
            output_hash = model.vertex_limit_hash
            strides = model.d3d11_game_type.CategoryStrideDict
            position_stride = strides.get("Position", 0)
            blend_stride = strides.get("Blend", 0)
            if (not model.d3d11_game_type.GPU_PreSkinning or not output_hash or output_hash in output_hashes
                    or position_stride <= 0 or blend_stride <= 0):
                raise ValueError(f"APMI：{draw_ib} 缺少可独立绑定的 GPU 蒙皮缓冲。")
            output_hashes.add(output_hash)
            cb_params = capture.skin_parameters(output_hash, position_stride, blend_stride)
            passes = []
            first_draw = None
            skeleton_ids = set()
            texcoords = texcoord_layout(model.d3d11_game_type)
            for part in model.submesh_model_list:
                strides = part.d3d11_game_type.CategoryStrideDict
                if strides.get("Position") != position_stride or strides.get("Blend") != blend_stride:
                    raise ValueError(f"APMI：{part.unique_str} 的 Position 或 Blend 与同一蒙皮通道的布局不一致。")
                if texcoord_layout(part.d3d11_game_type) != texcoords:
                    raise ValueError(f"APMI：{draw_ib} 的子网格声明了不同的 vb1 布局。")
                count = coerce_index(part.match_index_count, field="match_index_count", context=part.unique_str)
                start = coerce_index(part.match_first_index, field="match_first_index", context=part.unique_str)
                shaders, previous_position, first = capture.draw_passes(draw_ib, output_hash, count, start, part, texcoords)
                overrides = {}
                if render_captures:
                    primary = dict(capture.native_texcoord_layouts(draw_ib, output_hash, count, start))
                    observed = (
                        layout for render_capture in render_captures
                        for layout in render_capture.native_texcoord_layouts(draw_ib, output_hash, count, start)
                    )
                    overrides = plan_texcoord_overrides(primary, observed)
                first_draw = first if first_draw is None else min(first, first_draw)
                key = part.workspace_unique_str or part.unique_str
                skeleton_ids.add(self._skeleton_ids.get(key, ""))
                render_shaders.update(shaders)
                passes.append((part, count, start, previous_position, self._ps_texture_bindings(model, part), overrides))
            if len(skeleton_ids) != 1:
                raise ValueError(f"APMI：{draw_ib} 的子网格混用了不同骨架的顶点组。")
            skeleton_id = next(iter(skeleton_ids))
            skeleton = None
            if skeleton_id:
                skeletons = skeletons_by_id.get(skeleton_id, {})
                found = skeletons.get(key)
                if not found or found[0].identity != skeleton_id or found[1].output != output_hash:
                    raise ValueError(f"APMI：{key} 的骨骼来源已经变化，请重新导入后再导出。")
                skeleton, palette = found
                for part in model.submesh_model_list:
                    identity = part.workspace_unique_str or part.unique_str
                    if max(self._bone_ids[identity], default=-1) >= skeleton.count:
                        raise ValueError(f"APMI：{identity} 引用了骨骼池之外的顶点组。")
                    needed_slots[skeleton].update(self._bone_ids[identity])
                last_source = max(source.ordinal for source in skeleton.palettes)
                hosts = [source for source in skeleton.palettes
                         if source.shader == palette.shader and source.pose_constants == palette.pose_constants]
                if not any(last_source <= source.ordinal < first_draw for source in hosts):
                    raise ValueError(f"APMI：{draw_ib} 绘制前没有兼容的原生蒙皮调度可使用完整骨骼池。")
                merged_jobs[skeleton].extend((host.output, draw_ib) for host in hosts)
                for part in model.submesh_model_list:
                    identity = part.workspace_unique_str or part.unique_str
                    for source_key in sorted(self._replaced_parts.get(identity, ())):
                        source = skeletons.get(source_key)
                        if not source or source[0] != skeleton or source_key in self._skeleton_ids:
                            raise ValueError(f"APMI：{source_key} 须与宿主 {identity} 共用骨架且由宿主统一导出。")
                        metadata = SubmeshMetadataResolver.resolve(source_key)
                        source_parts = ObjectPrefixHelper.parse_prefix_parts(source_key)
                        source_ib = source_parts["draw_ib"]
                        source_count = coerce_index(source_parts["index_count"], field="index_count", context=source_key)
                        source_start = coerce_index(source_parts["first_index"], field="first_index", context=source_key)
                        source_output = source[1].output
                        source_shaders, _, source_first = capture.draw_passes(
                            source_ib, source_output, source_count, source_start, metadata,
                            texcoord_layout(metadata.d3d11_game_type),
                        )
                        if source_first <= last_source:
                            raise ValueError(f"APMI：{source_key} 在宿主蒙皮完成前绘制，无法安全合并。")
                        draw_key = source_ib, source_count, source_start
                        replacement = draw_ib, source_output, skeleton.identity
                        if replaced_draws.setdefault(draw_key, replacement) != replacement:
                            raise ValueError(f"APMI：{source_key} 被指定给了多个宿主。")
                        render_shaders.update(source_shaders)
            plans.append((model, cb_params, passes, skeleton))

        providers = {}
        palette_layouts = {}
        for skeleton in merged_jobs:
            for palette in skeleton.palettes:
                if providers.setdefault(palette.output, skeleton.identity) != skeleton.identity:
                    raise ValueError(f"APMI：同一骨骼来源 {palette.output} 混用了两套统一编号，请按同一编号重新导入。")
                layouts = {palette.blend_hash: palette.writes}
                required = needed_slots[skeleton].intersection(palette.writes)
                for order in palette.orders:
                    writes = [-1] * palette.count
                    for source_index, native_index in enumerate(order.local_indices):
                        if native_index >= 0:
                            writes[native_index] = palette.writes[source_index]
                    if required.issubset(writes):
                        layouts[order.blend_hash] = tuple(writes)
                palette_layouts[palette.output] = layouts
        filter_indices = {
            blend_hash: 7311000 + index
            for index, blend_hash in enumerate(sorted({key for layouts in palette_layouts.values() for key in layouts}))
        }

        TimerUtils.start_stage("缓冲文件生成")
        output = Path(GlobalConfig.path_generatemod_buffer_folder())
        self.generate_buffer_files(str(output))
        for model, cb_params, _, _ in plans:
            (output / f"{model.draw_ib}-SkinParams.buf").write_bytes(struct.pack("<I", model.vertex_count) + cb_params)
        for skeleton in merged_jobs:
            for palette in skeleton.palettes:
                for blend_hash, writes in palette_layouts[palette.output].items():
                    layout_name = self._palette_layout_name(palette, blend_hash)
                    use_map = self._needs_palette_map(palette, writes)
                    (output / f"APMI-{layout_name}-PaletteRange.buf").write_bytes(
                        struct.pack("<4I", palette.start, palette.count, use_map, 0),
                    )
                    if use_map:
                        (output / f"APMI-{layout_name}-PaletteMap.buf").write_bytes(
                            struct.pack(f"<{palette.count}I", *(slot & 0xffffffff for slot in writes)),
                        )
        TimerUtils.end_stage("缓冲文件生成")

        TimerUtils.start_stage("INI配置生成")
        ini = M_IniBuilder(compact=True)
        M_IniHelper.generate_hash_style_texture_ini(ini, models)
        M_IniHelper.generate_shared_slot_style_texture_ini(ini, models)
        self._integrate_object_swap_ini_hook(ini)
        for shader in sorted(render_shaders):
            self._section(ini, M_SectionType.VertexShaderCheck, f"ShaderOverrideAPMIDraw_{shader}",
                          [f"hash = {shader}", "allow_duplicate_hash = overrule", "checktextureoverride = ib"])
        replacement_hosts = {host for host, _, _ in replaced_draws.values()}
        for model, _, passes, skeleton in plans:
            with draw_ib_context(model.draw_ib):
                self._write_drawib(ini, model, passes, skeleton, track_replacement=model.draw_ib in replacement_hosts)
            M_IniHelper.move_slot_style_textures(draw_ib_model=model)
        for skeleton, jobs in merged_jobs.items():
            self._write_skeleton(ini, skeleton, jobs, palette_layouts, filter_indices)
        for blend_hash, index in filter_indices.items():
            self._section(ini, M_SectionType.TextureOverrideVB, f"TextureOverrideAPMIBlendLayout_{blend_hash}", [
                f"hash = {blend_hash}", "match_priority = -870", f"filter_index = {index}",
            ])
        for (source_ib, count, start), (host, source_output, skeleton_id) in replaced_draws.items():
            self._section(ini, M_SectionType.TextureOverrideIB, f"TextureOverrideAPMIReplaced_{source_ib}_{count}_{start}", [
                f"hash = {source_ib}",
                f"match_first_index = {start}",
                f"match_index_count = {count}",
                f"if $costume_mods && $apmi_{host}_skinned && !$APMISkeleton_{skeleton_id}_ambiguous"
                f" && ($apmi_{host}_drawn || $apmi_{host}_drawn_previous)"
                f" && @vb0 === @ResourceAPMINativeOutput_{source_output}",
                "    handling = skip",
                "endif",
            ])
        M_IniHelper.add_branch_key_sections(ini, self.blueprint_model.keyname_mkey_dict)
        M_IniHelper.add_shapekey_ini_sections(ini, models)
        ini.save_to_file(GlobalConfig.path_generate_mod_ini())
        TimerUtils.end_stage("INI配置生成")

    @staticmethod
    def _section(ini, kind, name, lines):
        section = M_IniSection(kind)
        if lines:
            section.SectionName = name
        else:
            section.append(f"[{name}]")
        section.extend(lines)
        ini.append_section(section)

    @staticmethod
    def _ps_texture_bindings(model, part):
        slots = {}
        if GlobalProterties.forbid_auto_texture_ini():
            return slots
        for mark in model.get_submesh_texture_markup_info_list(part):
            if not M_IniHelper.is_slot_binding_mark_type(mark.mark_type):
                continue
            slot = str(mark.mark_slot or "").strip().lower()
            match = _PS_SLOT.fullmatch(slot)
            if not match or int(match[1]) >= 128:
                raise ValueError(f"APMI：{part.unique_str} 的贴图 {mark.mark_name} 使用了无效的 PS 插槽 {mark.mark_slot!r}。")
            slots[slot] = mark.get_resource_name()
        return slots

    @staticmethod
    def _ttl_draw_lines(model, part, drawcall, offsets, base_vertex):
        info = drawcall.transparency_info
        if not info:
            return None
        resource = ""
        if info["mode"] == "TEXTURE":
            diffuse = {
                mark.get_resource_name() for mark in model.get_submesh_texture_markup_info_list(part)
                if mark.mark_name.casefold() == "diffusemap" and M_IniHelper.is_slot_binding_mark_type(mark.mark_type)
                and mark.mark_filename and not GlobalProterties.forbid_auto_texture_ini()
            }
            if len(diffuse) != 1 or drawcall.match_draw_ib != model.draw_ib:
                raise ValueError(f"APMI：{drawcall.obj_name} 的透明贴图节点需要唯一的 PS 插槽 DiffuseMap。")
            resource = diffuse.pop()
        index_offset = drawcall.index_offset if offsets is None else offsets.get(drawcall.obj_name, drawcall.index_offset)
        return build_transparency_draw_lines(info, resource, drawcall.index_count, index_offset, base_vertex)

    @staticmethod
    def _palette_layout_name(source, blend_hash):
        return source.output if blend_hash == source.blend_hash else f"{source.output}-{blend_hash}"

    @staticmethod
    def _needs_palette_map(source, writes):
        return writes != tuple(range(source.start, source.start + source.count))

    def _write_skeleton(self, ini, skeleton, jobs, palette_layouts, filter_indices):
        prefix = f"APMISkeleton_{skeleton.identity}"
        pool = "Resource" + prefix
        failed = "$" + prefix + "_ambiguous"
        seen = {source.output: "$" + prefix + f"_{source.output}_seen" for source in skeleton.palettes}
        self._section(ini, M_SectionType.Constants, "Constants",
                      [f"global {name} = 0" for name in (failed, *seen.values())])
        self._section(ini, M_SectionType.Present, "Present",
                      [f"post {name} = 0" for name in (failed, *seen.values())])
        self._section(ini, M_SectionType.ResourceBuffer, pool, [
            "type = StructuredBuffer", f"stride = {MATRIX_STRIDE}", f"array = {skeleton.count}",
            "bind_flags = shader_resource unordered_access",
        ])
        saved_cb, saved_uav = pool + "_SavedCB", pool + "_SavedUAV"
        for resource in (saved_cb, saved_uav):
            self._section(ini, M_SectionType.ResourceBuffer, resource, [])
        save = f"CommandList{prefix}_SaveBindings"
        collect = f"CommandList{prefix}_CollectPalette"
        save_lines = [f"{saved_cb} = ref cs-cb0", f"{saved_uav} = ref cs-u0"]
        collect_lines = [r"run = CustomShader\APMI\Skeleton\ImportPalette"]
        saved_map = pool + "_SavedMap"
        if any(self._needs_palette_map(palette, writes) for palette in skeleton.palettes
               for writes in palette_layouts[palette.output].values()):
            self._section(ini, M_SectionType.ResourceBuffer, saved_map, [])
            save_lines.append(f"{saved_map} = ref cs-t3")
            collect_lines.extend((f"cs-t3 = ref {saved_map}", f"{saved_map} = null"))
        save_lines.append(f"cs-u0 = ref {pool}")
        collect_lines.extend([
            f"cs-u0 = ref {saved_uav}", f"cs-cb0 = ref {saved_cb}",
            f"{saved_uav} = null", f"{saved_cb} = null",
        ])
        kind = M_SectionType.MergedSkeleton
        self._section(ini, kind, save, save_lines)
        self._section(ini, kind, collect, collect_lines)

        scheduled_hosts = defaultdict(list)
        for host, draw_ib in jobs:
            scheduled_hosts[host].append(draw_ib)
        skin_groups = {}
        ready = " && ".join(seen.values())
        for targets in dict.fromkeys(tuple(draw_ibs) for draw_ibs in scheduled_hosts.values()):
            command = f"CommandList{prefix}_Skin{len(skin_groups) + 1}"
            skin_groups[targets] = command
            self._section(ini, kind, command, [
                f"if {ready}",
                *(f"  run = CommandListAPMISkin_{draw_ib}" for draw_ib in targets),
                "endif",
            ])
        folder = BlueprintExportHelper.get_current_buffer_folder_name()
        for source in skeleton.palettes:
            output = source.output
            source_seen = seen[output]
            native_output = f"ResourceAPMINativeOutput_{output}"
            palette_input = f"{pool}_{output}_NativePalette"
            blend_input = f"{pool}_{output}_NativeBlend"
            native_bindings = {"cs-u0": native_output, "cs-t2": palette_input, "cs-t1": blend_input}
            for resource in native_bindings.values():
                self._section(ini, M_SectionType.ResourceBuffer, resource, [])
            self._section(ini, M_SectionType.Present, "Present",
                          [f"post {resource} = null" for resource in native_bindings.values()])
            layouts = palette_layouts[output]
            selection = []
            for index, (blend_hash, writes) in enumerate(layouts.items()):
                layout_name = self._palette_layout_name(source, blend_hash)
                params = pool + "_" + layout_name.replace("-", "_") + "_Range"
                self._section(ini, M_SectionType.ResourceBuffer, params, [
                    "type = Buffer", "bind_flags = constant_buffer",
                    f"filename = {folder}/APMI-{layout_name}-PaletteRange.buf",
                ])
                keyword = "if" if index == 0 else "elif"
                selection.append(f"    {keyword} cs-t1 == {filter_indices[blend_hash]}")
                selection.append(f"      cs-cb0 = ref {params}")
                if self._needs_palette_map(source, writes):
                    slot_map = pool + "_" + layout_name.replace("-", "_") + "_Map"
                    self._section(ini, M_SectionType.ResourceBuffer, slot_map, [
                        "type = StructuredBuffer", "stride = 4",
                        f"filename = {folder}/APMI-{layout_name}-PaletteMap.buf",
                    ])
                    selection.append(f"      cs-t3 = ref {slot_map}")
            selection.append("    endif")
            blend_matches = " || ".join(f"cs-t1 == {filter_indices[key]}" for key in layouts)
            same_native_bindings = " && ".join(f"@{slot} === @{resource}" for slot, resource in native_bindings.items())
            commands = [
                f"hash = {output}",
                f"if $costume_mods && cs-t2->stride == {MATRIX_STRIDE} && cs-t2->size == {source.count * MATRIX_STRIDE}"
                f" && ({blend_matches})",
                # The game can update the same palette again before drawing this instance.
                f"  if {source_seen} && !({same_native_bindings})",
                f"    {failed} = 1",
                f"  elif !{failed}",
                *(f"    {resource} = ref {slot}" for slot, resource in native_bindings.items()),
                f"    run = {save}",
                *selection,
                rf"    $\APMI\Skeleton\groups = {skin_group_count(source.count)}",
                f"    run = {collect}",
                f"    {source_seen} = 1",
            ]
            if output in scheduled_hosts:
                commands.append(f"    run = {skin_groups[tuple(scheduled_hosts[output])]}")
            commands.extend(("  endif", "endif"))
            self._section(ini, M_SectionType.TextureOverrideVB, f"TextureOverrideAPMIPalette_{output}", commands)

    def _write_drawib(self, ini, model, passes, skeleton, *, track_replacement):
        draw_ib = model.draw_ib
        folder = BlueprintExportHelper.get_current_buffer_folder_name()
        position = f"Resource{draw_ib}Position"
        prefix = f"ResourceAPMI_{draw_ib}_"
        active = f"$apmi_{draw_ib}_skinned"
        has_previous = f"$apmi_{draw_ib}_has_previous"
        drawn = f"$apmi_{draw_ib}_drawn"
        drawn_previous = f"$apmi_{draw_ib}_drawn_previous"
        needs_previous = any(previous for _, _, _, previous, _, _ in passes)
        constants = [f"global {active} = 0"]
        present = []
        if track_replacement:
            constants.extend((f"global {drawn} = 0", f"global {drawn_previous} = 0"))
            present.extend((f"post {drawn_previous} = {drawn}", f"post {drawn} = 0"))
        if needs_previous:
            constants.append(f"global {has_previous} = 0")
            history_ready = active
            if skeleton:
                history_ready += f" && !$APMISkeleton_{skeleton.identity}_ambiguous"
            present.extend([
                f"if {history_ready}",
                f"  post {prefix}Previous = copy {prefix}Skinned",
                f"  post {has_previous} = 1",
                "else",
                f"  post {has_previous} = 0",
                "endif",
            ])
        self._section(ini, M_SectionType.Constants, "Constants", constants)
        self._section(ini, M_SectionType.Present, "Present", present + [f"post {active} = 0"])
        buffer_type = M_SectionType.ResourceBuffer
        strides = model.d3d11_game_type.CategoryStrideDict
        position_stride = strides["Position"]
        blend_stride = strides["Blend"]
        self._section(ini, buffer_type, position, ["type = StructuredBuffer", f"stride = {position_stride}", f"filename = {folder}/{draw_ib}-Position.buf"])
        # Shape keys write a structured UAV. Unity's skin shaders read the same bytes through a raw SRV.
        self._section(ini, buffer_type, prefix + "SkinInput", [
            "type = ByteAddressBuffer", "misc_flags = buffer_allow_raw_views", "bind_flags = shader_resource",
        ])
        self._section(ini, buffer_type, prefix + "Blend", ["type = ByteAddressBuffer", f"stride = {blend_stride}", f"filename = {folder}/{draw_ib}-Blend.buf"])
        self._section(ini, buffer_type, prefix + "Params", ["type = Buffer", "bind_flags = constant_buffer", f"filename = {folder}/{draw_ib}-SkinParams.buf"])
        self._section(ini, buffer_type, prefix + "Skinned", ["type = ByteAddressBuffer", f"stride = {position_stride}", "bind_flags = vertex_buffer unordered_access", f"array = {model.vertex_count}"])
        if needs_previous:
            self._section(ini, buffer_type, prefix + "Previous", ["type = Buffer", f"stride = {position_stride}", "bind_flags = vertex_buffer"])
        texcoord_stride = strides["Texcoord"]
        self._section(ini, buffer_type, prefix + "Texcoord", ["type = Buffer", f"stride = {texcoord_stride}", f"filename = {folder}/{draw_ib}-Texcoord.buf"])
        ps_slots = sorted({slot for _, _, _, _, textures, _ in passes for slot in textures})
        skin_bindings = dict(zip(_SKIN_BINDINGS, (prefix + "SkinInput", prefix + "Blend", prefix + "Params", prefix + "Skinned")))
        if skeleton:
            skin_bindings = {"cs-t2": f"ResourceAPMISkeleton_{skeleton.identity}", **skin_bindings}
        saved = {slot: prefix + "Saved_" + slot.replace("-", "_") for slot in (*skin_bindings, "ib", "vb0", "vb1", "vb3", *ps_slots)}
        for resource in saved.values():
            self._section(ini, buffer_type, resource, [])

        # The native position input can change between scenes while the skin output remains bound to the draw.
        commands = ["if $costume_mods", f"  {prefix}SkinInput = copy {position}"]
        for slot, resource in skin_bindings.items():
            commands.extend([f"  {saved[slot]} = ref {slot}", f"  {slot} = ref {resource}"])
        commands.append(f"  dispatch = {skin_group_count(model.vertex_count)}, 1, 1")
        for slot in reversed(skin_bindings):
            commands.extend([f"  {slot} = ref {saved[slot]}", f"  {saved[slot]} = null"])
        if needs_previous:
            commands.extend([f"  if !{has_previous}", f"    {prefix}Previous = copy {prefix}Skinned", "  endif"])
        commands.extend([f"  {active} = 1", "endif"])
        if skeleton:
            self._section(ini, M_SectionType.MergedSkeleton, f"CommandListAPMISkin_{draw_ib}", commands)
        else:
            self._section(ini, M_SectionType.TextureOverrideVB, f"TextureOverrideAPMISkin_{draw_ib}",
                          [f"hash = {model.vertex_limit_hash}", *commands])

        for part, count, start, previous_position, textures, overrides in passes:
            ib_resource = model.get_submesh_ib_resource_name(part)
            basename = part.workspace_unique_str or part.unique_str
            self._section(ini, buffer_type, ib_resource, ["type = Buffer", "format = DXGI_FORMAT_R32_UINT", f"filename = {folder}/{basename}-Index.buf"])
            bindings = {"ib": ib_resource, "vb0": prefix + "Skinned", "vb1": prefix + "Texcoord"}
            if previous_position:
                bindings["vb3"] = prefix + "Previous"
            bindings.update(textures)
            stride_matches = " || ".join(f"vb1->stride == {stride}" for stride in (texcoord_stride, *overrides))
            ready = f"{active} && ({stride_matches})"
            if skeleton:
                ready += f" && !$APMISkeleton_{skeleton.identity}_ambiguous && @vb0 === @ResourceAPMINativeOutput_{model.vertex_limit_hash}"
            commands = [f"hash = {draw_ib}", f"match_first_index = {start}", f"match_index_count = {count}", f"if $costume_mods && {ready}", "  handling = skip"]
            for stride, lines in overrides.items():
                commands.append(f"  if vb1->stride == {stride}")
                commands.extend("    " + line for line in lines)
                commands.append("  endif")
            if self.blueprint_model.keyname_mkey_dict:
                commands.append("  $active0 = 1")
            for slot, resource in bindings.items():
                commands.extend([f"  {saved[slot]} = ref {slot}", f"  {slot} = ref {resource}"])
            if not GlobalProterties.forbid_auto_texture_ini():
                for mark in model.get_submesh_texture_markup_info_list(part):
                    if mark.mark_type == "Hash":
                        commands.append(f"  checktextureoverride = {mark.mark_slot}")
            def draw_lines_for_object(drawcall, offsets, base):
                lines = self._ttl_draw_lines(model, part, drawcall, offsets, base)
                if not track_replacement or drawcall.index_count <= 0:
                    return lines
                if lines is None:
                    lines = [drawcall.get_drawindexed_str(offsets, base_vertex=base)]
                return [*lines, f"{drawn} = 1"]

            draw_lines = M_IniHelper.get_drawindexed_str_list(
                part.drawcall_model_list, model.obj_name_draw_offset,
                draw_lines=draw_lines_for_object,
            )
            commands.extend("  " + line for line in draw_lines if line)
            suffix = model.get_submesh_texture_override_suffix(part)
            if textures:
                restore = f"CommandListAPMIRestoreTextures_{suffix}"
                self._section(ini, M_SectionType.CommandList, restore, [
                    line for slot in reversed(textures)
                    for line in (f"{slot} = ref {saved[slot]}", f"{saved[slot]} = null")
                ])
                commands.append(f"  run = {restore}")
            for slot in reversed(bindings):
                if slot not in textures:
                    commands.extend([f"  {slot} = ref {saved[slot]}", f"  {saved[slot]} = null"])
            commands.append("endif")
            self._section(ini, M_SectionType.TextureOverrideIB, f"TextureOverride_{suffix}", commands)
        self.add_resource_texture_sections(ini, model)

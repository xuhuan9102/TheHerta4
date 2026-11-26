import re
import struct
from collections import defaultdict
from pathlib import Path

from ...blueprint.export_helper import BlueprintExportHelper
from ...common.efmi_skeleton import EFMISkeletonMergeHelper
from ...common.export_section_registry import draw_ib_context
from ...common.global_config import GlobalConfig
from ...common.global_properties import GlobalProterties
from ...common.ini_variable_validator import find_runtime_ini
from ...common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from ...common.m_ini_helper import M_IniHelper
from ...common.object_prefix_helper import coerce_index
from ...common.submesh_metadata import SubmeshMetadataResolver
from ...utils.obj_utils import ObjUtils
from ...utils.timer_utils import TimerUtils
from .unity import ExportUnity


_SKIN_GROUP_SIZE = 64
_SKIN_BINDINGS = ("cs-t0", "cs-t1", "cs-cb0", "cs-u0")
_SKIN_DUMP = re.compile(r"(\d+)-(cs-t[01]|cs-cb0|u0)=([0-9a-f]{8})-cs=[0-9a-f]{16}\.buf", re.I)
_DRAW_DUMP = re.compile(r"(\d+)-(ib|vb[013])=([0-9a-f]{8})-vs=([0-9a-f]{16})-.*\.buf", re.I)
_DRAW_CALL = re.compile(r"(\d+) DrawIndexed\(IndexCount:(\d+), StartIndexLocation:(\d+), BaseVertexLocation:(-?\d+)\)")
_DISPATCH_CALL = re.compile(r"(\d+) Dispatch\(ThreadGroupCountX:(\d+), ThreadGroupCountY:(\d+), ThreadGroupCountZ:(\d+)\)")
_VB_ELEMENT = re.compile(
    r"\s*SemanticName: (\w+)\s+SemanticIndex: (\d+)\s+Format: (\w+)"
    r"\s+InputSlot: (\d+)\s+AlignedByteOffset: (\d+)"
)


def _texcoord_layout(game_type):
    layout = {}
    offset = 0
    for element in game_type.D3D11ElementList:
        if element.Category == "Texcoord":
            layout[element.SemanticName, element.SemanticIndex] = offset, element.Format
            offset += element.ByteWidth
    return layout


def _skin_group_count(vertex_count):
    return (vertex_count + _SKIN_GROUP_SIZE - 1) // _SKIN_GROUP_SIZE


def _validate_texcoord_layout(path, part, expected):
    if path is None or not path.is_file():
        raise ValueError(f"APMI：{part.unique_str} 的抓帧缺少 vb1 布局，请重新抓帧并保留 VB 的 TXT 文件。")
    with path.open(encoding="utf-8", errors="replace") as stream:
        header = []
        for line in stream:
            if line.startswith("vertex-data:"):
                break
            header.append(line)
    text = "".join(header)
    captured_stride = re.search(r"^stride: (\d+)", text, re.M)
    stride = part.d3d11_game_type.CategoryStrideDict.get("Texcoord")
    if captured_stride is None or int(captured_stride[1]) != stride:
        actual = captured_stride[1] if captured_stride else "未知"
        raise ValueError(f"APMI：{part.unique_str} 的 GameType 声明 vb1 步长为 {stride}，抓帧记录为 {actual}。")
    captured = {
        (name, int(index)): (int(offset), fmt)
        for name, index, fmt, slot, offset in _VB_ELEMENT.findall(text)
        if slot == "1"
    }
    for (name, index), (offset, fmt) in expected.items():
        actual = captured.get((name, index))
        if actual != (offset, fmt):
            found = f"偏移 {actual[0]} / {actual[1]}" if actual else "缺失"
            raise ValueError(
                f"APMI：{part.unique_str} 的 vb1 {name}{index} 布局不一致。"
                f"GameType 为偏移 {offset} / {fmt}，抓帧为 {found}。请校正数据类型后重新导入。"
            )
    # Multiple shader semantics can alias one physical field in the vertex buffer.
    if set(captured.values()) != set(expected.values()):
        raise ValueError(f"APMI：{part.unique_str} 的 GameType 未覆盖抓帧记录的 vb1 字段。")


class _Capture:
    def __init__(self, folder):
        self.skin = defaultdict(dict)
        self.draw = defaultdict(dict)
        self.texcoord_dumps = {}
        self.calls = {}
        self.dispatches = {}
        for path in Path(folder).glob("*.buf"):
            match = _SKIN_DUMP.fullmatch(path.name)
            if match:
                ordinal, slot, resource_hash = match.groups()
                self.skin[ordinal][slot] = (resource_hash, path)
                continue
            match = _DRAW_DUMP.fullmatch(path.name)
            if match:
                ordinal, slot, resource_hash, shader = match.groups()
                self.draw[ordinal, shader][slot] = resource_hash
                if slot == "vb1":
                    self.texcoord_dumps[ordinal, shader] = path.with_suffix(".txt")
        with (Path(folder) / "log.txt").open(encoding="utf-8", errors="replace") as log:
            for line in log:
                match = _DRAW_CALL.match(line)
                if match:
                    ordinal, count, start, base = match.groups()
                    self.calls[ordinal] = int(count), int(start), int(base)
                match = _DISPATCH_CALL.match(line)
                if match:
                    ordinal, x, y, z = match.groups()
                    self.dispatches[ordinal] = int(x), int(y), int(z)

    def skin_parameters(self, output_hash, position_stride, blend_stride):
        params = set()
        for ordinal, bindings in self.skin.items():
            if len(bindings) != 4 or bindings["u0"][0] != output_hash:
                continue
            position_size = bindings["cs-t0"][1].stat().st_size
            count, remainder = divmod(position_size, position_stride)
            cb = bindings["cs-cb0"][1].read_bytes()
            blend = bindings["cs-t1"][1]
            if (count and not remainder and len(cb) == 16 and struct.unpack_from("<I", cb)[0] == count
                    and self.dispatches.get(ordinal) == (_skin_group_count(count), 1, 1)
                    and blend.stat().st_size == count * blend_stride and bindings["u0"][1].stat().st_size == position_size):
                params.add(cb[4:])
        if not params:
            raise ValueError(
                f"APMI：抓帧中没有匹配输出 {output_hash} 的蒙皮通道。"
                f"请核对 GameType 的 Position 步长 {position_stride} 和 Blend 步长 {blend_stride}。"
            )
        if len(params) != 1:
            raise ValueError(f"APMI：蒙皮输出 {output_hash} 在抓帧中对应多组蒙皮参数。")
        return params.pop()

    def draw_passes(self, draw_ib, output_hash, count, start, part, texcoord_layout):
        shaders = set()
        previous_position = False
        for (ordinal, shader), bindings in self.draw.items():
            if (bindings.get("ib") != draw_ib or bindings.get("vb0") != output_hash
                    or self.calls.get(ordinal) != (count, start, 0)):
                continue
            if "vb3" in bindings and bindings["vb3"] != output_hash:
                raise ValueError(f"APMI：{draw_ib} 的 vb3 与蒙皮输出不同，当前无法恢复该渲染输入。")
            _validate_texcoord_layout(self.texcoord_dumps.get((ordinal, shader)), part, texcoord_layout)
            previous_position |= "vb3" in bindings
            shaders.add(shader)
        if not shaders:
            raise ValueError(f"APMI：抓帧中没有匹配 {draw_ib}（索引数 {count}，起始索引 {start}）的 DrawIndexed。")
        return shaders, previous_position


class ExportAPMI(ExportUnity):
    def __init__(self, blueprint_model):
        game_types = {}
        validated = set()
        for draw_call in blueprint_model.ordered_draw_obj_data_model_list:
            identity = draw_call.get_workspace_unique_str()
            obj = ObjUtils.get_obj_by_name(draw_call.get_blender_obj_name())
            if obj is None or (identity, obj.name) in validated:
                continue
            if identity not in game_types:
                game_types[identity] = SubmeshMetadataResolver.resolve(identity).d3d11_game_type
            self._validate_object(obj, game_types[identity])
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
        weighted_vertices = {
            vertex.index for vertex in obj.data.vertices
            if any(group.weight > 0 for group in vertex.groups)
        }
        if any(loop.vertex_index not in weighted_vertices for loop in obj.data.loops):
            raise ValueError(f"APMI：{obj.name} 有绘制顶点缺少有效顶点组权重，请重新导入或转移原始权重后再导出。")

    def export(self):
        output_dir = GlobalConfig.path_generate_mod_folder()
        runtime_ini = find_runtime_ini(output_dir, GlobalConfig.current_game_migoto_folder)
        skin_core = Path(runtime_ini).parent / "Core" / "APMI_Skinning.ini"
        if not runtime_ini or not skin_core.is_file():
            template = Path(__file__).resolve().parents[2] / "resources" / skin_core.name
            raise ValueError(f"APMI：缺少共享蒙皮配置 {skin_core}。请安装随插件提供的 {template} 后重新导出。")
        capture_dir = EFMISkeletonMergeHelper.resolve_frame_analysis_dir(GlobalConfig.path_workspace_folder())
        if not capture_dir or not (Path(capture_dir) / "log.txt").is_file():
            raise ValueError("APMI：工作空间缺少可读取的 FrameAnalysis。请关联提取时的抓帧目录后重新导出。")
        capture = _Capture(capture_dir)
        models = {model.draw_ib: model for model in self.drawib_model_list}
        plans = []
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
            texcoords = _texcoord_layout(model.d3d11_game_type)
            for part in model.submesh_model_list:
                strides = part.d3d11_game_type.CategoryStrideDict
                if strides.get("Position") != position_stride or strides.get("Blend") != blend_stride:
                    raise ValueError(f"APMI：{part.unique_str} 的 Position 或 Blend 与同一蒙皮通道的布局不一致。")
                if _texcoord_layout(part.d3d11_game_type) != texcoords:
                    raise ValueError(f"APMI：{draw_ib} 的子网格声明了不同的 vb1 布局。")
                count = coerce_index(part.match_index_count, field="match_index_count", context=part.unique_str)
                start = coerce_index(part.match_first_index, field="match_first_index", context=part.unique_str)
                shaders, previous_position = capture.draw_passes(draw_ib, output_hash, count, start, part, texcoords)
                render_shaders.update(shaders)
                passes.append((part, count, start, previous_position))
            plans.append((model, cb_params, passes))

        TimerUtils.start_stage("缓冲文件生成")
        output = Path(GlobalConfig.path_generatemod_buffer_folder())
        self.generate_buffer_files(str(output))
        for model, cb_params, _ in plans:
            (output / f"{model.draw_ib}-SkinParams.buf").write_bytes(struct.pack("<I", model.vertex_count) + cb_params)
        TimerUtils.end_stage("缓冲文件生成")

        TimerUtils.start_stage("INI配置生成")
        ini = M_IniBuilder()
        M_IniHelper.generate_hash_style_texture_ini(ini, models)
        M_IniHelper.generate_shared_slot_style_texture_ini(ini, models)
        self._integrate_object_swap_ini_hook(ini)
        for shader in sorted(render_shaders):
            self._section(ini, M_SectionType.VertexShaderCheck, f"ShaderOverrideAPMIDraw_{shader}",
                          [f"hash = {shader}", "allow_duplicate_hash = overrule", "checktextureoverride = ib"])
        for model, _, passes in plans:
            with draw_ib_context(model.draw_ib):
                self._write_drawib(ini, model, passes)
            M_IniHelper.move_slot_style_textures(draw_ib_model=model)
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
        section.new_line()
        ini.append_section(section)

    def _write_drawib(self, ini, model, passes):
        draw_ib = model.draw_ib
        folder = BlueprintExportHelper.get_current_buffer_folder_name()
        position = f"Resource{draw_ib}Position"
        prefix = f"ResourceAPMI_{draw_ib}_"
        active = f"$apmi_{draw_ib}_skinned"
        has_previous = f"$apmi_{draw_ib}_has_previous"
        needs_previous = any(previous for _, _, _, previous in passes)
        constants = [f"global {active} = 0"]
        present = []
        if needs_previous:
            constants.append(f"global {has_previous} = 0")
            present.extend([
                f"if {active}",
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
        saved = {slot: prefix + "Saved_" + slot.replace("-", "_") for slot in (*_SKIN_BINDINGS, "ib", "vb0", "vb1", "vb3")}
        for resource in saved.values():
            self._section(ini, buffer_type, resource, [])

        # The native position input can change between scenes while the skin output remains bound to the draw.
        commands = [f"hash = {model.vertex_limit_hash}", "if $costume_mods", f"  {prefix}SkinInput = copy {position}"]
        for slot, resource in zip(_SKIN_BINDINGS, (prefix + "SkinInput", prefix + "Blend", prefix + "Params", prefix + "Skinned")):
            commands.extend([f"  {saved[slot]} = ref {slot}", f"  {slot} = ref {resource}"])
        commands.append(f"  dispatch = {_skin_group_count(model.vertex_count)}, 1, 1")
        for slot in reversed(_SKIN_BINDINGS):
            commands.extend([f"  {slot} = ref {saved[slot]}", f"  {saved[slot]} = null"])
        if needs_previous:
            commands.extend([f"  if !{has_previous}", f"    {prefix}Previous = copy {prefix}Skinned", "  endif"])
        commands.extend([f"  {active} = 1", "endif"])
        self._section(ini, M_SectionType.TextureOverrideVB, f"TextureOverrideAPMISkin_{draw_ib}", commands)

        for part, count, start, previous_position in passes:
            ib_resource = model.get_submesh_ib_resource_name(part)
            basename = part.workspace_unique_str or part.unique_str
            self._section(ini, buffer_type, ib_resource, ["type = Buffer", "format = DXGI_FORMAT_R32_UINT", f"filename = {folder}/{basename}-Index.buf"])
            bindings = {"ib": ib_resource, "vb0": prefix + "Skinned", "vb1": prefix + "Texcoord"}
            if previous_position:
                bindings["vb3"] = prefix + "Previous"
            commands = [f"hash = {draw_ib}", f"match_first_index = {start}", f"match_index_count = {count}", f"if $costume_mods && {active} && vb1->stride == {texcoord_stride}", "  handling = skip"]
            if self.blueprint_model.keyname_mkey_dict:
                commands.append("  $active0 = 1")
            for slot, resource in bindings.items():
                commands.extend([f"  {saved[slot]} = ref {slot}", f"  {slot} = ref {resource}"])
            if not GlobalProterties.forbid_auto_texture_ini():
                for mark in model.get_submesh_texture_markup_info_list(part):
                    if M_IniHelper.is_slot_binding_mark_type(mark.mark_type):
                        commands.append(f"  {mark.mark_slot} = {mark.get_resource_name()}")
                    elif mark.mark_type == "Hash":
                        commands.append(f"  checktextureoverride = {mark.mark_slot}")
            commands.extend("  " + line for line in M_IniHelper.get_drawindexed_str_list(part.drawcall_model_list, model.obj_name_draw_offset) if line)
            for slot in reversed(bindings):
                commands.extend([f"  {slot} = ref {saved[slot]}", f"  {saved[slot]} = null"])
            commands.append("endif")
            suffix = model.get_submesh_texture_override_suffix(part)
            self._section(ini, M_SectionType.TextureOverrideIB, f"TextureOverride_{suffix}", commands)
        self.add_resource_texture_sections(ini, model)

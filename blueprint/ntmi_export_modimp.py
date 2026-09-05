from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil

import bpy

from .export_helper import BlueprintExportHelper
from .model import BluePrintModel
from .preprocess import PreProcessHelper
from .variable_registry import ensure_object_swap_variable_name, get_node_variable_name
from ..common.global_config import GlobalConfig
from ..common.global_key_count_helper import GlobalKeyCountHelper
from ..common.global_properties import GlobalProterties
from ..common.object_prefix_helper import ObjectPrefixHelper
from ..common.workspace_helper import WorkSpaceHelper
from ..utils.log_utils import LOG
from ..utils.timer_utils import TimerUtils


def _strip_material_texture_nodes(objects: list[bpy.types.Object]) -> list[dict]:
    saved = []
    seen_meshes = set()

    for obj in objects:
        if obj is None or obj.type != "MESH" or not getattr(obj, "data", None):
            continue
        mesh = obj.data
        mesh_key = mesh.name_full if getattr(mesh, "name_full", "") else str(id(mesh))
        if mesh_key in seen_meshes:
            continue
        seen_meshes.add(mesh_key)
        materials = list(mesh.materials)
        if not materials:
            continue
        saved.append(
            {
                "mesh": mesh,
                "materials": materials,
                "objects": [
                    {
                        "object": candidate,
                        "active_material_index": int(getattr(candidate, "active_material_index", 0) or 0),
                    }
                    for candidate in objects
                    if candidate is not None and getattr(candidate, "data", None) == mesh
                ],
            }
        )
        mesh.materials.clear()

    return saved


def _restore_material_texture_nodes(saved: list[dict]):
    for payload in saved:
        mesh = payload.get("mesh")
        if mesh is None:
            continue
        try:
            mesh.materials.clear()
            for material in payload.get("materials", []) or []:
                mesh.materials.append(material)
        except ReferenceError:
            continue
        except Exception as exc:
            LOG.warning(f"NTMI ModImp: failed to restore material slots: {exc}")

        for object_payload in payload.get("objects", []) or []:
            obj = object_payload.get("object")
            if obj is None:
                continue
            try:
                slot_count = len(getattr(obj, "material_slots", []) or [])
                if slot_count <= 0:
                    continue
                obj.active_material_index = max(
                    0,
                    min(int(object_payload.get("active_material_index", 0) or 0), slot_count - 1),
                )
            except ReferenceError:
                continue
            except Exception:
                continue
from ..ui.ntmi_modimp.export_tree_builder import (
    ExportTreeBuildResult,
    collect_object_conditions,
    build_export_tree,
    cleanup_collections,
    condition_from_swap_work_keys,
)
from ..ui.ntmi_modimp.ini_swap_patcher import ACTIVE_FLAG, patch_ini_file
from ..ui.ntmi_modimp.modimp_core import (
    detect_mod_importer_dependency,
    get_export_collection_package,
    resolve_mod_importer_root,
)
from ..ui.ntmi_modimp.texture_slot_refresh import refresh_texture_slots_for_objects
from .ntmi_multifile import execute_ntmi_multifile_postprocess
from .ntmi_shapekey import execute_ntmi_shapekey_postprocess


RESULT_NODE_TYPE = "SSMTNode_Result_Output_NTMIModImp"
MODIMP_MIRROR_FLIP_PROP = "modimp_mirror_flip"
COMPATIBLE_POSTPROCESS_NODE_TYPES = {
    "SSMTNode_PostProcess_AnimDriver",
    "SSMTNode_PostProcess_BufferCleanup",
    "SSMTNode_PostProcess_CommentCleanup",
    "SSMTNode_PostProcess_Material",
    "SSMTNode_PostProcess_CustomMaterialAssign",
    "SSMTNode_PostProcess_MultiFile",
    "SSMTNode_PostProcess_ResourceMerge",
    "SSMTNode_PostProcess_ShapeKey",
    "SSMTNode_PostProcess_SliderPanel",
}
NTMI_INTERNAL_POSTPROCESS_NODE_TYPES = {
}


class NTMIModImpExportError(RuntimeError):
    pass


def _resolve_default_ntmi_modimp_output_dir() -> str:
    try:
        GlobalConfig.read_from_main_json_ssmt4()
        default_output_dir = str(GlobalConfig.path_generate_mod_folder() or "").strip()
        if default_output_dir:
            return os.path.normpath(default_output_dir)
    except Exception as exc:
        LOG.warning(f"NTMI ModImp: failed to resolve SSMT export directory, fall back to legacy path: {exc}")

    blend_path = str(getattr(bpy.data, "filepath", "") or "").strip()
    if blend_path:
        return os.path.normpath(str(Path(blend_path).resolve().parent / "NTMI_ModImp_Output"))

    return os.path.normpath(str(Path.home() / "TheHerta4_NTMI_ModImp_Output"))


def resolve_ntmi_modimp_output_dir(node) -> str:
    use_custom_dir = bool(getattr(node, "use_custom_export_dir", False))
    configured = str(getattr(node, "export_dir", "") or "").strip()
    if use_custom_dir:
        if configured:
            return os.path.normpath(bpy.path.abspath(configured))
        LOG.warning(
            "NTMI ModImp: manual export directory is enabled but empty; "
            "fall back to the current SSMT export directory."
        )

    return _resolve_default_ntmi_modimp_output_dir()


def _reset_output_dir(path: str):
    output_path = Path(path).resolve()
    anchor_path = Path(output_path.anchor).resolve()
    home_path = Path.home().resolve()
    if output_path in {anchor_path, home_path}:
        raise NTMIModImpExportError(f"Refuse to reset unsafe output directory: {output_path}")

    if output_path.is_file():
        raise NTMIModImpExportError(f"Output path is a file, not a directory: {output_path}")

    output_path.mkdir(parents=True, exist_ok=True)

    buffer_dir = output_path / "Buffer"
    if buffer_dir.is_file():
        raise NTMIModImpExportError(f"Buffer path is a file, not a directory: {buffer_dir}")
    if buffer_dir.is_dir():
        shutil.rmtree(buffer_dir, ignore_errors=True)
    buffer_dir.mkdir(parents=True, exist_ok=True)

    for ini_path in output_path.glob("*.ini"):
        if ini_path.is_file():
            ini_path.unlink(missing_ok=True)

    report_path = output_path / "theherta4_ntmi_modimp_export_report.json"
    if report_path.is_file():
        report_path.unlink(missing_ok=True)


def _collect_nested_trees(tree, visited=None):
    if visited is None:
        visited = set()
    nested = []
    if not tree or tree.name in visited:
        return nested
    visited.add(tree.name)
    for node in tree.nodes:
        if getattr(node, "mute", False):
            continue
        if getattr(node, "bl_idname", "") != "SSMTNode_Blueprint_Nest":
            continue
        blueprint_name = str(getattr(node, "blueprint_name", "") or "")
        if not blueprint_name or blueprint_name == "NONE":
            continue
        nested_tree = bpy.data.node_groups.get(blueprint_name)
        if nested_tree and getattr(nested_tree, "bl_idname", "") == "SSMTBlueprintTreeType":
            nested.append(nested_tree)
            nested.extend(_collect_nested_trees(nested_tree, visited))
    return nested


def _object_conditions_from_blueprint_model(source) -> dict[str, str]:
    if isinstance(source, ExportTreeBuildResult):
        return collect_object_conditions(source)
    return collect_object_conditions(build_export_tree(source))


def _wrap_condition(condition: str) -> str:
    condition = str(condition or "").strip()
    if not condition:
        return ""
    return condition


def _merge_conditions(existing: str, incoming: str) -> str:
    existing = str(existing or "").strip()
    incoming = str(incoming or "").strip()
    if not existing:
        return incoming
    if not incoming or incoming == existing:
        return existing
    return f"{existing} && {incoming}"


def _condition_from_chain(chain) -> str:
    conditions = []
    swap_condition = condition_from_swap_work_keys(getattr(chain, "shapekey_params", []) or [])
    if swap_condition:
        conditions.append(swap_condition)
    multifile_condition = str(getattr(chain, "ntmi_multifile_condition", "") or "").strip()
    if multifile_condition:
        conditions.append(multifile_condition)
    return " && ".join(condition for condition in conditions if condition)


def _normalize_ini_variable(value: str, fallback: str) -> str:
    variable = str(value or "").strip() or fallback
    if not variable.startswith("$"):
        variable = f"${variable}"
    return variable


def _parse_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _draw_ib_from_object_name(object_name: str) -> str:
    name = str(object_name or "").strip()
    prefix_info = ObjectPrefixHelper.extract_prefix_info(name)
    prefix = prefix_info[0] if prefix_info else name
    return str(ObjectPrefixHelper.parse_prefix_parts(prefix).get("draw_ib", "") or "").strip().lower()


def _hashes_from_multifile_export_node(node) -> set[str]:
    hashes = set()
    for item in getattr(node, "object_list", []) or []:
        draw_ib = _draw_ib_from_object_name(getattr(item, "object_name", ""))
        if draw_ib:
            hashes.add(draw_ib)
    return hashes


def _hashes_from_multifile_config_node(node) -> set[str]:
    hashes = set()
    raw_values = re.split(r"[,;\n]+", str(getattr(node, "hash_values", "") or ""))
    for raw_value in raw_values:
        value = raw_value.strip()
        if not value:
            continue
        draw_ib = _draw_ib_from_object_name(value)
        if draw_ib:
            hashes.add(draw_ib)
    return hashes


def _multifile_config_for_export_node(postprocess_nodes, export_node):
    config_nodes = [
        node
        for node in postprocess_nodes or []
        if str(getattr(node, "bl_idname", "") or "") == "SSMTNode_PostProcess_MultiFile"
    ]
    if not config_nodes:
        return None

    export_hashes = _hashes_from_multifile_export_node(export_node)
    for config_node in config_nodes:
        config_hashes = _hashes_from_multifile_config_node(config_node)
        if config_hashes and export_hashes and config_hashes.intersection(export_hashes):
            return config_node
    return config_nodes[0]


def _multifile_node_payloads(blueprint_model: BluePrintModel) -> list[dict[str, object]]:
    payloads = []
    postprocess_nodes = getattr(blueprint_model, "postprocess_nodes", []) or []
    for export_node in getattr(blueprint_model, "multi_file_export_nodes", []) or []:
        option_count = len(getattr(export_node, "object_list", []) or [])
        if option_count <= 1:
            continue

        config_node = _multifile_config_for_export_node(postprocess_nodes, export_node)
        animation_variable = _normalize_ini_variable(
            getattr(config_node, "animation_swapkey", "") if config_node else "",
            "$swapkey100",
        )
        active_variable = _normalize_ini_variable(
            getattr(config_node, "active_swapkey", "") if config_node else "",
            ACTIVE_FLAG,
        )
        if active_variable == "$active0":
            active_variable = ACTIVE_FLAG
        active_value = _parse_int(getattr(config_node, "active_value", 1) if config_node else 1, 1)
        node_key = f"{export_node.id_data.name}::{export_node.name}" if getattr(export_node, "id_data", None) else export_node.name
        payloads.append(
            {
                "node_name": export_node.name,
                "node_key": node_key,
                "config_node_name": getattr(config_node, "name", "") if config_node else "",
                "animation_variable": animation_variable,
                "active_variable": active_variable,
                "active_value": active_value,
                "option_count": option_count,
                "comment": str(getattr(config_node, "comment", "") or "") if config_node else "",
            }
        )
    return payloads


def _collapse_ntmi_multifile_runtime_chains(blueprint_model: BluePrintModel) -> int:
    processing_chains = list(getattr(blueprint_model, "processing_chains", []) or [])
    if not processing_chains:
        return 0

    kept_chains = []
    dropped_count = 0
    for chain in processing_chains:
        node_key = str(getattr(chain, "multi_file_source_node_key", "") or "")
        option_index = getattr(chain, "multi_file_option_index", None)
        if node_key and option_index is not None and int(option_index) > 0:
            dropped_count += 1
            continue
        kept_chains.append(chain)

    if dropped_count <= 0:
        return 0

    blueprint_model.processing_chains = kept_chains
    merge_chains = getattr(blueprint_model, "_merge_processing_chains", None)
    if callable(merge_chains):
        merge_chains()
    rebuild_draw_models = getattr(blueprint_model, "_build_draw_call_models_from_chains", None)
    if callable(rebuild_draw_models):
        rebuild_draw_models()

    LOG.info(
        "NTMI ModImp: collapsed MultiFile runtime draw chains to base state only; "
        f"dropped {dropped_count} alternate chain(s)."
    )
    return dropped_count


def _apply_ntmi_multifile_conditions(
    blueprint_model: BluePrintModel,
    multifile_nodes: list[dict[str, object]],
    *,
    base_draw_only: bool = False,
):
    payload_by_key = {str(item["node_key"]): item for item in multifile_nodes}
    applied_count = 0
    for chain in getattr(blueprint_model, "processing_chains", []) or []:
        node_key = str(getattr(chain, "multi_file_source_node_key", "") or "")
        if not node_key:
            continue
        payload = payload_by_key.get(node_key)
        if not payload:
            continue
        option_index = getattr(chain, "multi_file_option_index", None)
        if option_index is None:
            continue
        state_index = int(option_index) + 1
        active_variable = str(payload["active_variable"])
        animation_variable = str(payload["animation_variable"])
        active_value = int(payload["active_value"])
        if base_draw_only and state_index == 1:
            condition = ""
        elif state_index == 1:
            condition = (
                f"{active_variable} != {active_value} "
                f"|| {animation_variable} == 0 "
                f"|| {animation_variable} == {state_index}"
            )
        else:
            condition = f"{active_variable} == {active_value} && {animation_variable} == {state_index}"
        setattr(chain, "ntmi_multifile_condition", condition)
        applied_count += 1
    if applied_count:
        LOG.info(f"NTMI ModImp: applied MultiFile draw conditions to {applied_count} chain(s).")


def _swap_node_payloads(blueprint_model: BluePrintModel) -> list[dict[str, object]]:
    registry = getattr(blueprint_model, "_swap_key_registry", None)
    if registry is None:
        return []

    payloads = []
    for fallback_index, node in enumerate(getattr(registry, "swapkey_nodes", []) or []):
        node_key = f"{node.id_data.name}::{node.name}" if getattr(node, "id_data", None) else node.name
        index = getattr(registry, "node_swapkey_map", {}).get(node_key, fallback_index)
        ensure_object_swap_variable_name(node)
        variable_name = get_node_variable_name(node)
        payloads.append(
            {
                "node_name": node.name,
                "node_key": node_key,
                "index": index,
                "section_name": f"KeySwap_NTMIModImp_{index}",
                "variable_name": variable_name,
                "hotkey": str(getattr(node, "hotkey", "") or "No_Modifiers Numpad3"),
                "swap_type": str(getattr(node, "swap_type", "") or "cycle"),
                "option_count": int(getattr(node, "input_slot_count", 2) or 2),
                "comment": str(getattr(node, "comment", "") or ""),
            }
        )

    payloads.sort(key=lambda item: int(item["index"]))
    return payloads


def _write_report(
    output_dir: str,
    *,
    build_result: ExportTreeBuildResult,
    export_results: list[dict[str, object]],
    object_conditions: dict[str, str],
    swap_nodes: list[dict[str, object]],
    multifile_nodes: list[dict[str, object]],
    requested_generate_ini: bool,
    effective_generate_ini: bool,
):
    payload = {
        "requested_generate_ini": requested_generate_ini,
        "effective_generate_ini": effective_generate_ini,
        "source_records": [asdict(record) for record in build_result.source_records],
        "warnings": build_result.warnings,
        "export_results": export_results,
        "object_conditions": object_conditions,
        "swap_nodes": swap_nodes,
        "multifile_nodes": multifile_nodes,
    }
    report_path = Path(output_dir) / "theherta4_ntmi_modimp_export_report.json"
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _execute_supported_postprocess_nodes(blueprint_model: BluePrintModel, output_dir: str, exporter=None):
    compatible_nodes = []
    for node in getattr(blueprint_model, "postprocess_nodes", []) or []:
        node_type = str(getattr(node, "bl_idname", "") or "")
        if node_type not in COMPATIBLE_POSTPROCESS_NODE_TYPES:
            if node_type in NTMI_INTERNAL_POSTPROCESS_NODE_TYPES:
                continue
            LOG.warning(f"Skip NTMI-incompatible postprocess node: {getattr(node, 'name', '')} ({node_type})")
            continue
        compatible_nodes.append(node)

    for node in compatible_nodes:
        node_class = type(node)
        clear_cache = getattr(node_class, "clear_cache", None)
        if callable(clear_cache):
            try:
                clear_cache()
            except Exception:
                pass

    name_mapping = dict(getattr(BluePrintModel, "_object_name_mapping", {}) or {})
    if name_mapping:
        LOG.info(f"NTMI ModImp: pass {len(name_mapping)} object name mapping rule(s) to postprocess nodes.")
        for node in compatible_nodes:
            apply_name_mapping = getattr(node, "apply_name_mapping", None)
            if not callable(apply_name_mapping):
                continue
            try:
                apply_name_mapping(name_mapping)
            except Exception as exc:
                LOG.warning(f"NTMI ModImp: postprocess node '{getattr(node, 'name', '')}' failed to apply name mapping: {exc}")

    multi_file_export_nodes = [
        node
        for node in getattr(blueprint_model, "multi_file_export_nodes", []) or []
        if len(getattr(node, "object_list", []) or []) > 1
    ]

    for node in compatible_nodes:
        execute_postprocess = getattr(node, "execute_postprocess", None)
        node_type = str(getattr(node, "bl_idname", "") or "")

        if node_type == "SSMTNode_PostProcess_ShapeKey":
            execute_ntmi_shapekey_postprocess(
                node=node,
                output_dir=output_dir,
                blueprint_model=blueprint_model,
                exporter=exporter,
            )
            continue

        if node_type == "SSMTNode_PostProcess_MultiFile":
            if not multi_file_export_nodes:
                LOG.info("NTMI ModImp: skip MultiFile postprocess because no multi-file export node requires animation output.")
                continue
            execute_ntmi_multifile_postprocess(
                config_node=node,
                multi_file_nodes=multi_file_export_nodes,
                output_dir=output_dir,
                exporter=exporter,
            )
            continue

        if not callable(execute_postprocess):
            LOG.warning(f"Skip unsupported postprocess node: {getattr(node, 'name', '')}")
            continue
        try:
            if node_type in {
                "SSMTNode_PostProcess_Material",
                "SSMTNode_PostProcess_CustomMaterialAssign",
            }:
                execute_postprocess(output_dir, exporter=exporter)
            else:
                execute_postprocess(output_dir)
        except NotImplementedError:
            LOG.warning(f"Skip postprocess node without implementation: {getattr(node, 'name', '')}")


def _sync_modimp_mirror_flags_after_preprocess(original_to_copy_map: dict[str, str]):
    if not GlobalProterties.enable_non_mirror_workflow():
        return

    disabled_count = 0
    for copy_name in (original_to_copy_map or {}).values():
        copy_obj = bpy.data.objects.get(copy_name)
        if copy_obj is None or copy_obj.type != "MESH":
            continue
        if MODIMP_MIRROR_FLIP_PROP not in copy_obj:
            continue
        if not bool(copy_obj.get(MODIMP_MIRROR_FLIP_PROP, False)):
            continue

        copy_obj[MODIMP_MIRROR_FLIP_PROP] = False
        disabled_count += 1

    if disabled_count:
        LOG.info(
            "NTMI ModImp: disabled inherited modimp_mirror_flip on "
            f"{disabled_count} preprocessed export object(s) to avoid double X mirror."
        )


def _numeric_vertex_group_names(obj: bpy.types.Object) -> list[str]:
    return [
        str(getattr(vertex_group, "name", "") or "")
        for vertex_group in getattr(obj, "vertex_groups", []) or []
        if str(getattr(vertex_group, "name", "") or "").isdigit()
    ]


def _validate_modimp_export_objects(export_objects: list[bpy.types.Object]):
    missing_numeric_groups = []
    for obj in export_objects:
        if obj is None or obj.type != "MESH":
            continue
        if _numeric_vertex_group_names(obj):
            continue
        missing_numeric_groups.append(obj.name)

    if missing_numeric_groups:
        preview = ", ".join(missing_numeric_groups[:8])
        remaining = len(missing_numeric_groups) - 8
        if remaining > 0:
            preview = f"{preview}, ... (+{remaining})"
        raise NTMIModImpExportError(
            "NTMI ModImp 导出需要数字命名的顶点组来生成蒙皮权重。"
            "以下导出物体未找到任何数字顶点组："
            f"{preview}。请先执行顶点组映射/处理，或将静态/非蒙皮网格从该 NTMI 导出链路中移除。"
        )


class ExportNTMIModImp:
    def __init__(self, blueprint_model: BluePrintModel, node=None, output_dir: str = ""):
        self.blueprint_model = blueprint_model
        self.node = node
        self.output_dir = output_dir or resolve_ntmi_modimp_output_dir(node)
        self.mod_importer_root = str(getattr(node, "mod_importer_root", "") or "").strip()
        self.flip_uv_v = bool(getattr(node, "flip_uv_v", False))
        self.default_mirror_flip = bool(getattr(node, "default_mirror_flip", False))
        self.generate_ini = bool(getattr(node, "generate_ini", True))
        self.force_buffer_only_when_contract_missing = bool(
            getattr(node, "force_buffer_only_when_contract_missing", True)
        )
        self.keep_temp_collection_tree = bool(getattr(node, "keep_temp_collection_tree", False))
        self.export_runtime_shapekeys = bool(getattr(node, "export_runtime_shapekeys", False))
        self.runtime_shapekey_names = str(getattr(node, "runtime_shapekey_names", "") or "").strip()
        self.extra_ps_t2_diffuse_map = bool(getattr(node, "extra_ps_t2_diffuse_map", False))

    def export(self) -> list[dict[str, object]]:
        multifile_nodes = _multifile_node_payloads(self.blueprint_model)
        _collapse_ntmi_multifile_runtime_chains(self.blueprint_model)
        _apply_ntmi_multifile_conditions(
            self.blueprint_model,
            multifile_nodes,
            base_draw_only=True,
        )
        refresh_texture_slots_for_objects(
            [
                obj
                for draw_call_model in getattr(self.blueprint_model, "ordered_draw_obj_data_model_list", []) or []
                for obj in [bpy.data.objects.get(draw_call_model.get_blender_obj_name())]
                if obj is not None and obj.type == "MESH"
            ]
        )
        build_result = build_export_tree(self.blueprint_model)
        export_results: list[dict[str, object]] = []
        effective_generate_ini = self.generate_ini
        if self.generate_ini and self.force_buffer_only_when_contract_missing and not build_result.has_full_ini_contract():
            effective_generate_ini = False
            LOG.warning(
                "NTMI ModImp: missing runtime contract fields; generated buffers only. "
                "See the JSON report for missing modimp_* fields."
            )

        try:
            dependency_status = detect_mod_importer_dependency(self.mod_importer_root)
            if not dependency_status.available:
                checked = "\n".join(dependency_status.checked_paths)
                raise NTMIModImpExportError(
                    "NTMI ModImp 依赖 Mod Importer。"
                    "请先安装/启用该依赖，或在输出节点上正确配置依赖路径。\n"
                    f"已检查路径：\n{checked}"
                )
            export_collection_package = get_export_collection_package(self.mod_importer_root)
            resolve_mod_importer_root(self.mod_importer_root)

            export_objects = [
                obj
                for root_col in build_result.root_collections
                for obj in root_col.all_objects
                if obj.type == "MESH"
            ]
            _validate_modimp_export_objects(export_objects)
            saved_texture_nodes = _strip_material_texture_nodes(export_objects)
            LOG.info(f"NTMI ModImp: detached material slots from {len(export_objects)} object(s) before export")

            try:
                for root_collection in build_result.root_collections:
                    result = export_collection_package(
                        collection_name=root_collection.name,
                        export_dir=self.output_dir,
                        flip_uv_v=self.flip_uv_v,
                        default_mirror_flip=self.default_mirror_flip,
                        generate_ini=effective_generate_ini,
                        export_runtime_shapekeys=self.export_runtime_shapekeys,
                        runtime_shapekey_names=self.runtime_shapekey_names or None,
                    )
                    export_results.append(dict(result))
            finally:
                _restore_material_texture_nodes(saved_texture_nodes)
                LOG.info("NTMI ModImp: restored material slots after export")

            object_conditions = _object_conditions_from_blueprint_model(build_result)
            swap_nodes = _swap_node_payloads(self.blueprint_model)

            if effective_generate_ini:
                for result in export_results:
                    ini_path = str(result.get("ini_path", "") or "")
                    if not ini_path:
                        continue
                    patch_ini_file(
                        ini_path,
                        swap_nodes=swap_nodes,
                        object_conditions=object_conditions,
                        multifile_nodes=multifile_nodes,
                    )

            _write_report(
                self.output_dir,
                build_result=build_result,
                export_results=export_results,
                object_conditions=object_conditions,
                swap_nodes=swap_nodes,
                multifile_nodes=multifile_nodes,
                requested_generate_ini=self.generate_ini,
                effective_generate_ini=effective_generate_ini,
            )

            return export_results
        finally:
            if not self.keep_temp_collection_tree:
                cleanup_collections(build_result.created_collection_names)

    def export_buffers_only(self):
        self.generate_ini = False
        return self.export()


class NTMIModImpExportSession:
    def __init__(self, context, tree, node):
        self.context = context
        self.tree = tree
        self.node = node
        self.output_dir = resolve_ntmi_modimp_output_dir(node)

    def _collect_object_names(self) -> list[str]:
        names = BlueprintExportHelper.collect_connected_preprocess_object_names(self.tree)
        return PreProcessHelper.collect_target_object_names_strict(names)

    def run(self):
        previous_result_type = BlueprintExportHelper.runtime_result_output_node_type
        previous_runtime_tree_name = BlueprintExportHelper.runtime_blueprint_tree_name
        previous_buffer_folder = BlueprintExportHelper.get_current_buffer_folder_name()
        previous_export_index = BlueprintExportHelper.current_export_index
        nested_trees = _collect_nested_trees(self.tree)

        BlueprintExportHelper.set_runtime_result_output_node_type(RESULT_NODE_TYPE)
        BlueprintExportHelper.set_runtime_blueprint_tree(self.tree)
        BlueprintExportHelper.set_current_export_index(1)
        BlueprintExportHelper.set_current_buffer_folder_name("Buffer")
        BluePrintModel.clear_object_name_mapping()
        GlobalKeyCountHelper.initialize()

        _reset_output_dir(self.output_dir)

        try:
            TimerUtils.start_stage("NTMI-ModImp-CollectObjects")
            object_names = self._collect_object_names()
            TimerUtils.end_stage("NTMI-ModImp-CollectObjects")

            if not object_names:
                raise NTMIModImpExportError("NTMI ModImp 输出节点未连接任何可导出的网格物体。")

            capture_direct_shapekeys = bool(
                getattr(self.node, "run_postprocess_nodes", True)
                and BlueprintExportHelper.collect_shapekey_postprocess_nodes(self.tree)
            )

            TimerUtils.start_stage("NTMI-ModImp-Preprocess")
            PreProcessHelper.recover_blueprint_node_references(self.tree, nested_trees)
            if capture_direct_shapekeys:
                BlueprintExportHelper.clear_direct_shapekey_position_records()
                original_to_copy_map = PreProcessHelper.execute_preprocess_capture_shape_keys(object_names)
            else:
                original_to_copy_map = PreProcessHelper.execute_preprocess(object_names)
            if original_to_copy_map:
                _sync_modimp_mirror_flags_after_preprocess(original_to_copy_map)
                PreProcessHelper.update_blueprint_node_references(self.tree, nested_trees)
            TimerUtils.end_stage("NTMI-ModImp-Preprocess")

            TimerUtils.start_stage("NTMI-ModImp-BlueprintModel")
            blueprint_model = BluePrintModel(tree=self.tree, context=self.context)
            TimerUtils.end_stage("NTMI-ModImp-BlueprintModel")

            TimerUtils.start_stage("NTMI-ModImp-Export")
            exporter = ExportNTMIModImp(
                blueprint_model=blueprint_model,
                node=self.node,
                output_dir=self.output_dir,
            )
            results = exporter.export()
            TimerUtils.end_stage("NTMI-ModImp-Export")

            if bool(getattr(self.node, "run_postprocess_nodes", True)):
                TimerUtils.start_stage("NTMI-ModImp-Postprocess")
                _execute_supported_postprocess_nodes(
                    blueprint_model,
                    self.output_dir,
                    exporter=exporter,
                )
                TimerUtils.end_stage("NTMI-ModImp-Postprocess")

            return results
        finally:
            try:
                PreProcessHelper.cleanup_copies()
            finally:
                BlueprintExportHelper.runtime_result_output_node_type = previous_result_type
                BlueprintExportHelper.runtime_blueprint_tree_name = previous_runtime_tree_name
                BlueprintExportHelper.set_current_buffer_folder_name(previous_buffer_folder)
                BlueprintExportHelper.set_current_export_index(previous_export_index)


def execute_ntmi_modimp_export(context, tree, node):
    session = NTMIModImpExportSession(context=context, tree=tree, node=node)
    return session.run()

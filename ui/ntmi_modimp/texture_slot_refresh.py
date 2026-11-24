from __future__ import annotations

import json
import os
from typing import Iterable

import bpy

try:
    from ...common.dhash_metadata import slot_dhash_fields
except ImportError:  # tests/ 以 fake 包骨架按文件路径加载本模块时走到
    import importlib.util as _importlib_util
    import sys as _sys

    _dhash_name = "theherta4_dhash_metadata"
    _dhash_mod = _sys.modules.get(_dhash_name)
    if _dhash_mod is None:
        _dhash_spec = _importlib_util.spec_from_file_location(
            _dhash_name,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "common", "dhash_metadata.py"),
        )
        _dhash_mod = _importlib_util.module_from_spec(_dhash_spec)
        _sys.modules[_dhash_name] = _dhash_mod
        _dhash_spec.loader.exec_module(_dhash_mod)
    slot_dhash_fields = _dhash_mod.slot_dhash_fields

from ...common.submesh_metadata import SubmeshMetadataResolver
from ...common.texture_metadata_helper import TextureMetadataResolver
from ...common.workspace_helper import WorkSpaceHelper
from .prefix_property_cache import get_prefix_record_props, has_prefix_record, replace_prefix_record_props, update_prefix_record_for_object


def _resolve_type_dir(workspace_unique_str: str) -> str:
    submesh_folder = WorkSpaceHelper.get_submesh_folder_path(workspace_unique_str)
    if not os.path.isdir(submesh_folder):
        return ""

    try:
        metadata = SubmeshMetadataResolver.resolve(workspace_unique_str)
        candidate = str(getattr(metadata, "extract_gametype_folder_path", "") or "").strip()
        if candidate and os.path.isdir(candidate):
            return candidate.rstrip("\\/")
    except Exception:
        pass

    for entry in sorted(os.scandir(submesh_folder), key=lambda item: item.name):
        if entry.is_dir() and entry.name.startswith("TYPE_"):
            return entry.path
    return ""


def _resolve_deduped_texture_dir(workspace_unique_str: str) -> str:
    lod_name, _bare_unique = WorkSpaceHelper.parse_lod_unique_str(workspace_unique_str)
    base_workspace = WorkSpaceHelper.get_submesh_folder_path(workspace_unique_str)
    base_dir = os.path.dirname(base_workspace)
    if lod_name:
        candidate = os.path.join(base_dir, "DedupedTextures")
        if os.path.isdir(candidate):
            return candidate
    workspace_root = os.path.dirname(base_dir) if lod_name else base_dir
    candidate = os.path.join(workspace_root, "LOD0", "DedupedTextures")
    if os.path.isdir(candidate):
        return candidate
    return ""


def _find_texture_in_type_dir(type_dir: str, bare_unique_str: str, mark_name: str) -> str:
    candidate = os.path.join(type_dir, f"{bare_unique_str}-{mark_name}.dds")
    if os.path.isfile(candidate):
        return candidate
    return ""


def _find_texture_in_deduped(deduped_dir: str, mark_hash: str) -> str:
    if not deduped_dir or not os.path.isdir(deduped_dir):
        return ""
    for entry in os.scandir(deduped_dir):
        if not entry.is_file() or not entry.name.lower().endswith(".dds"):
            continue
        if mark_hash.lower() in entry.name.lower():
            return entry.path
    return ""


def build_texture_slots_from_workspace_unique(workspace_unique_str: str) -> dict:
    workspace_unique_str = str(workspace_unique_str or "").strip()
    if not workspace_unique_str:
        return {}

    type_dir = _resolve_type_dir(workspace_unique_str)
    _lod_name, bare_unique_str = WorkSpaceHelper.parse_lod_unique_str(workspace_unique_str)
    if "." in bare_unique_str:
        bare_unique_str = bare_unique_str.split(".", 1)[0]
    deduped_dir = _resolve_deduped_texture_dir(workspace_unique_str)
    try:
        metadata = SubmeshMetadataResolver.resolve(workspace_unique_str)
    except Exception:
        return {}

    texture_markup_info_list = TextureMetadataResolver.normalize_texture_markup_info_list(
        getattr(metadata, "texture_markup_info_list", []) or []
    )

    slots = {}
    for texture_info in texture_markup_info_list:
        mark_name = str(getattr(texture_info, "mark_name", "") or "").strip()
        mark_hash = str(getattr(texture_info, "mark_hash", "") or "").strip()
        mark_slot = str(getattr(texture_info, "mark_slot", "") or "").strip()
        mark_type = str(getattr(texture_info, "mark_type", "") or "").strip()
        mark_filename = str(getattr(texture_info, "mark_filename", "") or "").strip()
        if not mark_name or not mark_hash:
            continue

        source_path = _find_texture_in_type_dir(type_dir, bare_unique_str, mark_name)
        if not source_path:
            source_path = _find_texture_in_deduped(deduped_dir, mark_hash)
        extension = os.path.splitext(source_path)[1].lstrip(".")
        if not extension:
            extension = os.path.splitext(mark_filename)[1].lstrip(".")
        if not extension:
            extension = "dds"
        if not mark_filename:
            mark_filename = f"{mark_hash}-{mark_name}.{extension}"

        slots[mark_slot] = {
            "hash": mark_hash,
            "source_path": source_path,
            "extension": extension,
            "mark_type": mark_type,
            "mark_name": mark_name,
            "mark_slot": mark_slot,
            "mark_hash": mark_hash,
            "mark_filename": mark_filename,
            # dHash 契约：统一由 slot_dhash_fields 逐别名 normalize。
            **slot_dhash_fields(texture_info),
        }
    return slots


def refresh_object_texture_slots(obj, extra_owners: Iterable[object] = ()) -> dict:
    cached_props = {}
    has_cached_record = False
    try:
        cached_props = get_prefix_record_props(getattr(obj, "name", ""))
        has_cached_record = has_prefix_record(getattr(obj, "name", ""))
    except Exception:
        cached_props = {}
        has_cached_record = False

    workspace_unique_str = ""
    if has_cached_record:
        workspace_unique_str = str(cached_props.get("modimp_workspace_unique_str", "") or "").strip()
    if not workspace_unique_str and not has_cached_record:
        workspace_unique_str = str(obj.get("modimp_workspace_unique_str", "") or "").strip()

    if not workspace_unique_str:
        for owner in (obj, *tuple(extra_owners or ())):
            try:
                owner.pop("modimp_texture_slots", None)
            except Exception:
                continue
        if has_cached_record:
            merged = dict(cached_props) if isinstance(cached_props, dict) else {}
            merged.pop("modimp_texture_slots", None)
            replace_prefix_record_props(getattr(obj, "name", ""), merged)
        else:
            update_prefix_record_for_object(obj, extra_owners=extra_owners)
        return {}

    slots = build_texture_slots_from_workspace_unique(workspace_unique_str)
    if not slots:
        for owner in (obj, *tuple(extra_owners or ())):
            try:
                owner.pop("modimp_texture_slots", None)
            except Exception:
                continue
        if has_cached_record:
            merged = dict(cached_props) if isinstance(cached_props, dict) else {}
            merged["modimp_workspace_unique_str"] = workspace_unique_str
            merged.pop("modimp_texture_slots", None)
            replace_prefix_record_props(getattr(obj, "name", ""), merged)
        else:
            update_prefix_record_for_object(obj, extra_owners=extra_owners)
        return {}

    serialized = json.dumps(slots, ensure_ascii=False)
    obj["modimp_texture_slots"] = serialized
    for owner in extra_owners or ():
        try:
            owner["modimp_texture_slots"] = serialized
        except Exception:
            continue

    if has_cached_record:
        merged = dict(cached_props) if isinstance(cached_props, dict) else {}
        merged["modimp_workspace_unique_str"] = workspace_unique_str
        merged["modimp_texture_slots"] = serialized
        replace_prefix_record_props(getattr(obj, "name", ""), merged)
    else:
        update_prefix_record_for_object(obj, extra_owners=extra_owners)
    return slots


def refresh_texture_slots_for_objects(objects: Iterable[object]):
    refreshed = {}
    for obj in objects or ():
        if obj is None:
            continue
        slots = refresh_object_texture_slots(obj, extra_owners=getattr(obj, "users_collection", []) or ())
        if slots:
            refreshed[getattr(obj, "name", "")] = slots
    return refreshed

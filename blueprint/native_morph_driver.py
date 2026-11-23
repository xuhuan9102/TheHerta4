from dataclasses import dataclass
import hashlib
import math
import re
import struct


NATIVE_MORPH_LOOKUP_COUNT_REGISTER = 94
NATIVE_MORPH_SOURCE_FILTER_REGISTER = 95
CAPTURE_SHADER_NAME = "zzmi_native_morph_capture.hlsl"
WEIGHT_SYNC_SHADER_NAME = "shapekey_weight_sync_guarded.hlsl"
CLEAR_SHADER_NAME = "zzmi_native_morph_clear.hlsl"
GENERIC_CAPTURE_SECTION = "ShaderRegexZZMINativeMorphCapture"
GENERIC_CAPTURE_COMMAND = "CommandListZZMINativeMorphCaptureGeneric"
SHADER_OVERRIDE_PREFIX = "ShaderOverrideZZMINativeMorphCapture"
GENERIC_CAPTURE_PATTERN = (
    "(?ms)^cs_5_0\\r?\\n"
    ".*?^dcl_resource_structured t0, 40\\r?\\n"
    ".*?^dcl_uav_structured u0, 40\\r?\\n"
    ".*?^ld_structured_indexable\\(structured_buffer, stride=40\\)"
    ".*?t0\\.xyzw\\r?\\n"
    ".*?^ld_structured_indexable\\(structured_buffer, stride=40\\)"
    ".*?t0\\.xyzw\\r?\\n"
    ".*?^ld_structured_indexable\\(structured_buffer, stride=40\\)"
    ".*?t0\\.xyxx\\r?\\n"
    ".*?^mad [^\\r\\n]*cb0\\[0\\]\\.zzzz[^\\r\\n]*\\r?\\n"
    ".*?^mad [^\\r\\n]*cb0\\[0\\]\\.zzzz[^\\r\\n]*\\r?\\n"
    ".*?^mad [^\\r\\n]*cb0\\[0\\]\\.zzzz[^\\r\\n]*\\r?\\n"
    ".*?^store_structured u0\\.xyz,[^\\r\\n]*l\\(24\\)"
)

_HEX_HASH_RE = re.compile(r"^[0-9a-fA-F]{8}$|^[0-9a-fA-F]{16}$")
_DEFORM_NAME_RE = re.compile(r"^Deform\s+(\d+)$", re.IGNORECASE)
_FILTER_INDEX_RE = re.compile(
    r"^\s*filter_index\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class NativeMorphKey:
    shader_hash: str
    source_hash: str
    constant_buffer_hash: str
    offset: int
    count: int
    shape_key_index: int


@dataclass(frozen=True)
class NativeMorphLookupEntry:
    source_filter: int
    source_hash: str
    shader_hash: str
    offset: int
    count: int
    shape_key_slot: int


@dataclass(frozen=True)
class NativeMorphPlan:
    logical_hash: str
    entries: tuple
    source_filters: dict
    shader_hashes: tuple
    lookup_count: int
    lookup_buffer: bytes


def _strict_uint32(value, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be uint32")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{label} must be uint32")
    parsed = int(value)
    if parsed < 0 or parsed > 0xFFFFFFFF:
        raise ValueError(f"{label} must be uint32")
    return parsed


def _normalized_hash(value, label: str) -> str:
    text = str(value or "").strip().lower()
    if not _HEX_HASH_RE.fullmatch(text):
        raise ValueError(f"{label} is not an 8/16 digit hash")
    return text


def parse_native_morph_keys(shape_keys_info: dict) -> tuple:
    if not isinstance(shape_keys_info, dict):
        return ()
    raw_keys = shape_keys_info.get("native_morph_keys", [])
    if not isinstance(raw_keys, list):
        return ()

    keys = []
    seen = set()
    for raw in raw_keys:
        if not isinstance(raw, dict):
            continue
        try:
            key = NativeMorphKey(
                shader_hash=_normalized_hash(raw.get("shader_hash"), "shader_hash"),
                source_hash=_normalized_hash(raw.get("source_hash"), "source_hash"),
                constant_buffer_hash=_normalized_hash(
                    raw.get("constant_buffer_hash"), "constant_buffer_hash"
                ),
                offset=_strict_uint32(raw.get("offset", 0), "offset"),
                count=_strict_uint32(raw.get("count", 0), "count"),
                shape_key_index=_strict_uint32(
                    raw.get("shape_key_index", 0), "shape_key_index"
                ),
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if key.count <= 0:
            continue
        identity = (
            key.shader_hash,
            key.source_hash,
            key.constant_buffer_hash,
            key.offset,
            key.count,
            key.shape_key_index,
        )
        if identity in seen:
            continue
        seen.add(identity)
        keys.append(key)
    return tuple(sorted(keys))


def shape_key_runtime_slot_by_index(shape_key_names) -> dict:
    result = {}
    for slot, name in enumerate(shape_key_names or []):
        match = _DEFORM_NAME_RE.fullmatch(str(name or "").strip())
        if not match:
            continue
        native_index = int(match.group(1))
        result.setdefault(native_index, int(slot))
    return result


def collect_reserved_filter_indices(sections) -> set:
    result = set()
    for section_name, lines in (sections or {}).items():
        if str(section_name).startswith(
            "[TextureOverrideZZMINativeMorphSource_"
        ):
            continue
        for line in lines or ():
            match = _FILTER_INDEX_RE.match(str(line or ""))
            if match:
                result.add(int(float(match.group(1))))
    return result


def _allocate_filter_index(source_hash: str, used_filter_indices: set) -> int:
    digest = hashlib.sha1(source_hash.encode("ascii")).digest()
    candidate = 100000 + int.from_bytes(digest[:4], "little") % 800000
    while candidate in used_filter_indices:
        candidate += 1
        if candidate > 999999:
            candidate = 100000
    used_filter_indices.add(candidate)
    return candidate


def build_native_morph_plans(
    keys_by_hash: dict,
    runtime_slot_by_native_index: dict,
    reserved_filter_indices=None,
) -> dict:
    used_filter_indices = set()
    if reserved_filter_indices is not None:
        if isinstance(reserved_filter_indices, dict):
            used_filter_indices.update(
                int(value) for value in reserved_filter_indices.values()
            )
        else:
            used_filter_indices.update(int(value) for value in reserved_filter_indices)

    plans = {}
    source_filter_by_hash = {}
    for logical_hash, keys in sorted((keys_by_hash or {}).items()):
        slot_map = dict((runtime_slot_by_native_index or {}).get(logical_hash, {}) or {})
        source_filters = {}
        shader_hashes = set()
        entries = []
        seen_rows = set()
        for key in sorted(keys or ()):
            if key.shape_key_index not in slot_map:
                continue
            source_filter = source_filter_by_hash.get(key.source_hash)
            if source_filter is None:
                source_filter = _allocate_filter_index(
                    key.source_hash, used_filter_indices
                )
                source_filter_by_hash[key.source_hash] = source_filter
            source_filters[key.source_hash] = source_filter
            shape_key_slot = int(slot_map[key.shape_key_index])
            row_identity = (
                source_filter,
                key.offset,
                key.count,
                shape_key_slot,
            )
            shader_hashes.add(key.shader_hash)
            if row_identity in seen_rows:
                continue
            seen_rows.add(row_identity)
            entries.append(
                NativeMorphLookupEntry(
                    source_filter=source_filter,
                    source_hash=key.source_hash,
                    shader_hash=key.shader_hash,
                    offset=key.offset,
                    count=key.count,
                    shape_key_slot=shape_key_slot,
                )
            )
        if not entries:
            continue
        lookup_buffer = b"".join(
            struct.pack(
                "<IIII",
                entry.source_filter,
                entry.offset,
                entry.count,
                entry.shape_key_slot,
            )
            for entry in entries
        )
        plans[logical_hash] = NativeMorphPlan(
            logical_hash=logical_hash,
            entries=tuple(entries),
            source_filters=source_filters,
            shader_hashes=tuple(sorted(shader_hashes)),
            lookup_count=len(entries),
            lookup_buffer=lookup_buffer,
        )
    return plans


def safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "_", str(value or "")) or "0"


def derive_native_morph_lookup_resource_name(
    base_resource_name: str,
    logical_hash: str = "",
) -> str:
    clean_name = re.sub(r"[^0-9A-Za-z_]", "_", str(base_resource_name or ""))
    if clean_name.endswith("_Position") or clean_name.endswith("Position"):
        resource_name = clean_name + "_NativeMorphLookup"
    else:
        resource_name = clean_name + "_Position_NativeMorphLookup"
    if logical_hash:
        resource_name += "_" + safe_name(logical_hash)
    return resource_name


def custom_shader_capture_name(logical_hash: str) -> str:
    return f"CustomShaderZZMINativeMorphCapture_{safe_name(logical_hash)}"


def custom_shader_clear_name(logical_hash: str) -> str:
    return f"CustomShaderZZMINativeMorphClear_{safe_name(logical_hash)}"


def shader_override_name(shader_hash: str) -> str:
    return f"{SHADER_OVERRIDE_PREFIX}_{safe_name(shader_hash)}"


def _group_capture_commands(plans_by_hash: dict) -> list:
    grouped_by_filter = {}
    for logical_hash, plan in sorted(plans_by_hash.items()):
        for entry in plan.entries:
            grouped_by_filter.setdefault(entry.source_filter, []).append(
                (logical_hash, entry)
            )

    command_lines = [
        "local $zzmi_saved_x94 = x94",
        "local $zzmi_saved_x95 = x95",
        "x95 = cs-t0",
        "local $zzmi_morph_source_filter = x95",
    ]
    for source_filter, grouped_entries in sorted(grouped_by_filter.items()):
        command_lines.append(
            f"if $zzmi_morph_source_filter == {int(source_filter)}"
        )
        emitted_hashes = set()
        for logical_hash, _entry in grouped_entries:
            if logical_hash in emitted_hashes:
                continue
            emitted_hashes.add(logical_hash)
            plan = plans_by_hash[logical_hash]
            command_lines.extend(
                [
                    f"    x94 = {plan.lookup_count}",
                    f"    run = {custom_shader_capture_name(logical_hash)}",
                ]
            )
        command_lines.append("endif")
    command_lines.extend(
        [
            "x94 = $zzmi_saved_x94",
            "x95 = $zzmi_saved_x95",
        ]
    )
    return command_lines


def render_native_morph_ini_sections(
    plans_by_hash: dict,
    lookup_resource_by_hash: dict,
    weight_resource_by_hash: dict,
    lookup_filename_by_hash: dict,
    capture_shader_rel: str,
    clear_shader_rel: str,
) -> dict:
    sections = {}
    if not plans_by_hash:
        return sections

    source_to_filter = {}
    for plan in plans_by_hash.values():
        source_to_filter.update(plan.source_filters)
    for source_hash, source_filter in sorted(source_to_filter.items()):
        safe_hash = safe_name(source_hash)
        sections[f"[TextureOverrideZZMINativeMorphSource_{safe_hash}]"] = [
            f"hash = {source_hash}",
            f"filter_index = {source_filter}",
        ]

    shader_hashes = sorted(
        {
            shader_hash
            for plan in plans_by_hash.values()
            for shader_hash in plan.shader_hashes
        }
    )
    for shader_hash in shader_hashes:
        sections[f"[{shader_override_name(shader_hash)}]"] = [
            f"hash = {shader_hash}",
            f"post run = {GENERIC_CAPTURE_COMMAND}",
        ]

    sections[f"[{GENERIC_CAPTURE_SECTION}]"] = [
        "shader_model = cs_5_0",
        f"post run = {GENERIC_CAPTURE_COMMAND}",
    ]
    sections[f"[{GENERIC_CAPTURE_SECTION}.Pattern]"] = [
        GENERIC_CAPTURE_PATTERN,
    ]
    sections[f"[{GENERIC_CAPTURE_COMMAND}]"] = _group_capture_commands(
        plans_by_hash
    )

    for logical_hash, plan in sorted(plans_by_hash.items()):
        safe_hash = safe_name(logical_hash)
        lookup_resource = lookup_resource_by_hash[logical_hash]
        weight_resource = weight_resource_by_hash[logical_hash]
        lookup_filename = lookup_filename_by_hash[logical_hash]
        dispatch_count = max(1, math.ceil(plan.lookup_count / 64))
        saved_t0 = f"ResourceZZMINativeMorphSavedCST0_{safe_hash}"
        saved_cb0 = f"ResourceZZMINativeMorphSavedCSCB0_{safe_hash}"
        saved_u0 = f"ResourceZZMINativeMorphSavedCSU0_{safe_hash}"
        sections[f"[{lookup_resource}]"] = [
            "type = Buffer",
            "stride = 16",
            f"filename = {lookup_filename}",
        ]
        for saved_resource in (saved_t0, saved_cb0, saved_u0):
            sections[f"[{saved_resource}]"] = []
        sections[f"[{custom_shader_capture_name(logical_hash)}]"] = [
            f"{saved_t0} = ref cs-t0",
            f"{saved_cb0} = ref cs-cb0",
            f"{saved_u0} = ref cs-u0",
            f"cs = {capture_shader_rel}",
            f"cs-t0 = copy {lookup_resource}",
            f"cs-cb0 = ref {saved_cb0}",
            f"cs-u0 = {weight_resource}",
            f"Dispatch = {dispatch_count}, 1, 1",
            f"post cs-t0 = ref {saved_t0}",
            f"post cs-cb0 = ref {saved_cb0}",
            f"post cs-u0 = ref {saved_u0}",
            f"post {saved_t0} = null",
            f"post {saved_cb0} = null",
            f"post {saved_u0} = null",
        ]

        clear_dispatch = max(1, math.ceil(plan.lookup_count / 64))
        sections[f"[{custom_shader_clear_name(logical_hash)}]"] = [
            f"{saved_t0} = ref cs-t0",
            f"{saved_u0} = ref cs-u0",
            f"cs = {clear_shader_rel}",
            f"cs-t0 = copy {lookup_resource}",
            f"cs-u0 = {weight_resource}",
            f"Dispatch = {clear_dispatch}, 1, 1",
            f"post cs-t0 = ref {saved_t0}",
            f"post cs-u0 = ref {saved_u0}",
            f"post {saved_t0} = null",
            f"post {saved_u0} = null",
        ]
    return sections

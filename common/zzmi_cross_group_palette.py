# -*- coding: utf-8 -*-
"""ZZMI 跨组骨骼合并：调色板归一（palette 锚点链）的契约与运行期表。

本模块无 ``bpy`` 依赖。提取侧、Blender 导入侧与 ZZMI 导出侧通过工作区里的
``ZZMI_CrossGroupPalette.json`` 交换数据。

## 绝区零怎么摆一个角色

每个部件（一个 IB）先由自己的 stream-output draw 蒙皮，再参与渲染。那笔画
draw 把该部件的骨骼表绑在 ``vs-t0``（``float4x3`` 行，每骨三个 ``float4``），
顶点着色器直接读 ``bones[BLENDINDICES]``。游戏在部件自己那笔蒙皮 draw 之前
用 ``Map(WRITE_DISCARD)`` 上传这份表。

每份表表达在**该部件根骨的空间**里，索引 draw 再用部件自己的物体矩阵把蒙皮
后的顶点摆到世界。所以同一根骨骼在每个部件里矩阵都不同。

两个部件若共用一根骨骼，它们在世界空间里对这根骨**一致**；这根共用骨（锚点）
给出两个空间之间的换算，而且不需要索引 draw 的任何数据：

    E(父 <- 子) = P_父[锚点] * inverse(P_子[锚点])

合并骨架把每个合并部件的表按 ``vg_offset`` 放进同一个池。部件被蒙皮之前，先用
池里建出一份**以它自己空间表达**的完整表（别的部件的骨骼经锚点链换算），于是
任何部件画出来的网格都能用上所有合并部件的骨骼。

形态键驱动的部件（脸）每帧由 compute pass 重写位置缓冲，不参与合并；和合并部件
没有共用骨的部件换算不了，保持独立。
"""

from __future__ import annotations

import hashlib
import json
import os

import numpy


PROFILE_FILENAME = "ZZMI_CrossGroupPalette.json"
PROFILE_MODE = "ZZMI_CROSS_GROUP_PALETTE"
PROFILE_FORMAT_VERSION = 1

ROLE_MERGED = "merged"
ROLE_SHAPE_KEYED = "shape_keyed"
ROLE_STANDALONE = "standalone"
ROLES = (ROLE_MERGED, ROLE_SHAPE_KEYED, ROLE_STANDALONE)

# 游戏的蒙皮着色器声明 ``float4x3 bones[1024]``。
MAX_POOL_BONES = 1024
NO_PARENT = -1

# 骨骼矩阵的世界空间匹配。匹配上的骨骼在采集帧里一致到 ~2e-5（世界坐标的
# float32 精度），不同的骨骼差得远得多。平移容差随离世界原点的距离放大，因为
# 输入是 float32。
MATCH_ROTATION_TOLERANCE = 1e-4
MATCH_TRANSLATION_TOLERANCE = 1e-3
MATCH_TRANSLATION_RELATIVE_TOLERANCE = 4e-6

# 一条锚点边至少要这么多根互唯一骨对，和参考实现一致：只对上 1 根的部件按它的
# 说法属于"只通过采集帧里不动的骨碰到角色"，无法可靠锚定，保留自己的顶点组。
MIN_ANCHOR_PAIRS = 2


class ZZMICrossGroupPaletteError(ValueError):
    """跨组骨骼合并配置/数据不合法。"""


# ---------------------------------------------------------------------------
# 配置契约
# ---------------------------------------------------------------------------

def get_profile_path(workspace_folder: str, lod: str = "LOD0") -> str:
    filename = PROFILE_FILENAME if lod == "LOD0" else f"{PROFILE_FILENAME[:-5]}_{lod}.json"
    return os.path.join(os.path.abspath(str(workspace_folder)), filename)


def _as_int(value, field_name: str, minimum: int | None = 0) -> int:
    if isinstance(value, bool):
        raise ZZMICrossGroupPaletteError(field_name + " 必须是整数。")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ZZMICrossGroupPaletteError(field_name + " 必须是整数。")
    if minimum is not None and result < minimum:
        raise ZZMICrossGroupPaletteError(
            field_name + " 不能小于 " + str(minimum) + "。"
        )
    return result


def normalize_vg_map(vg_map, field_name: str = "vg_map") -> dict[str, int]:
    if vg_map is None:
        return {}
    if not isinstance(vg_map, dict):
        raise ZZMICrossGroupPaletteError(field_name + " 必须是对象映射。")
    normalized = {}
    for local_id, global_id in vg_map.items():
        normalized[str(_as_int(local_id, field_name + " 的本地骨骼"))] = _as_int(
            global_id, field_name + " 的全局骨骼"
        )
    return dict(sorted(normalized.items(), key=lambda item: int(item[0])))


def _slot_in(component: dict, slot: int) -> bool:
    return component["vg_offset"] <= slot < component["vg_offset"] + component["vg_count"]


def validate_profile(profile: dict) -> dict:
    """逐项校验并规范化配置；不合法直接抛，不猜。"""
    if not isinstance(profile, dict):
        raise ZZMICrossGroupPaletteError("骨骼合并配置根节点必须是 JSON 对象。")
    if profile.get("mode") != PROFILE_MODE:
        raise ZZMICrossGroupPaletteError(
            "骨骼合并配置 mode 无效，期望 " + PROFILE_MODE + "。"
        )
    if _as_int(profile.get("format_version", 0), "format_version") != PROFILE_FORMAT_VERSION:
        raise ZZMICrossGroupPaletteError(
            "不支持的骨骼合并配置版本: " + str(profile.get("format_version"))
        )
    raw_components = profile.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise ZZMICrossGroupPaletteError("骨骼合并配置没有 components。")

    components = []
    seen_ib = set()
    for list_index, raw in enumerate(raw_components):
        if not isinstance(raw, dict):
            raise ZZMICrossGroupPaletteError(
                "components[" + str(list_index) + "] 必须是对象。"
            )
        component = dict(raw)
        label = "components[" + str(list_index) + "]"
        if _as_int(component.get("component_id", -1), label + ".component_id") != list_index:
            raise ZZMICrossGroupPaletteError("component_id 必须从 0 连续编号。")
        component["component_id"] = list_index
        component["ib_hash"] = str(component.get("ib_hash", "")).strip().lower()
        if not component["ib_hash"]:
            raise ZZMICrossGroupPaletteError(label + " 缺少 ib_hash。")
        if component["ib_hash"] in seen_ib:
            raise ZZMICrossGroupPaletteError("ib_hash 重复: " + component["ib_hash"])
        seen_ib.add(component["ib_hash"])
        label = component["ib_hash"]

        component["role"] = str(component.get("role", ""))
        if component["role"] not in ROLES:
            raise ZZMICrossGroupPaletteError(
                label + " 的 role 无效: " + component["role"]
            )
        component["reason"] = str(component.get("reason", ""))
        component["pose_vs"] = str(component.get("pose_vs", "")).strip().lower()
        component["pose_order"] = _as_int(component.get("pose_order", 0), label + ".pose_order")
        component["vertex_count"] = _as_int(component.get("vertex_count", 0), label + ".vertex_count")
        component["palette_bone_count"] = _as_int(
            component.get("palette_bone_count", 0), label + ".palette_bone_count"
        )

        if component["role"] == ROLE_MERGED:
            component["vg_offset"] = _as_int(component.get("vg_offset", 0), label + ".vg_offset")
            component["vg_count"] = _as_int(component.get("vg_count", 0), label + ".vg_count", 1)
            component["vg_map"] = normalize_vg_map(component.get("vg_map"), label + ".vg_map")
            missing = [
                local_id for local_id in range(component["vg_count"])
                if str(local_id) not in component["vg_map"]
            ]
            if missing:
                raise ZZMICrossGroupPaletteError(
                    label + " 的 vg_map 不完整，缺少本地骨骼: "
                    + ", ".join(map(str, missing[:8]))
                )
            component["parent_component_id"] = _as_int(
                component.get("parent_component_id", NO_PARENT), label + ".parent_component_id"
            )
            component["anchor_parent_slot"] = _as_int(
                component.get("anchor_parent_slot", 0), label + ".anchor_parent_slot"
            )
            component["anchor_self_slot"] = _as_int(
                component.get("anchor_self_slot", 0), label + ".anchor_self_slot"
            )
        else:
            component["vg_offset"] = 0
            component["vg_count"] = 0
            component["vg_map"] = {}
            component["parent_component_id"] = NO_PARENT
            component["anchor_parent_slot"] = 0
            component["anchor_self_slot"] = 0
        components.append(component)

    merged = [component for component in components if component["role"] == ROLE_MERGED]
    if not merged:
        raise ZZMICrossGroupPaletteError("骨骼合并配置里没有参与合并的部件。")
    expected_offset = 0
    for component in merged:
        if component["vg_offset"] != expected_offset:
            raise ZZMICrossGroupPaletteError(
                component["ib_hash"] + " 的 vg_offset 应为 " + str(expected_offset)
                + "，实际为 " + str(component["vg_offset"]) + "。"
            )
        expected_offset += component["vg_count"]
    bones_count = expected_offset
    if bones_count > MAX_POOL_BONES:
        raise ZZMICrossGroupPaletteError(
            "合并后的骨骼数 " + str(bones_count)
            + " 超过游戏蒙皮着色器上限 " + str(MAX_POOL_BONES) + "。"
        )

    by_id = {component["component_id"]: component for component in components}
    roots = [
        component for component in merged
        if component["parent_component_id"] == component["component_id"]
    ]
    if len(roots) != 1:
        raise ZZMICrossGroupPaletteError("骨骼合并配置必须正好有一个根部件。")
    for component in merged:
        parent = by_id.get(component["parent_component_id"])
        if parent is None or parent["role"] != ROLE_MERGED:
            raise ZZMICrossGroupPaletteError(
                component["ib_hash"] + " 的锚点父部件无效。"
            )
        for slot_key in ("anchor_parent_slot", "anchor_self_slot"):
            if component[slot_key] >= bones_count:
                raise ZZMICrossGroupPaletteError(
                    component["ib_hash"] + " 的 " + slot_key + " 超出骨骼池。"
                )
        if parent is not component:
            if not _slot_in(parent, component["anchor_parent_slot"]):
                raise ZZMICrossGroupPaletteError(
                    component["ib_hash"] + " 的锚点不在父部件的骨骼范围内。"
                )
            if not _slot_in(component, component["anchor_self_slot"]):
                raise ZZMICrossGroupPaletteError(
                    component["ib_hash"] + " 的锚点不在自身骨骼范围内。"
                )
        for global_id in component["vg_map"].values():
            if global_id >= bones_count:
                raise ZZMICrossGroupPaletteError(
                    component["ib_hash"] + " 的 vg_map 超出骨骼池。"
                )
        # 锚点链必须无环地走到根。
        visited = set()
        current = component
        while current["parent_component_id"] != current["component_id"]:
            if current["component_id"] in visited:
                raise ZZMICrossGroupPaletteError(component["ib_hash"] + " 的锚点链有环。")
            visited.add(current["component_id"])
            current = by_id[current["parent_component_id"]]

    result = dict(profile)
    result["format_version"] = PROFILE_FORMAT_VERSION
    result["mode"] = PROFILE_MODE
    result["components"] = components
    result["bones_count"] = bones_count
    result["root_component_id"] = roots[0]["component_id"]
    result["source_frame_dump"] = str(result.get("source_frame_dump", "")).strip()
    return result


def load_profile(workspace_folder: str, required: bool = True, *, lod: str = "LOD0") -> dict | None:
    path = get_profile_path(workspace_folder, lod)
    if not os.path.isfile(path):
        if required:
            raise ZZMICrossGroupPaletteError(
                "当前工作空间没有 " + PROFILE_FILENAME
                + "，请先在绝区零预设下用「骨骼合并」提取。"
            )
        return None
    try:
        with open(path, "r", encoding="utf-8") as file:
            profile = json.load(file)
    except Exception as exc:  # noqa: BLE001 - 交给调用方决定怎么报
        raise ZZMICrossGroupPaletteError("读取骨骼合并配置失败: " + repr(exc))
    return validate_profile(profile)


def write_profile(workspace_folder: str, profile: dict, *, lod: str = "LOD0") -> str:
    normalized = validate_profile(profile)
    path = get_profile_path(workspace_folder, lod)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(normalized, file, ensure_ascii=False, indent=4)
    return path


def merged_components(profile: dict) -> list[dict]:
    return [component for component in profile["components"] if component["role"] == ROLE_MERGED]


def get_component_by_ib(profile: dict) -> dict[str, dict]:
    return {component["ib_hash"]: component for component in profile["components"]}


# ---------------------------------------------------------------------------
# 导出：canonical id -> 池槽位
# ---------------------------------------------------------------------------

def build_private_remap(component: dict) -> dict[int, int]:
    """把 canonical 全局 id 映射到这个部件**自己**那一份骨骼。

    Blender 里一根 canonical 骨只有一个顶点组，权重可以跨部件流动；画这个部件
    时，它自己有的骨就读自己的表（当帧、逐位精确），只有它没有的骨才保留别的
    部件的 canonical 槽位。
    """
    vg_offset = int(component["vg_offset"])
    local_ids_by_canonical: dict[int, list[int]] = {}
    for local_text, canonical in component["vg_map"].items():
        local_ids_by_canonical.setdefault(int(canonical), []).append(int(local_text))
    remap = {}
    for canonical, local_ids in local_ids_by_canonical.items():
        own = canonical - vg_offset
        remap[canonical] = vg_offset + (own if own in local_ids else min(local_ids))
    return remap


# ---------------------------------------------------------------------------
# 运行期表（直接写进 mod 的 ini）
# ---------------------------------------------------------------------------

def runtime_component_table(profile: dict) -> list[int]:
    """每个合并部件两个 ``uint4``，按池顺序。

    [2k + 0] = (vg_offset, vg_count, 父部件在表里的下标, 1)
    [2k + 1] = (锚点在父部件里的槽位, 锚点在本部件里的槽位, 0, 0)

    下标是这张表里的位置，不是配置里的 component_id。
    """
    merged = merged_components(profile)
    index_by_id = {component["component_id"]: index for index, component in enumerate(merged)}
    values = []
    for component in merged:
        values += [
            component["vg_offset"], component["vg_count"],
            index_by_id[component["parent_component_id"]], 1,
            component["anchor_parent_slot"], component["anchor_self_slot"], 0, 0,
        ]
    return values


def runtime_component_index(profile: dict, component_id: int) -> int:
    for index, component in enumerate(merged_components(profile)):
        if component["component_id"] == int(component_id):
            return index
    raise ZZMICrossGroupPaletteError("部件 " + str(component_id) + " 不参与骨骼合并。")


def runtime_params(profile: dict, component_id: int) -> list[int]:
    """``uint4``（表下标、合并部件数、池骨骼数、0）。"""
    return [
        runtime_component_index(profile, component_id),
        len(merged_components(profile)),
        int(profile["bones_count"]),
        0,
    ]


# ---------------------------------------------------------------------------
# 运行期着色器（由导出侧写到 mod 的 Shaders/ 下）
# ---------------------------------------------------------------------------

IMPORT_SHADER_FILENAME = "ZZMICrossGroupPalette_Import.hlsl"
PALETTE_SHADER_FILENAME = "ZZMICrossGroupPalette_Build.hlsl"

IMPORT_SHADER_SOURCE = r"""// ZZMI 跨组骨骼合并：骨骼导入。
// 把本部件蒙皮 draw 绑在 vs-t0 的那份骨骼表，拷进合并骨骼池里本部件的区间。

// 按游戏蒙皮着色器的方式读这张表（tbuffer），这样游戏缓冲的视图格式不影响这里。
tbuffer ComponentBones : register(t0)
{
    float4 ComponentBoneRows[3072];
};

// 每个合并部件两个条目：
//   [2k + 0] = (vg_offset, vg_count, 父部件下标, 1)
//   [2k + 1] = (锚点在父部件里的槽位, 锚点在本部件里的槽位, 0, 0)
Buffer<uint4> Components : register(t1);

// (本部件在表里的下标, 合并部件数, 池骨骼数, 0)
Buffer<uint4> Params : register(t2);

RWBuffer<float4> Pool : register(u0);

[numthreads(64, 1, 1)]
void main(uint3 thread_id : SV_DispatchThreadID)
{
    uint4 params = Params[0];
    uint4 component = Components[params.x * 2];
    uint bone = thread_id.x;
    if (bone >= component.y)
        return;

    uint src = bone * 3;
    uint dst = (component.x + bone) * 3;
    Pool[dst + 0] = ComponentBoneRows[src + 0];
    Pool[dst + 1] = ComponentBoneRows[src + 1];
    Pool[dst + 2] = ComponentBoneRows[src + 2];
}
"""

PALETTE_SHADER_SOURCE = r"""// ZZMI 跨组骨骼合并：调色板构建。
// 为某个部件的蒙皮 draw 建一份表。每个池槽位都表达在**该部件根骨的空间**里，
// 别的部件的骨骼经共用骨（锚点）换算：
//     E(父 <- 子) = Pool[父的锚点] * inverse(Pool[子的锚点])

Buffer<float4> Pool : register(t0);

// 每个合并部件两个条目，布局见 Import.hlsl。
Buffer<uint4> Components : register(t1);

// (本部件在表里的下标, 合并部件数, 池骨骼数, 0)
Buffer<uint4> Params : register(t2);

RWBuffer<float4> Palette : register(u0);

// 3x4 仿射矩阵，按行存，和游戏的 float4x3 bones 一致。
struct Affine
{
    float4 r0;
    float4 r1;
    float4 r2;
};

Affine Identity()
{
    Affine m;
    m.r0 = float4(1, 0, 0, 0);
    m.r1 = float4(0, 1, 0, 0);
    m.r2 = float4(0, 0, 1, 0);
    return m;
}

Affine LoadSlot(uint slot)
{
    Affine m;
    m.r0 = Pool[slot * 3 + 0];
    m.r1 = Pool[slot * 3 + 1];
    m.r2 = Pool[slot * 3 + 2];
    return m;
}

// a * b，两者底行都隐含 (0, 0, 0, 1)。
float4 MulRow(float4 row, Affine b)
{
    return row.x * b.r0 + row.y * b.r1 + row.z * b.r2 + float4(0, 0, 0, row.w);
}

Affine Mul(Affine a, Affine b)
{
    Affine m;
    m.r0 = MulRow(a.r0, b);
    m.r1 = MulRow(a.r1, b);
    m.r2 = MulRow(a.r2, b);
    return m;
}

Affine Inverse(Affine m)
{
    float3 a = m.r0.xyz;
    float3 b = m.r1.xyz;
    float3 c = m.r2.xyz;
    float3 c0 = cross(b, c);
    float3 c1 = cross(c, a);
    float3 c2 = cross(a, b);
    float det = dot(a, c0);
    // 从没导入过的槽位是零；保证结果有限。
    Affine r = Identity();
    if (abs(det) >= 1e-20)
    {
        float inv_det = 1.0 / det;
        // 逆 3x3 的行就是余子式矩阵的列。
        float3 i0 = float3(c0.x, c1.x, c2.x) * inv_det;
        float3 i1 = float3(c0.y, c1.y, c2.y) * inv_det;
        float3 i2 = float3(c0.z, c1.z, c2.z) * inv_det;
        float3 t = float3(m.r0.w, m.r1.w, m.r2.w);
        r.r0 = float4(i0, -dot(i0, t));
        r.r1 = float4(i1, -dot(i1, t));
        r.r2 = float4(i2, -dot(i2, t));
    }
    return r;
}

// 从部件 k 的空间换算到根的空间。
Affine ToRoot(uint k)
{
    Affine acc = Identity();
    [loop]
    for (uint step = 0; step < 32; ++step)
    {
        uint4 entry = Components[k * 2 + 0];
        if (entry.z == k)
            break;
        uint4 anchor = Components[k * 2 + 1];
        Affine edge = Mul(LoadSlot(anchor.x), Inverse(LoadSlot(anchor.y)));
        acc = Mul(edge, acc);
        k = entry.z;
    }
    return acc;
}

[numthreads(64, 1, 1)]
void main(uint3 thread_id : SV_DispatchThreadID)
{
    uint4 params = Params[0];
    uint slot = thread_id.x;
    if (slot >= params.z)
        return;

    uint owner = params.y;
    [loop]
    for (uint k = 0; k < params.y; ++k)
    {
        uint4 entry = Components[k * 2 + 0];
        if (slot >= entry.x && slot < entry.x + entry.y)
        {
            owner = k;
            break;
        }
    }

    Affine bone = LoadSlot(slot);
    if (owner != params.x && owner < params.y)
    {
        Affine to_self = Mul(Inverse(ToRoot(params.x)), ToRoot(owner));
        bone = Mul(to_self, bone);
    }
    Palette[slot * 3 + 0] = bone.r0;
    Palette[slot * 3 + 1] = bone.r1;
    Palette[slot * 3 + 2] = bone.r2;
}
"""


def runtime_shader_sources() -> list[tuple[str, str]]:
    return [
        (IMPORT_SHADER_FILENAME, IMPORT_SHADER_SOURCE),
        (PALETTE_SHADER_FILENAME, PALETTE_SHADER_SOURCE),
    ]


def write_runtime_shaders(shader_folder: str) -> list[str]:
    os.makedirs(shader_folder, exist_ok=True)
    paths = []
    for filename, source in runtime_shader_sources():
        path = os.path.join(shader_folder, filename)
        with open(path, "w", encoding="utf-8", newline="\n") as file:
            file.write(source)
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# 运行期的参考实现（测试与提取自检用）
# ---------------------------------------------------------------------------

def _to_affine44(rows: numpy.ndarray) -> numpy.ndarray:
    matrix = numpy.zeros(rows.shape[:-2] + (4, 4), dtype=numpy.float64)
    matrix[..., :3, :] = rows
    matrix[..., 3, 3] = 1.0
    return matrix


def reference_palette(profile: dict, pool: numpy.ndarray, component_id: int) -> numpy.ndarray:
    """``component_id`` 的归一表（float64），即着色器会写出的内容。

    ``pool`` 是 ``bones_count`` 根骨、形状 (3, 4)。
    """
    merged = merged_components(profile)
    table = runtime_component_table(profile)
    pool = numpy.asarray(pool, dtype=numpy.float64).reshape(-1, 3, 4)

    def to_root(index: int) -> numpy.ndarray:
        accumulated = numpy.eye(4)
        for _ in range(32):
            parent = table[index * 8 + 2]
            if parent == index:
                break
            parent_slot, self_slot = table[index * 8 + 4], table[index * 8 + 5]
            edge = _to_affine44(pool[parent_slot]) @ numpy.linalg.inv(
                _to_affine44(pool[self_slot])
            )
            accumulated = edge @ accumulated
            index = parent
        return accumulated

    own_index = runtime_component_index(profile, component_id)
    to_self_root_inverse = numpy.linalg.inv(to_root(own_index))
    palette = pool.copy()
    for index, component in enumerate(merged):
        if index == own_index:
            continue
        conversion = to_self_root_inverse @ to_root(index)
        rows = slice(component["vg_offset"], component["vg_offset"] + component["vg_count"])
        palette[rows] = (conversion[None] @ _to_affine44(pool[rows]))[:, :3, :]
    return palette


# ---------------------------------------------------------------------------
# 提取期定锚
# ---------------------------------------------------------------------------

def find_world_matches(world_a: numpy.ndarray, world_b: numpy.ndarray) -> tuple[list, int]:
    """两个部件在世界空间里一致的骨骼。

    返回 ``(互唯一骨对, 全部命中数)``。一骨对是互唯一的，当且仅当两边都没有第二个
    匹配对象；只是在采集帧里没动的骨骼会同时匹配好几个，不能用。
    """
    rows_a = world_a[:, :3, :]
    rows_b = world_b[:, :3, :]
    difference = numpy.abs(rows_a[:, None] - rows_b[None])
    rotation_error = difference[..., :3].max(axis=(2, 3))
    translation_error = difference[..., 3].max(axis=2)
    magnitude = numpy.maximum(
        numpy.abs(rows_a[:, :, 3]).max(axis=1)[:, None],
        numpy.abs(rows_b[:, :, 3]).max(axis=1)[None],
    )
    tolerance = MATCH_TRANSLATION_TOLERANCE + MATCH_TRANSLATION_RELATIVE_TOLERANCE * magnitude
    nonzero_a = numpy.abs(rows_a).max(axis=(1, 2)) > 0
    nonzero_b = numpy.abs(rows_b).max(axis=(1, 2)) > 0
    matches = (
        (rotation_error < MATCH_ROTATION_TOLERANCE)
        & (translation_error < tolerance)
        & nonzero_a[:, None] & nonzero_b[None]
    )
    row_counts = matches.sum(axis=1)
    column_counts = matches.sum(axis=0)
    unique_pairs = [
        (int(i), int(j)) for i, j in numpy.argwhere(matches)
        if row_counts[i] == 1 and column_counts[j] == 1
    ]
    return unique_pairs, int(matches.sum())


def _affine_of(palette) -> numpy.ndarray:
    array = numpy.asarray(palette, dtype=numpy.float64)
    if array.ndim == 3 and array.shape[1:] == (4, 4):
        return array
    rows = array.reshape(-1, 3, 4)
    affine = numpy.zeros((rows.shape[0], 4, 4), dtype=numpy.float64)
    affine[:, :3, :] = rows
    affine[:, 3, 3] = 1.0
    return affine


def _palette_tag(palette) -> str:
    """骨骼表内容的指纹，用来跳过重复的匹配计算。"""
    rows = numpy.asarray(palette, dtype=numpy.float32)
    digest = hashlib.sha1()
    digest.update(str(rows.shape).encode('ascii'))
    digest.update(numpy.ascontiguousarray(rows).tobytes())
    return digest.hexdigest()


def _row_hashes(rows: numpy.ndarray, decimals: int, weights: numpy.ndarray) -> numpy.ndarray:
    """把每根骨的 3x4 行量化成一个大整数，用来做常数时间的行比对。"""
    with numpy.errstate(over='ignore'):
        quantized = numpy.round(rows, decimals) * 1000.0
        integers = quantized.reshape(-1, 12).astype(numpy.int64)
        hashes = (integers * weights[None, :12]).sum(axis=1)
    return hashes.reshape(rows.shape[:-2])


def _best_conversion(palette_target, palette_source, decimals: int = 3):
    """解出 ``C`` 使 ``target ≈ C @ source``，取命中最多的那个骨对解。

    参考实现靠每个部件的 ``object_matrix``（根骨骼→世界）把骨骼表抬到世界空间再
    比；这份数据里 ``vs-cb1`` 前 4 行是**网格物体**的物体→世界（三个部件的平移
    都在 600 附近、最大差 0.9），不是根骨骼的，所以抬不到同一个世界。这里改成
    把这个矩阵**解出来**：与参考实现的匹配只差「世界原点选在哪」，任意刚体变换
    都不改变「哪两根骨是同一根」的判定。

    返回 ``(C, 命中数)``；``C`` 是 4x4 float64（列向量），解不出时返回 ``None``。
    """
    affine_target = _affine_of(palette_target)
    affine_source = _affine_of(palette_source)
    count_target = affine_target.shape[0]
    count_source = affine_source.shape[0]
    if not count_target or not count_source:
        return None, 0
    inverses = numpy.linalg.inv(affine_source)
    rotation_source = affine_source[:, :3, :3]
    translation_source = affine_source[:, :3, 3]
    weights = numpy.int64(1000003) ** numpy.arange(12, dtype=numpy.int64)
    # 「这一帧没动」的单位矩阵行哪张表里都有一堆，它们精确相等也证明不了同一根骨，
    # 反而会把换算矩阵解成「把单位阵映射到单位阵」的假解。计数时把它们排除。
    valid_target = ~_is_identity_rows(affine_target)
    valid_source = ~_is_identity_rows(affine_source)
    hashes_target = _row_hashes(affine_target[:, :3, :][valid_target], decimals, weights)
    best: tuple[int, int, int] = (0, -1, -1)
    perfect = int(min(valid_target.sum(), valid_source.sum()))
    for index in range(count_target):
        if not valid_target[index]:
            continue
        # 一根骨对整张表解一次换算矩阵，再把「这个矩阵乘到源的全部骨上」一次算完。
        conversions = affine_target[index] @ inverses
        rotations = conversions[:, :3, :3]
        translations = conversions[:, :3, 3]
        rows = numpy.einsum('bij,mjk->bmik', rotations, rotation_source)
        offsets = numpy.einsum('bij,mj->bmi', rotations, translation_source)
        offsets = offsets + translations[:, None, :]
        probe = numpy.concatenate((rows, offsets[:, :, :, None]), axis=3)
        hashes = _row_hashes(probe, decimals, weights)
        matched = numpy.isin(hashes.ravel(), hashes_target).reshape(hashes.shape)
        matched[:, ~valid_source] = False
        hit_counts = matched.sum(axis=1)
        source_index = int(hit_counts.argmax())
        hits = int(hit_counts[source_index])
        if hits > best[0]:
            best = (hits, index, source_index)
        if hits == perfect:
            break
    hits, index, source_index = best
    if hits <= 0:
        return None, 0
    return affine_target[index] @ inverses[source_index], hits


def derive_object_matrices(candidates: list[dict]) -> dict:
    """给每个部件解一个「本部件根骨 → 世界」矩阵，供世界空间匹配使用。

    以骨骼最多的部件为世界原点，沿「能解出常量换算」的关系逐层推导（同一副角色
    的部件之间总有一条链），解不出来的部件返回 ``None``（参考实现遇到这种部件
    也是直接排除）。
    """
    palettes = {
        candidate["ib_hash"]: _affine_of(candidate["palette"])
        for candidate in candidates if len(candidate["palette"])
    }
    if not palettes:
        return {}
    ordered = sorted(
        palettes, key=lambda key: (-palettes[key].shape[0], key)
    )
    matrices = {ordered[0]: numpy.eye(4, dtype=numpy.float64)}
    pending = [key for key in ordered[1:]]
    cache: dict[tuple, tuple] = {}

    def conversion(target: str, source: str):
        key = (target, source)
        if key not in cache:
            cache[key] = _best_conversion(palettes[target], palettes[source])
        return cache[key]

    while pending:
        progressed = False
        for target in list(pending):
            for source, matrix in matrices.items():
                if source == target or matrix is None:
                    continue
                found, _hits = conversion(target, source)
                # 一根骨就能解出「同一个常量」，但只有一根骨重合多半是巧合；至少两对
                # 才当两个部件真有空间关系（真实数据里身体↔头发正好是两对）。
                if found is None or _hits < 2:
                    continue
                # found: palette_target ≈ found @ palette_source
                # 要让两边都落在 source 的世界里：world_target = inv(found) @ palette
                # 而 palette_target = found @ palette_source，world_source = matrix @
                # palette_source ⇒ 两者相消靠 matrix @ inv(found)（右乘，不是左乘）。
                matrices[target] = matrix @ numpy.linalg.inv(found)
                pending.remove(target)
                progressed = True
                break
        if not progressed:
            for target in pending:
                matrices[target] = None
            break
    return matrices


def find_constant_matches(
    palette_a, palette_b, decimals: int = 3
) -> tuple[list, int]:
    """两个部件的骨骼表只差一个常量换算矩阵时的互唯一骨对（保留给测试用）。

    等价于「先把 b 抬到 a 的空间，再做世界空间互唯一筛选」。
    """
    conversion, _hits = _best_conversion(palette_a, palette_b, decimals)
    if conversion is None:
        return [], 0
    affine_a = _affine_of(palette_a)
    converted = numpy.einsum('ij,bjk->bik', conversion, _affine_of(palette_b))
    return find_world_matches(affine_a, converted)


def _count(weighted_counts, local_id: int) -> int:
    if weighted_counts is None or local_id >= len(weighted_counts):
        return 0
    return int(weighted_counts[local_id])


def _is_identity_rows(rows) -> "numpy.ndarray":
    """哪些行是「这一帧没动」的单位矩阵（骨骼表里大量重复的那种）。"""
    data = numpy.asarray(rows, dtype=numpy.float64)
    rotation = data[:, :3, :3]
    translation = data[:, :3, 3]
    flat = rotation.reshape(len(data), -1)
    return (numpy.abs(flat - numpy.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0])).max(axis=1) <= 1e-6) & (
        numpy.abs(translation).max(axis=1) <= 1e-3
    )


def plan_cross_group_palette(candidates: list[dict]) -> tuple[dict, list[str]]:
    """选出角色、参与合并的部件与共用骨架。

    每个 candidate 是：
        ib_hash, (可选) unique_strs / pose_vs / pose_order / vertex_count,
        palette (n, 3, 4) float32,
        object_matrix (4, 4) float64 或 None（列向量）,
        shape_keyed (bool), weighted_counts (n,) int。

    返回 ``(未校验的 profile, warnings)``。返回的部件顺序：先合并的（根在前，
    其余按采集顺序），再是其它。
    """
    warnings: list[str] = []
    usable = [
        candidate for candidate in candidates
        if candidate.get("object_matrix") is not None and len(candidate["palette"])
    ]
    for candidate in candidates:
        if candidate not in usable:
            warnings.append(
                candidate["ib_hash"] + " 读不到骨骼表或物体矩阵，不参与骨骼合并。"
            )
    if not usable:
        raise ZZMICrossGroupPaletteError("帧分析里没有可用于骨骼合并的角色部件。")

    # 参考实现的匹配在世界空间做：``world = object_matrix @ palette``，物体矩阵取
    # 每个部件自己那次绘制绑的 ``vs-cb1``。参考实现里各部件这份矩阵不同，所以世界
    # 空间里能对上的骨就是真共用的骨；本仓库这份数据里 ``vs-cb1`` 首位常量各部件
    # 逐位相同，换算矩阵在两边抵消，世界空间比对退化成骨骼表直接比对 —— 结论等价，
    # 仍然是同一套互唯一判定。
    palette_space = {}
    world_space = {}
    palette_tag = {}
    resolved = []
    for candidate in usable:
        ib_hash = candidate["ib_hash"]
        matrix = numpy.asarray(candidate["object_matrix"], dtype=numpy.float64)
        affine = _affine_of(candidate["palette"])
        palette_space[ib_hash] = affine
        world_space[ib_hash] = numpy.einsum('ij,bjk->bik', matrix, affine)
        palette_tag[ib_hash] = _palette_tag(candidate["palette"])
        resolved.append(candidate)
    usable = resolved
    if not usable:
        raise ZZMICrossGroupPaletteError("帧分析里没有可用于骨骼合并的角色部件。")

    # 同一个骨骼表会在角色身上出现很多次（LOD、重复部件），按骨骼表内容缓存结果，
    # 否则一份 172 骨的模型要重复扫十几遍，导入会明显变卡。
    pair_info = {}
    # 共用骨**本来就已知**：导入期按骨骼矩阵逐位相等做的跨部件去重（`VGMap`）已经
    # 写明「本部件第 L 根骨 = 别的部件第 M 根骨」。参考实现的锚点公式一字不改
    # （`E = P_父[锚点] · inv(P_子[锚点])`），只是「哪两个槽位是同一根骨」这个事实
    # 不再靠世界空间反推 —— 本机各部件 `vs-cb1` 逐位相同，`world = obj @ palette`
    # 永远对不齐，反推结果是 0 条锚点边，于是头发/道具件根本没进生成树。
    for candidate in usable:
        own_ib = candidate["ib_hash"]
        for other_ib, pairs in (candidate.get("anchor_pairs") or {}).items():
            pairs = [(int(a), int(b)) for a, b in pairs]
            if not pairs:
                continue
            if own_ib + "|" + other_ib in pair_info or (own_ib, other_ib) in pair_info:
                continue
            pair_info[(own_ib, other_ib)] = (pairs, len(pairs))
            pair_info[(other_ib, own_ib)] = ([(b, a) for a, b in pairs], len(pairs))
    match_cache: dict[tuple, tuple] = {}

    # 导入期的去重表（按骨骼矩阵逐位相等）已经知道哪些骨是同一根：那是最硬的
    # 共有骨证据，直接当锚点。参考实现的锚点公式、生成树、运行期换算都不变，
    # 只是「谁和谁是同一根骨」这个事实不再靠世界空间反推 —— 本机各部件的
    # `vs-cb1` 逐位相同（同一帧五份 dump 的 sha 一致），抬到世界空间一根也对不上，
    # 而按骨骼表本身判等得到的对应关系是精确的。
    for candidate in usable:
        own_ib = candidate["ib_hash"]
        for other_ib, pairs in (candidate.get("anchor_pairs") or {}).items():
            pairs = [(int(first), int(second)) for first, second in pairs or ()]
            if not pairs or (own_ib, other_ib) in pair_info:
                continue
            pair_info[(own_ib, other_ib)] = (pairs, len(pairs))
            pair_info[(other_ib, own_ib)] = (
                [(second, first) for first, second in pairs],
                len(pairs),
            )

    def matches_for(ib_a: str, ib_b: str) -> tuple[list, int]:
        tag_a = palette_tag[ib_a]
        tag_b = palette_tag[ib_b]
        if tag_a <= tag_b:
            cache_key, first, second, flip = (tag_a, tag_b), ib_a, ib_b, False
        else:
            cache_key, first, second, flip = (tag_b, tag_a), ib_b, ib_a, True
        cached = match_cache.get(cache_key)
        if cached is None:
            # 参考实现的「世界空间互唯一」筛选，输入是抬到同一世界后的骨骼表。
            # 先按行去重：同一部件里会有多条完全相同的行（若干骨共用同一个矩阵），
            # 互唯一过滤会把它们当成「一个骨对上了好几根骨」全部丢掉；去重后判定
            # 的是同一件事（哪两条不同的矩阵落在同一个位置），只是不再因为源表里
            # 的重复行而失配，最后把去重下标映回原始骨号。
            collapsed = []
            rows_first = []
            for ib in (first, second):
                world = numpy.asarray(world_space[ib], dtype=numpy.float64)
                flat = world.reshape(len(world), -1).round(6)
                _, first_index = numpy.unique(flat, axis=0, return_index=True)
                keep = numpy.sort(first_index)
                rows_first.append(keep)
                collapsed.append(world[keep])
            cached = find_world_matches(collapsed[0], collapsed[1])
            cached = (
                [
                    (int(rows_first[0][i]), int(rows_first[1][j]))
                    for i, j in cached[0]
                ],
                cached[1],
                False,
            )
            match_cache[cache_key] = cached
        unique_pairs, all_count, exact = cached
        if flip and unique_pairs:
            unique_pairs = [(j, i) for i, j in unique_pairs]
        return unique_pairs, all_count, exact

    for index_a, candidate_a in enumerate(usable):
        for candidate_b in usable[index_a + 1:]:
            if (candidate_a["ib_hash"], candidate_b["ib_hash"]) in pair_info:
                # 去重表已经给出共有骨（精确），不必再扫世界空间。
                continue
            unique_pairs, all_count, exact = matches_for(
                candidate_a["ib_hash"], candidate_b["ib_hash"]
            )
            # 精确重合（误差 < 1e-4）一根骨就够定换算；容差匹配至少要两对才算数。
            if not unique_pairs or (not exact and len(unique_pairs) < MIN_ANCHOR_PAIRS):
                continue
            if len(unique_pairs) <= MIN_ANCHOR_PAIRS + 1:
                warnings.append(
                    candidate_a["ib_hash"] + " 与 " + candidate_b["ib_hash"]
                    + " 只共用 " + str(len(unique_pairs))
                    + " 根骨，锚点链很弱（骨骼表可能不是同一帧采的），换算只能按这几根骨推。"
                )
            if all_count:
                key = (candidate_a["ib_hash"], candidate_b["ib_hash"])
                pair_info[key] = (unique_pairs, all_count)
                pair_info[(key[1], key[0])] = ([(j, i) for i, j in unique_pairs], all_count)

    rigged = [candidate for candidate in usable if not candidate["shape_keyed"]]
    if not rigged:
        raise ZZMICrossGroupPaletteError("帧分析里只有形态键驱动的部件，没有可合并的骨骼。")
    root = max(
        rigged,
        key=lambda candidate: (
            int(candidate.get("vertex_count", 0)), -int(candidate.get("pose_order", 0))
        ),
    )

    # 从根开始沿互唯一锚点长合并集合，每次取共用骨最多的那条边（最大生成树）。
    by_ib = {candidate["ib_hash"]: candidate for candidate in usable}
    parent_of = {root["ib_hash"]: (root["ib_hash"], None)}
    while True:
        best = None
        for (ib_a, ib_b), (unique_pairs, _) in pair_info.items():
            if ib_a not in parent_of or ib_b in parent_of or not unique_pairs:
                continue
            if ib_a not in by_ib or ib_b not in by_ib:
                continue
            if by_ib[ib_b]["shape_keyed"]:
                continue
            rank = (len(unique_pairs), -int(by_ib[ib_b].get("pose_order", 0)))
            if best is None or rank > best[0]:
                best = (rank, ib_a, ib_b, unique_pairs)
        if best is None:
            break
        _, ib_parent, ib_child, unique_pairs = best
        weights_parent = by_ib[ib_parent].get("weighted_counts")
        weights_child = by_ib[ib_child].get("weighted_counts")
        # 锚点要在两个网格里都真被用到（头、脖子），而不是恰好唯一的一根辅助骨。
        anchor = max(
            unique_pairs,
            key=lambda pair: (
                min(_count(weights_parent, pair[0]), _count(weights_child, pair[1])),
                -pair[0],
            ),
        )
        parent_of[ib_child] = (ib_parent, anchor)

    merged_ibs = set(parent_of)
    attached = []
    for candidate in usable:
        ib_hash = candidate["ib_hash"]
        if ib_hash in merged_ibs:
            continue
        touches_character = any(
            (ib_hash, merged_ib) in pair_info for merged_ib in merged_ibs
        )
        if not touches_character:
            warnings.append(
                ib_hash + " 和角色没有共用的骨骼，判断为画面里的其它物体"
                "（邦布、武器或别的角色），没有提取。"
            )
            continue
        if candidate["shape_keyed"]:
            role, reason = ROLE_SHAPE_KEYED, "形态键驱动（脸部表情）"
        else:
            role, reason = ROLE_STANDALONE, "和合并部件没有一一对应的共用骨骼，换算不了骨骼空间"
        attached.append((candidate, role, reason))

    merged_order = [root] + sorted(
        (by_ib[ib_hash] for ib_hash in merged_ibs if ib_hash != root["ib_hash"]),
        key=lambda candidate: int(candidate.get("pose_order", 0)),
    )
    offsets = {}
    vg_offset = 0
    for candidate in merged_order:
        offsets[candidate["ib_hash"]] = vg_offset
        vg_offset += len(candidate["palette"])
    bones_count = vg_offset
    if bones_count > MAX_POOL_BONES:
        raise ZZMICrossGroupPaletteError(
            "合并后的骨骼数 " + str(bones_count)
            + " 超过游戏蒙皮着色器上限 " + str(MAX_POOL_BONES) + "。"
        )

    # canonical id：合并部件之间互唯一骨对的并集；代表取当帧上传最早的那一份。
    union_parent = list(range(bones_count))

    def find(slot: int) -> int:
        while union_parent[slot] != slot:
            union_parent[slot] = union_parent[union_parent[slot]]
            slot = union_parent[slot]
        return slot

    for (ib_a, ib_b), (unique_pairs, _) in pair_info.items():
        if ib_a not in merged_ibs or ib_b not in merged_ibs or ib_a > ib_b:
            continue
        for i, j in unique_pairs:
            root_a, root_b = find(offsets[ib_a] + i), find(offsets[ib_b] + j)
            if root_a != root_b:
                union_parent[max(root_a, root_b)] = min(root_a, root_b)

    order_of_slot = {}
    for candidate in merged_order:
        for local_id in range(len(candidate["palette"])):
            order_of_slot[offsets[candidate["ib_hash"]] + local_id] = (
                int(candidate.get("pose_order", 0)),
                -_count(candidate.get("weighted_counts"), local_id),
                offsets[candidate["ib_hash"]] + local_id,
            )
    members: dict[int, list[int]] = {}
    for slot in range(bones_count):
        members.setdefault(find(slot), []).append(slot)
    canonical_of = {}
    for slot_group in members.values():
        representative = min(slot_group, key=lambda slot: order_of_slot[slot])
        for slot in slot_group:
            canonical_of[slot] = representative

    components = []
    for candidate in merged_order:
        ib_hash = candidate["ib_hash"]
        parent_ib, anchor = parent_of[ib_hash]
        offset = offsets[ib_hash]
        components.append({
            "ib_hash": ib_hash,
            "role": ROLE_MERGED,
            "reason": "",
            "vg_offset": offset,
            "vg_count": len(candidate["palette"]),
            "vg_map": {
                str(local_id): int(canonical_of[offset + local_id])
                for local_id in range(len(candidate["palette"]))
            },
            "_parent_ib": parent_ib,
            "anchor_parent_slot": (offsets[parent_ib] + anchor[0]) if anchor else 0,
            "anchor_self_slot": (offset + anchor[1]) if anchor else 0,
            **_common_fields(candidate),
        })
    for candidate, role, reason in sorted(
        attached, key=lambda item: int(item[0].get("pose_order", 0))
    ):
        components.append({
            "ib_hash": candidate["ib_hash"], "role": role, "reason": reason,
            **_common_fields(candidate),
        })

    id_by_ib = {component["ib_hash"]: index for index, component in enumerate(components)}
    for index, component in enumerate(components):
        component["component_id"] = index
        parent_ib = component.pop("_parent_ib", None)
        component["parent_component_id"] = id_by_ib[parent_ib] if parent_ib else NO_PARENT

    for component in components:
        if component["role"] == ROLE_SHAPE_KEYED:
            warnings.append(
                component["ib_hash"] + " 是形态键驱动的部件（脸部表情），不参与骨骼合并；"
                "导出时保持游戏原网格，只替换贴图。"
            )
        elif component["role"] == ROLE_STANDALONE:
            warnings.append(
                component["ib_hash"] + " " + component["reason"]
                + "，不参与骨骼合并，按普通方式导出。"
            )

    profile = {
        "format_version": PROFILE_FORMAT_VERSION,
        "mode": PROFILE_MODE,
        "components": components,
    }
    # 骨骼池的宽度：所有部件槽位范围的最大值（与运行期 `array = bones_count * 3`
    # 同口径）。导出侧和运行期都要按这个宽度声明缓冲，不能各算各的。
    pool_bones = max(
        (
            int(component.get("vg_offset", 0)) + int(component.get("vg_count", 0))
            for component in components
        ),
        default=0,
    )
    profile["bones_count"] = pool_bones
    profile["pool_bones"] = pool_bones
    return profile, warnings


def _finite_int(value: object, fallback: int) -> int:
    """把可能带 NaN/inf 的数值收成有限非负整数；收不出来就用 ``fallback``。

    ``int()`` 遇到 NaN 直接 ValueError，而调用方把 ``ValueError`` 当「整池方案
    算不出来」处理 —— 一个部件的脏数据会因此否决整只角色。所有来自部件数据的
    数值都先过这里。
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not numpy.isfinite(number):
        return fallback
    return int(max(0.0, number))


def _common_fields(candidate: dict) -> dict:
    return {
        "pose_vs": candidate.get("pose_vs", ""),
        "pose_order": int(candidate.get("pose_order", 0)),
        "vertex_count": _finite_int(candidate.get("vertex_count", 0), 0),
        "palette_bone_count": int(len(candidate["palette"])),
    }


def plan_from_parts(parts: dict[str, dict]) -> tuple[dict, dict, dict, list[str]]:
    """把导入期手上已有的部件数据直接喂给 :func:`plan_cross_group_palette`。

    ``parts`` 按**采集顺序**排列（Python 字典保序；键是 draw_ib），每项：

        palette       每骨 3×4 或 12 个 float 的骨骼表（``load_palette`` 的产物）
        transform     该部件的物体矩阵（``parse_object_transform`` 的 16 个 float）
        slot_weights  本地骨槽权重（用来挑「两边都真用到」的锚点，可缺）
        vertex_count  顶点数（只用来选根，缺省时退化成权重和）
        shape_keyed   形态键驱动的部件（不参与合并）

    非有限值（NaN/inf）按**损坏数据**处理，不许流进判等与 ``int()``：骨骼表带
    NaN 的部件不参与合并（骨骼矩阵就是它的身份，NaN 既认不出共有骨也无法换算），
    只占池尾私有槽位；权重带 NaN 则该骨计数按 0 算（只影响挑锚点与选根）。
    实测：脸部 1 骨部件 ``dbbf0aee`` 的 Blend 权重含 NaN，旧的
    ``int(round(float(weighted.sum())))`` 直接抛 ValueError，整池方案当场算不出来，
    于是整只角色回退历史分组，看上去就是「带脸部导入就退回非跨组合并」。
    Blend 权重本来就可能是合成/损坏数据（历史分组路径 :2251 早有按 0 记账的
    处理），跨组计划器不该是唯一没有防护的那条路。

    返回 ``(vg_maps, vg_offsets, profile, warnings)``：
    ``vg_maps[draw_ib]`` 是「本地骨号 -> **骨骼池槽位**」，就是导出侧要写进
    SubmeshJson 的 VGMap（运行期按池槽位索引调色板，没有第二层 remap）；
    ``vg_offsets`` 是该部件在骨骼池里的起始槽位。
    """
    candidates = []
    warnings: list[str] = []
    for pose_order, (draw_ib, part) in enumerate(parts.items()):
        palette = part.get("palette")
        transform = part.get("transform")
        if palette is None or transform is None:
            warnings.append(str(draw_ib) + " 缺少骨骼表或物体矩阵，没有参与骨骼合并。")
            continue
        rows = numpy.asarray(palette, dtype=numpy.float32).reshape(-1, 3, 4)
        if not len(rows) or not numpy.isfinite(rows).all():
            # 骨骼矩阵带 NaN/inf：既不能拿去认共有骨，也不能拿来换算，硬塞进池里
            # 会把锚点链污染成 NaN 槽位。让它走池尾私有段。
            warnings.append(
                str(draw_ib) + " 的骨骼表含非有限值（NaN/inf），"
                "没有纳入骨骼合并计算（在骨骼池末尾独占槽位）。"
            )
            continue
        weights = part.get("slot_weights")
        if weights is not None:
            weighted = numpy.asarray(weights, dtype=numpy.float64).reshape(-1)
            if len(weighted) < len(rows):
                weighted = numpy.concatenate(
                    (weighted, numpy.zeros(len(rows) - len(weighted)))
                )
            weighted = weighted[: len(rows)]
            if not numpy.isfinite(weighted).all():
                warnings.append(
                    str(draw_ib) + " 的骨骼权重含非有限值（NaN/inf），"
                    "这些骨的计数按 0 处理（不参与锚点挑选与选根）。"
                )
                weighted = numpy.nan_to_num(weighted, nan=0.0, posinf=0.0, neginf=0.0)
        else:
            weighted = numpy.ones(len(rows), dtype=numpy.float64)
        vertex_count = part.get("vertex_count")
        if vertex_count is None:
            # 没有顶点数时用「权重和」当尺子：越大的网格叠的骨骼影响越多。
            vertex_count = float(weighted.sum())
        vertex_count = _finite_int(vertex_count, 0)
        matrix = numpy.asarray(transform, dtype=numpy.float64).reshape(4, 4)
        # `parse_object_transform` 给的是**行向量式** 4×4（平移在第 3 行、w 在 (3,3)），
        # 而换算链要的是**列向量式**（平移在第 3 列）；判形态后转置。
        if (
            abs(matrix[3, 3] - 1.0) <= 1e-3
            and float(numpy.abs(matrix[:3, 3]).max()) <= 1e-3
            and float(numpy.abs(matrix[3, :3]).max()) > 1e-9
        ):
            matrix = matrix.T
        candidates.append({
            "ib_hash": str(draw_ib),
            "palette": rows,
            "object_matrix": matrix,
            "pose_order": pose_order,
            "vertex_count": int(vertex_count),
            "shape_keyed": bool(part.get("shape_keyed", False)),
            "weighted_counts": weighted,
            # 导入期去重表给出的共有骨对应关系（本部件局部骨号 ↔ 另一部件局部骨号）。
            # 有它就当锚点用，没有才退回参考实现的「世界空间认骨」。
            "anchor_pairs": {
                str(other_ib): [
                    (int(first), int(second)) for first, second in pairs or ()
                ]
                for other_ib, pairs in (part.get("shared") or {}).items()
            },
        })

    profile, plan_warnings = plan_cross_group_palette(candidates)
    warnings.extend(plan_warnings)
    profile = validate_profile(profile)

    vg_maps: dict[str, dict] = {}
    vg_offsets: dict[str, int] = {}
    # 顶点里的骨号必须是**骨骼池槽位**（参考实现的运行期按池槽位索引调色板，没有
    # 第二层 remap）。共用骨只留一个顶点组：取「按 pose 顺序第一个拥有它的部件」
    # 的池槽位当代表槽 —— 任何部件绘制时，那个槽位都会被换算到本部件空间，所以
    # 一份顶点组对所有绘制部件都成立。
    representative: dict[int, int] = {}
    for component in merged_components(profile):
        offset = int(component["vg_offset"])
        for local, canonical in component["vg_map"].items():
            representative.setdefault(
                int(canonical), offset + int(local)
            )
    for component in merged_components(profile):
        draw_ib = component["ib_hash"]
        offset = int(component["vg_offset"])
        vg_maps[draw_ib] = {
            int(local): int(representative.get(int(canonical), offset + int(local)))
            for local, canonical in component["vg_map"].items()
        }
        vg_offsets[draw_ib] = offset

    # 没参与合并的部件（形态键、缺物体矩阵、和角色不共用骨骼）也要有槽位：
    # 在骨骼池末尾给它们各自一段私有空间，本地骨号原样映射 —— 它们的矩阵本来就
    # 在自己空间里，没人要跟它们换算，谁也不影响谁。
    extra_offset = int(profile["bones_count"])
    for draw_ib, part in parts.items():
        if draw_ib in vg_maps:
            continue
        if part.get("palette") is None:
            continue
        rows = numpy.asarray(part["palette"], dtype=numpy.float32).reshape(-1, 3, 4)
        if not len(rows):
            continue
        vg_maps[str(draw_ib)] = {
            local: extra_offset + local for local in range(len(rows))
        }
        vg_offsets[str(draw_ib)] = extra_offset
        extra_offset += len(rows)
    if extra_offset > MAX_POOL_BONES:
        raise ZZMICrossGroupPaletteError(
            "骨骼池 " + str(extra_offset) + " 根超过游戏上限 " + str(MAX_POOL_BONES) + "。"
        )
    profile = dict(profile)
    profile["pool_bones"] = extra_offset
    # 骨骼池必须是**整池宽度**：没参与合并的部件也把骨骼落在池尾那几段里，运行期
    # 骨架缓冲、换算缓冲、remap 都按这个数开（参考实现里也是这样，standalone 部件
    # 的骨骼同样占池位）。只按合并部件的 offset 收尾会让这些槽位落在缓冲之外，
    # 蒙皮直接读到没写过的空间（实测：整块炸开）。
    profile["bones_count"] = extra_offset
    return vg_maps, vg_offsets, profile, warnings

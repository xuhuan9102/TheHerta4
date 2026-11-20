import re
from collections import Counter
from typing import Iterable, Optional

import bpy


OBJECT_SWAP_PREFIX = "swapkey"
SHAPEKEY_PREFIX = "Freq_"
CONTINUOUS_SHAPEKEY_INDEX_PREFIX = "continuous_shapekey_frame"
UV_OFFSET_PREFIX = "uv_offset"
#: 「运行时间」动画驱动节点的帧计数器变量前缀（按节点 auto_index 命名）。
#: 全部运行时间节点都用这一族名字（不再有共享的 ``swapvar`` 兼容别名），
#: 因此不会与历史产物里的 ``$swapvar`` / Velo 桥接的 ``$swapvar_*`` 交叉冲突。
ANIM_DRIVER_FRAME_PREFIX = "anim_frame"

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")

# --- 新增：CJK 字符 ASCII 化 ---
# 常见 CJK 范围：基本区、扩展A、兼容区
_CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
# pypinyin 可用性缓存：None=尚未探测，True/False=探测结果
_PINYIN_AVAILABLE: Optional[bool] = None


def _probe_pinyin_available() -> bool:
    """探测 pypinyin 是否可导入（只看 spec，不真正导入，避免副作用）。"""
    try:
        import importlib.util

        return importlib.util.find_spec("pypinyin") is not None
    except Exception:
        return False


def is_pinyin_available(refresh: bool = False) -> bool:
    """返回 pypinyin 是否可用。

    结果会缓存：命名逻辑与节点 UI 共用同一个缓存，UI 重绘不再每次扫盘。
    ``refresh=True`` 强制重新探测；安装/卸载依赖后请调用 ``reset_pinyin_cache()``。
    """
    global _PINYIN_AVAILABLE
    if refresh or _PINYIN_AVAILABLE is None:
        _PINYIN_AVAILABLE = _probe_pinyin_available()
    return bool(_PINYIN_AVAILABLE)


def reset_pinyin_cache() -> None:
    """清空 pypinyin 探测缓存。

    安装/卸载 pypinyin 之后必须调用：否则同一 Blender 会话会一直沿用旧的探测结果
    （表现为“刚装好 pypinyin，中文形态键变量名仍是 uXXXX，重启才变拼音”）。
    """
    global _PINYIN_AVAILABLE
    _PINYIN_AVAILABLE = None


def cjk_to_ascii(text: str) -> str:
    """把 CJK 字符转成稳定的 ASCII 表示，供 3DMigoto 变量名使用。

    - 优先使用拼音（需安装 pypinyin）；例如 “屁股摇摆” -> “piguyaobai”。
    - 未安装 pypinyin 时回退到 Unicode 码点十六进制（uXXXX），完全可逆；
      例如 “屁股摇摆” -> “u5c41u80a1u6447u6446”。
    - 非 CJK 字符原样保留，因此纯英文名走的是零开销快路径。
    """
    if not text or not _CJK_RE.search(text):
        return text

    lazy_pinyin = None
    if is_pinyin_available():
        try:
            from pypinyin import lazy_pinyin as _lazy_pinyin
        except Exception:
            # 探测到但实际导入失败（残缺/损坏安装）：清缓存供下次重探，本次回落 uXXXX
            reset_pinyin_cache()
        else:
            lazy_pinyin = _lazy_pinyin

    parts: list[str] = []
    for ch in text:
        if not _CJK_RE.match(ch):
            parts.append(ch)
            continue
        if lazy_pinyin is not None:
            py = lazy_pinyin(ch)
            # 若 pypinyin 对该字符无音（返回原文），走十六进制回退
            if py and py[0] and not _CJK_RE.search(py[0]):
                parts.append(py[0])
                continue
        parts.append(f"u{ord(ch):04x}")
    return "".join(parts)


def _get_scene_global_properties(context=None):
    scene = getattr(context, "scene", None) if context is not None else getattr(bpy.context, "scene", None)
    if scene is None:
        return None
    return getattr(scene, "global_properties", None)


def _sanitize_name(text: str, fallback: str = "var") -> str:
    # 先做 CJK -> ASCII，再走原有的空格/非法字符清洗
    safe_text = re.sub(r"\s+", "_", cjk_to_ascii(str(text or "").strip()))
    safe_text = _SAFE_NAME_RE.sub("", safe_text)
    if safe_text and safe_text[0].isdigit():
        safe_text = "_" + safe_text
    return safe_text or fallback


def _split_csv(text: str) -> list[str]:
    return [item for item in str(text or "").split(",") if item]


def _join_csv(values: Iterable[str]) -> str:
    return ",".join(values)


def _iter_blueprint_nodes(tree=None):
    """遍历蓝图节点；``tree`` 给定时只遍历该树（不传则扫全部蓝图树）。"""
    if tree is not None:
        for node in getattr(tree, "nodes", []) or []:
            yield node
        return

    node_groups = getattr(getattr(bpy, "data", None), "node_groups", None)
    if not node_groups:
        return

    for tree in node_groups:
        if getattr(tree, "bl_idname", "") != "SSMTBlueprintTreeType":
            continue
        for node in getattr(tree, "nodes", []):
            yield node


def _iter_node_variable_names(node):
    custom_name = normalize_variable_name(getattr(node, "custom_var_name", "") or "")
    assigned_name = normalize_variable_name(getattr(node, "assigned_variable_name", "") or "")
    if custom_name:
        yield custom_name
    if assigned_name:
        yield assigned_name
    effect_name = normalize_variable_name(getattr(node, "effect_switch_variable", ""))
    if effect_name:
        yield effect_name
    continuous_custom_name = normalize_variable_name(getattr(node, "custom_continuous_index_variable_name", "") or "")
    continuous_assigned_name = normalize_variable_name(getattr(node, "assigned_continuous_index_variable_name", "") or "")
    if continuous_custom_name:
        yield continuous_custom_name
    if continuous_assigned_name:
        yield continuous_assigned_name
    paused_name = normalize_variable_name(getattr(node, "custom_paused_var", "") or "")
    driven_name = normalize_variable_name(getattr(node, "driven_variable", "") or "")
    if paused_name:
        yield paused_name
    if driven_name:
        yield driven_name
    for attr_name in (
        "drag_mode_variable_name",
        "ui_detected_variable_name",
        "ui_zone_variable_name",
    ):
        drag_name = normalize_variable_name(getattr(node, attr_name, "") or "")
        if drag_name:
            yield drag_name
    for item in getattr(node, "driven_variable_list", []) or []:
        variable_name = normalize_variable_name(getattr(item, "variable_name", "") or "")
        if variable_name:
            yield variable_name


def _iter_shapekey_item_variable_names(item):
    custom_name = normalize_variable_name(getattr(item, "custom_variable_name", "") or "")
    assigned_name = normalize_variable_name(getattr(item, "assigned_variable_name", "") or "")
    if custom_name:
        yield custom_name
    if assigned_name:
        yield assigned_name


# ---------------------------------------------------------------------------
# 「所有权」与「引用」的区分（预分配 _1 后缀 bug 的根因修复）
# ---------------------------------------------------------------------------
# 一个变量名只能被一个节点**声明**（owner）。动画驱动节点的「驱动变量」列表
# （driven_variable_list / click_target_list / target_list / condition_list /
# shapekey_items[].variable …）只是**引用**别的节点已经声明的变量，它自己不声明
# 那个名字。
#
# 历史实现把两者混进同一个计数器（_collect_used_variable_name_counts），于是
# 形态键节点做预分配时会把「驱动节点想驱动的那个基名」当成别人已占用 →
# 退到 Freq_xxx_1；而驱动节点保存的仍是基名 → 两边永久错开：导出后驱动写一个
# 没人声明的变量（3DMigoto 视为局部变量 → 跨段失效），形态键着色器读另一个变量，
# 「拖拽/动画驱动 ↔ 形态键变量」整条联动静默死掉。
#
# 正确口径：冲突检测只看 owner；引用名由 build_shape_key_reference_alias_map()
# 在导出时解析成 owner 的权威名。

# 节点上「自己声明」的标量变量字段（owner）。
_OWNER_SCALAR_VARIABLE_FIELDS = (
    "effect_switch_variable",
    "material_switch_var",
    "custom_var_name",
    "assigned_variable_name",
    "custom_continuous_index_variable_name",
    "assigned_continuous_index_variable_name",
    "custom_paused_var",
    "custom_frame_variable_name",
    "assigned_frame_variable_name",
    "accumulator_variable",
    "drag_mode_variable_name",
    "ui_detected_variable_name",
    "ui_zone_variable_name",
)

# 节点上的「引用别人变量」的集合字段（reference）。元素字段名见
# _REFERENCE_ITEM_FIELDS。
_REFERENCE_COLLECTION_FIELDS = (
    "driven_variable_list",
    "click_target_list",
    "pause_target_list",
    "target_list",
    "else_target_list",
    "condition_list",
    "shapekey_items",
    "continuous_shapekey_items",
)

# 引用型集合元素里可能承载变量名的字段。
_REFERENCE_ITEM_FIELDS = ("variable_name", "variable")

# driven_variable 是「自声明」的节点类型（形态键序列驱动把它当自增帧计数器，
# 由该节点自己 global persist 声明）；其余驱动类型的 driven_variable 是迁移前的
# 遗留「目标变量」引用。
_DRIVEN_VARIABLE_IS_OWNED_TYPES = ("SSMTNode_AnimDriver_ShapeKeySequence",)


def _iter_reference_item_variable_names(item):
    for field in _REFERENCE_ITEM_FIELDS:
        raw = getattr(item, field, None)
        if raw is None:
            continue
        name = normalize_variable_name(raw)
        if name:
            yield name


def _iter_node_reference_variable_names(node):
    """产出节点上「引用别人变量」的名字（不含自己声明的）。"""
    bl_idname = str(getattr(node, "bl_idname", "") or "")
    if bl_idname not in _DRIVEN_VARIABLE_IS_OWNED_TYPES:
        legacy = normalize_variable_name(getattr(node, "driven_variable", "") or "")
        if legacy:
            yield legacy
    for field in _REFERENCE_COLLECTION_FIELDS:
        for item in getattr(node, field, None) or []:
            yield from _iter_reference_item_variable_names(item)


def _iter_node_owned_variable_names(node):
    """产出节点上「自己声明」的名字（owner）。"""
    bl_idname = str(getattr(node, "bl_idname", "") or "")
    if bl_idname in _DRIVEN_VARIABLE_IS_OWNED_TYPES:
        owned = normalize_variable_name(getattr(node, "driven_variable", "") or "")
        if owned:
            yield owned
    for field in _OWNER_SCALAR_VARIABLE_FIELDS:
        name = normalize_variable_name(getattr(node, field, "") or "")
        if name:
            yield name


def _collect_owner_variable_name_counts(context=None) -> Counter:
    """只统计「被某个节点声明」的变量名。

    与 _collect_used_variable_name_counts 的差别：不把动画驱动的目标变量引用算进来。
    分配器必须用这份计数判冲突，否则引用名会把 owner 挤到 _N 后缀（见上方注释）。
    """
    counts = Counter()

    for node in _iter_blueprint_nodes() or ():
        bl_idname = getattr(node, "bl_idname", "")

        if bl_idname == "SSMTNode_PostProcess_ShapeKey":
            for item in getattr(node, "shapekey_variable_items", []):
                counts.update(_iter_shapekey_item_variable_names(item))
            continue

        if bl_idname == "SSMTNode_PostProcess_UVOffset":
            for item in getattr(node, "uv_offset_variable_items", []):
                counts.update(_iter_uv_offset_item_variable_names(item))
            continue

        owned_names = tuple(_iter_node_owned_variable_names(node))
        if owned_names:
            counts.update(owned_names)

    return counts


def build_shape_key_reference_alias_map(context=None) -> dict:
    """构建「形态键基名 → 该形态键真正分配的变量名」别名表。

    只有当权威名与基名不同、且基名本身没有被任何 owner 占用时才登记
    （基名被占用说明那是真正的同名去重，不该被别名改写）。
    同一个基名对应多个形态键时视为歧义，不登记（交给导出校验告警）。
    """
    owners = _collect_owner_variable_name_counts(context)
    candidates = {}
    ambiguous = set()

    for node in _iter_blueprint_nodes() or ():
        if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_ShapeKey":
            continue
        for item in getattr(node, "shapekey_variable_items", []) or []:
            shape_key_name = str(getattr(item, "shape_key_name", "") or "").strip()
            if not shape_key_name:
                continue
            actual = normalize_variable_name(getattr(item, "custom_variable_name", "") or "")
            if not actual:
                actual = normalize_variable_name(getattr(item, "assigned_variable_name", "") or "")
            if not actual:
                continue
            base = f"{SHAPEKEY_PREFIX}{_sanitize_name(shape_key_name, fallback='shape')}"
            if base == actual:
                continue
            if base in owners:
                # 基名属于别的 owner：这是合法的同名去重，不做别名改写
                continue
            previous = candidates.get(base)
            if previous is not None and previous != actual:
                ambiguous.add(base)
                continue
            candidates[base] = actual

    for base in ambiguous:
        candidates.pop(base, None)
    return candidates


def resolve_reference_variable_name(name: str, alias_map: Optional[dict] = None) -> str:
    """把一个引用名解析成权威 owner 名；没有别名时原样返回（无 $ 前缀）。"""
    normalized = normalize_variable_name(name)
    if not normalized:
        return ""
    if alias_map is None:
        alias_map = build_shape_key_reference_alias_map()
    return alias_map.get(normalized, normalized)


_REFERENCE_TOKEN_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def rewrite_reference_variables_in_text(text: str, alias_map: Optional[dict] = None) -> str:
    """把文本里所有 ``$引用名`` 改写成权威 owner 名（只在别名命中时改写）。

    用于动画驱动段这类「整体生成、事后无法逐个字段回改」的产物：驱动节点保存的
    可能是旧基名，而形态键节点已经拿了 _N 名，这里在发射前把引用对齐。
    """
    source = str(text or "")
    if not source:
        return source
    if alias_map is None:
        alias_map = build_shape_key_reference_alias_map()
    if not alias_map:
        return source

    def _replace(match):
        token = match.group(1)
        resolved = alias_map.get(token)
        return f"${resolved}" if resolved else match.group(0)

    return _REFERENCE_TOKEN_RE.sub(_replace, source)


def _iter_uv_offset_item_variable_names(item):
    custom_name = normalize_variable_name(getattr(item, "custom_variable_name", "") or "")
    assigned_name = normalize_variable_name(getattr(item, "assigned_variable_name", "") or "")
    if custom_name:
        yield custom_name
    if assigned_name:
        yield assigned_name


def _collect_used_variable_name_counts(context=None) -> Counter:
    counts = Counter()

    for node in _iter_blueprint_nodes() or ():
        bl_idname = getattr(node, "bl_idname", "")
        if bl_idname == "SSMTNode_PostProcess_ShapeKey":
            for item in getattr(node, "shapekey_variable_items", []):
                counts.update(_iter_shapekey_item_variable_names(item))
            continue

        if bl_idname == "SSMTNode_PostProcess_UVOffset":
            for item in getattr(node, "uv_offset_variable_items", []):
                counts.update(_iter_uv_offset_item_variable_names(item))
            continue

        node_variable_names = tuple(_iter_node_variable_names(node))
        if node_variable_names:
            counts.update(node_variable_names)

    _sync_variable_usage_cache(counts, context=context)
    return counts


def _sync_variable_usage_cache(counts: Counter, context=None):
    props = _get_scene_global_properties(context)
    if props is None:
        return

    props.allocated_variable_names_csv = _join_csv(sorted(counts.keys()))

    next_index = 0
    while counts.get(f"{OBJECT_SWAP_PREFIX}{next_index}", 0) > 0:
        next_index += 1
    props.object_swap_variable_counter = next_index


def _normalize_owned_counts(owned_names: Optional[Iterable[str]] = None) -> Counter:
    counts = Counter()
    if not owned_names:
        return counts

    for name in owned_names:
        normalized = normalize_variable_name(name)
        if normalized:
            counts[normalized] += 1
    return counts


def _is_name_used_by_other_owner(name: str, used_counts: Counter, owned_counts: Optional[Counter] = None) -> bool:
    normalized = normalize_variable_name(name)
    if not normalized:
        return False
    owned_count = owned_counts.get(normalized, 0) if owned_counts else 0
    return used_counts.get(normalized, 0) > owned_count


def get_used_variable_names(context=None) -> set[str]:
    return set(_collect_used_variable_name_counts(context).keys())


def mark_variable_name_used(var_name: str, context=None):
    normalized = normalize_variable_name(var_name)
    if not normalized:
        return
    counts = _collect_used_variable_name_counts(context)
    counts[normalized] += 1
    _sync_variable_usage_cache(counts, context=context)


def normalize_variable_name(var_name: str) -> str:
    cleaned = str(var_name or "").strip()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    return _sanitize_name(cleaned, fallback="")


def glow_fx_switch_variable_name(node, base_name):
    """Keep a saved name if free, otherwise allocate an unused Glow FX name."""
    used = set()
    tree = getattr(node, "id_data", None)
    nodes = (*_iter_blueprint_nodes(), *(_iter_blueprint_nodes(tree) if tree is not None else ()))
    for other in nodes:
        if other == node:
            continue
        used.update(name.casefold() for name in _iter_node_owned_variable_names(other))
        for item in getattr(other, "shapekey_variable_items", ()) or ():
            used.update(name.casefold() for name in _iter_shapekey_item_variable_names(item))
        for item in getattr(other, "uv_offset_variable_items", ()) or ():
            used.update(name.casefold() for name in _iter_uv_offset_item_variable_names(item))
        groups = list(getattr(other, "global_switch_groups", ()) or ())
        for item in getattr(other, "target_items", ()) or ():
            groups.extend(getattr(item, "switch_groups", ()) or ())
        for group in groups:
            used.add(normalize_variable_name(getattr(group, "switch_variable", "")).casefold())
        if getattr(other, "bl_idname", "") == "SSMTNode_PostProcess_DiffuseSwitch":
            from .node_postprocess_diffuse_switch import diffuse_group_variable

            used.update(
                diffuse_group_variable(group).casefold()
                for group in other.groups if str(getattr(group, "uid", "") or "").strip()
            )

    current = str(getattr(node, "effect_switch_variable", "") or "").strip().lstrip("$").lower()
    if current and current not in used:
        return f"${current}"
    candidate = base_name
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{base_name}_{suffix}"
        suffix += 1
    return f"${candidate}"


def ensure_object_swap_variable_name(node, context=None) -> str:
    current = normalize_variable_name(getattr(node, "assigned_variable_name", ""))
    if current:
        _collect_used_variable_name_counts(context)
        return current

    used_counts = _collect_used_variable_name_counts(context)
    next_index = 0
    while True:
        candidate = f"{OBJECT_SWAP_PREFIX}{next_index}"
        next_index += 1
        if _is_name_used_by_other_owner(candidate, used_counts):
            continue
        node.assigned_variable_name = candidate
        used_counts[candidate] += 1
        _sync_variable_usage_cache(used_counts, context=context)
        return candidate


def ensure_anim_driver_frame_variable_name(node, context=None) -> str:
    """「运行时间」动画驱动节点的帧计数器变量名（预分配，按 auto_index 命名）。

    为什么要预分配：运行时间节点此前硬编码 ``global persist $swapvar``，N 个节点
    就 N 份同名声明 —— 同值要靠归并、异值要靠分叉（分叉还要段内改名，极易出错）。
    改成每节点一个预分配名后，同名声明从源头消失，归一逻辑对这类重复不再触发。

    名字已存在时直接返回（**不重新分配**）；新分配时避开其它 owner 已占用的名字。
    """
    current = normalize_variable_name(getattr(node, "assigned_frame_variable_name", "") or "")
    if current:
        _collect_used_variable_name_counts(context)
        return current

    used_counts = _collect_owner_variable_name_counts(context)
    base = f"{ANIM_DRIVER_FRAME_PREFIX}{int(getattr(node, 'auto_index', 0) or 0)}"
    candidate = base
    suffix = 1
    while _is_name_used_by_other_owner(candidate, used_counts):
        candidate = f"{base}_{suffix}"
        suffix += 1
    node.assigned_frame_variable_name = candidate
    used_counts[candidate] += 1
    _sync_variable_usage_cache(used_counts, context=context)
    return candidate


def validate_unique_object_swap_variable_names(nodes, context=None) -> None:
    """校验每个物体切换节点最终生效的变量名在本次导出中唯一。"""
    owners = {}
    for node in nodes or ():
        custom_name = normalize_variable_name(getattr(node, "custom_var_name", ""))
        effective_name = custom_name or ensure_object_swap_variable_name(node, context=context)
        if not effective_name:
            raise ValueError(f"物体切换节点“{getattr(node, 'name', '未命名')}”没有有效变量名")

        # 3DMigoto INI 标识符按不区分大小写处理；只比较 custom/assigned
        # 二者中最终生效的一个，避免将同一节点自身误报为重复声明。
        comparison_key = effective_name.casefold()
        previous_node = owners.get(comparison_key)
        if previous_node is not None and previous_node is not node:
            previous_name = getattr(previous_node, "name", "未命名")
            current_name = getattr(node, "name", "未命名")
            raise ValueError(
                f"物体切换变量名 '${effective_name}' 重复："
                f"节点“{previous_name}”与“{current_name}”必须使用不同变量名"
            )
        owners[comparison_key] = node


def allocate_shape_key_variable_name(
    shape_key_name: str,
    *,
    preferred: Optional[str] = None,
    context=None,
    owned_names: Optional[Iterable[str]] = None,
) -> str:
    """给形态键分配导出变量名。

    冲突检测只针对**owner**（_collect_owner_variable_name_counts）：动画驱动
    /点击导出保存的「目标变量」只是引用，绝不能把形态键挤到 _N 后缀——否则
    驱动引用名与形态键实际变量名永久错开，导出后联动整条失效（见模块上方
    「所有权与引用的区分」注释）。
    """
    preferred_normalized = normalize_variable_name(preferred or "")
    used_counts = _collect_owner_variable_name_counts(context)
    owned_counts = _normalize_owned_counts(owned_names)

    if preferred_normalized:
        if not _is_name_used_by_other_owner(preferred_normalized, used_counts, owned_counts):
            return preferred_normalized

    base_name = _sanitize_name(shape_key_name, fallback="shape")
    candidate = f"{SHAPEKEY_PREFIX}{base_name}"
    if not _is_name_used_by_other_owner(candidate, used_counts, owned_counts):
        return candidate

    suffix = 1
    while True:
        indexed = f"{candidate}_{suffix}"
        if not _is_name_used_by_other_owner(indexed, used_counts, owned_counts):
            return indexed
        suffix += 1


def shape_key_base_variable_name(shape_key_name: str) -> str:
    """形态键在**当前命名规则**下的基名（不含冲突后缀）。

    只反映命名函数（``cjk_to_ascii`` / ``_sanitize_name``）的输出，与「有没有被
    别的 owner 占用」无关。预分配刷新靠它区分两件事：

    * 旧名既不是基名、也不是「基名_N」 → 命名规则变了（装上/卸下 pypinyin、
      ``cjk_to_ascii`` 升级），允许按新规则刷新已分配名；
    * 旧名 == 基名 或 「基名_N」 → 命名规则没变，差异只可能来自「基名被别的
      owner 占了」这种冲突去重，**绝不允许**改写已分配名（否则别的蓝图树一增
      一减就会把本树的变量名改掉，驱动/导出引用全部错位）。
    """
    return f"{SHAPEKEY_PREFIX}{_sanitize_name(shape_key_name, fallback='shape')}"


def get_referenced_variable_names(context=None, tree=None) -> set[str]:
    """所有「引用别人变量」的名字（驱动/点击目标/条件等），不含 owner 声明。

    导出期的预分配名自愈（``heal_forked_shape_key_variable_names``）只允许在
    「基名确实还被某处引用」时才把 ``基名_N`` 拉回基名：那种情况是历史 bug
    （引用名挤占 owner）留下的分叉，必须对齐；没有引用时后缀只是历史冲突残留，
    拉回去就等于改写了一个已分配的变量名。

    ``tree`` 给定时只扫该树 —— 自愈必须**按树**判定：别的蓝图树（别的
    workspace）的引用名不该决定本树的变量名，否则变量名又会随别的树一增一减
    而变。跨树的引用对齐由 ``build_shape_key_reference_alias_map`` 在发射前
    完成，不依赖这里的自愈。
    """
    names: set[str] = set()
    for node in _iter_blueprint_nodes(tree):
        names.update(_iter_node_reference_variable_names(node))
    return names


def get_node_variable_name(node, context=None) -> str:
    assigned_name = ensure_object_swap_variable_name(node, context=context)
    manual_name = normalize_variable_name(getattr(node, "custom_var_name", ""))
    resolved = manual_name or assigned_name
    mark_variable_name_used(resolved, context=context)
    return f"${resolved}"


def allocate_continuous_shapekey_index_variable_name(
    *,
    preferred: Optional[str] = None,
    context=None,
    owned_names: Optional[Iterable[str]] = None,
) -> str:
    preferred_normalized = normalize_variable_name(preferred or "")
    used_counts = _collect_used_variable_name_counts(context)
    owned_counts = _normalize_owned_counts(owned_names)

    if preferred_normalized:
        if not _is_name_used_by_other_owner(preferred_normalized, used_counts, owned_counts):
            return preferred_normalized

    suffix = 1
    while True:
        candidate = f"{CONTINUOUS_SHAPEKEY_INDEX_PREFIX}{suffix}"
        if not _is_name_used_by_other_owner(candidate, used_counts, owned_counts):
            return candidate
        suffix += 1


def allocate_uv_offset_variable_name(
    axis: str,
    *,
    preferred: Optional[str] = None,
    context=None,
    owned_names: Optional[Iterable[str]] = None,
) -> str:
    preferred_normalized = normalize_variable_name(preferred or "")
    used_counts = _collect_used_variable_name_counts(context)
    owned_counts = _normalize_owned_counts(owned_names)

    if preferred_normalized:
        if not _is_name_used_by_other_owner(preferred_normalized, used_counts, owned_counts):
            return preferred_normalized

    base_name = _sanitize_name(axis, fallback="axis")
    candidate = f"{UV_OFFSET_PREFIX}_{base_name}"
    if not _is_name_used_by_other_owner(candidate, used_counts, owned_counts):
        return candidate

    suffix = 1
    while True:
        indexed = f"{candidate}_{suffix}"
        if not _is_name_used_by_other_owner(indexed, used_counts, owned_counts):
            return indexed
        suffix += 1

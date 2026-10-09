"""按 SSMT 子网格贴图标记创建贴图材质（规范材质 + 渲染材质）。

导出 mod 时贴图类型完全由工作空间里 SubmeshJson 的 ``TextureMarkUpInfoList``
决定：``common/m_ini_helper.py`` 按 ``mark_type`` 分流、按 ``mark_filename`` 取
文件、按 ``mark_slot`` 写 ``ps-tN``——所以 Blender 物体上有没有材质都不影响导出，
没标记的类型既不会导出贴图也不会写进 ini。

本模块让**导入**方向使用同一份标记：标记了哪种类型就为哪种类型建材质。

## 材质布局（两套并存）

Blender 里每个面由 ``material_index`` 指向**唯一一个**材质槽，一个面不可能同时
用两个材质（EEVEE/Cycles 都是这个模型，没有真正的跨材质）。所以同一面要同时
显示多种贴图，只能让它们待在同一个材质里；而导出侧又要按「材质名首段」判定类型、
沿材质输出递归只取**第一张**图像纹理，多张图挤进一个材质会让导出认不准。
两者无法用同一个材质满足，于是拆成两套：

- **槽 0 —— 渲染材质** ``IMGPV_<网格名>``：各类型贴图接在同一个原理化 BSDF 的
  不同通道上。导入的网格面 ``material_index`` 都是 0，所以**真正被渲染的是它**。
  它的首段 ``imgpv`` 不是任何贴图类型名，导出侧会完全忽略。
- **槽 1..N —— 规范材质** ``<类型名>_<网格名>``：一材质一张图，首段恰好是类型名，
  专供「材质转资源pro」按类型识别（见 ``blueprint/node_postprocess_material.py``
  的 ``_find_workspace_slot_materials``）。它们**一律按「颜色贴图」接法**：不管标记
  是哪种类型，都用 TheHerta4 原生的漫反射图（``ShaderNodeBsdfDiffuse`` + 透明混合，
  与 DiffuseMap 同一套），**绝不经过原理化 BSDF**——原理化的镜面/粗糙度默认值会在
  渲染时改变颜色（法线/光照/遮罩贴图尤其明显），而规范材质本来就只用于按材质名
  识别类型，不需要通道语义。

渲染材质的各通道接法对齐独立插件「快速应用纹理」（MOD 规范）：
  - DiffuseMap  -> Base Color + Alpha
  - NormalMap   -> 与导入侧「自动上贴图时使用法线贴图」同一套按游戏类型接法
                   （IdentityV 反相 G / 标准 / ZZMI·GIMI 由 R/G 重建 Z）
  - LightMap    -> 绝区零（ZZMI）走通道拆分（G->金属度、B->高光）；其它接金属度
  - MaterialMap -> 粗糙度
  - BodyMaskMap -> 自发光强度（黑白遮罩，白色发光）
  - 其它类型    -> 不接

2026-10-09：各通道都按工作文件的那套排版摆进 NodeFrame 分组框
（DiffuseMap / LightMap / MaterialMap / NormalMap / BodyMaskMap），
LightMap、MaterialMap、BodyMaskMap 一律直连对应输入，不再插转换/缩放节点。
"""

import os

import bpy

from .global_config import GlobalConfig
from .global_properties import GlobalProterties
from .logic_name import LogicName
from .submesh_metadata import SubmeshMetadataResolver
from .texture_metadata_helper import TextureMetadataResolver


#: 标记类型（小写）-> 通道接法。
#: 同一个贴图类型在 SSMT 里有多种写法，两种常见命名都要认出来：
#:   4a178546-18468-0-DiffuseMap.dds
#:   LOD0.c209c22b-45087-0.身体_Diffuse.dds
MARK_SOCKET_TYPE_DICT = {
    "diffusemap": "DIFFUSE",
    "diffuse": "DIFFUSE",
    "normalmap": "NORMAL",
    "normal": "NORMAL",
    "lightmap": "LIGHTMAP",
    "light": "LIGHTMAP",
    "hairlightmap": "LIGHTMAP",
    "hairlight": "LIGHTMAP",
    "materialmap": "SURFACE",
    "material": "SURFACE",
    "bodymaskmap": "BODYMASK",
    "bodymask": "BODYMASK",
}

#: 认定「带槽位绑定」的标记类型。与 ``M_IniHelper.is_slot_binding_mark_type`` 同口径：
#: Hash 型标记靠贴图 hash 匹配游戏原有纹理、没有固定文件名与槽位，导入侧不建材质。
SLOT_MARK_TYPES = {"Slot", "SharedSlot"}

#: 渲染材质前缀。首段不是任何贴图类型名，导出侧会忽略它。
#: 与独立插件「快速应用纹理」同前缀（该插件的渲染材质是 ``IMGPV_<物体名>``）。
RENDER_MATERIAL_PREFIX = "IMGPV_"

_ILLEGAL_NAME_CHARS = '\\/:*?"<>|'

#: Blender 数据块名上限 63 字节，留余量避免超长被自动截断后 get() 再也匹配不上。
_MAX_MATERIAL_NAME_BYTES = 60

#: 取图时优先尝试的扩展名顺序（工作空间里可能同时存在原始 .dds 与转换出的 .png）。
_PREFERRED_TEXTURE_EXTENSIONS = (".png", ".dds")

#: 身体发光遮罩接自发光强度时的缩放。2026-10-09 起遮罩改为直连（与工作文件的
#: MOD 规范材质一致），此常量不再参与接线，保留仅为兼容既有测试的引用。
_BODYMASK_EMISSION_SCALE = 0.35

# ---------------------------------------------------------------
# 渲染材质的节点排版（照搬工作文件里那套 MOD 规范材质）
# ---------------------------------------------------------------
#: 渲染材质里原理化 BSDF / 材质输出的坐标
_RENDER_PRINCIPLED = (890, 280)
_RENDER_OUTPUT = (1170, 280)

#: 分组框：通道接法 -> (label, 位置, 尺寸)。尺寸只是初始值，
#: 框开了 shrink，子节点变多/变宽时会自动跟着长。
_RENDER_FRAME = {
    "DIFFUSE": ("DiffuseMap", (0, 936), (300, 337)),
    "LIGHTMAP": ("LightMap", (0, 576), (300, 337)),
    "SURFACE": ("MaterialMap", (0, 196), (300, 337)),
    "NORMAL": ("NormalMap", (0, -184), (700, 337)),
    "BODYMASK": ("BodyMaskMap", (0, -564), (300, 337)),
}

#: 分组框内贴图节点的相对坐标
_RENDER_TEX = (30, -36)

#: 框内并排节点的相对坐标（法线链的第 2、3 个节点）
_RENDER_EXTRA = (350, -36)
_RENDER_EXTRA_2 = (530, -36)


def _find_render_frame(node_tree, socket_type: str):
    """按 label 找已建好的分组框；同 label 的框会被复用（LightMap 的两种接法共用）。"""
    spec = _RENDER_FRAME.get(socket_type)
    if spec is None:
        return None
    for node in node_tree.nodes:
        if node.type == "FRAME" and node.label == spec[0]:
            return node
    return None


def _ensure_render_frame(node_tree, socket_type: str):
    """取得该通道的分组框，没有就按规格新建。"""
    frame = _find_render_frame(node_tree, socket_type)
    if frame is not None:
        return frame
    spec = _RENDER_FRAME.get(socket_type)
    if spec is None:
        return None
    label, location, size = spec
    frame = node_tree.nodes.new("NodeFrame")
    frame.label = label
    frame.location = location
    try:
        frame.width, frame.height = size
    except Exception:
        pass
    try:
        frame.shrink = True
    except Exception:
        pass
    return frame


def _attach_to_frame(node_tree, node, socket_type: str, location=None):
    """把节点放进该通道的分组框并设相对坐标。

    必须先挂 parent 再设 location —— 反着来的话坐标会被当成绝对坐标，
    节点会跳到 ``框位置 + 原坐标`` 上。
    """
    frame = _ensure_render_frame(node_tree, socket_type)
    if frame is None:
        return None
    node.parent = frame
    node.location = _RENDER_TEX if location is None else location
    return frame


def _attach_normal_chain(node_tree, nodes):
    """把法线链的整组节点整体平移到 NormalMap 框内，保持它们彼此的相对位置。

    法线图是按游戏类型搭的（IdentityV 反相 G / 标准 / ZZMI·GIMI 由 R/G 重建 Z），
    节点数与形状都不同，所以不逐个指定坐标，而是取整组包围盒的最小角对齐到框内
    左上角，组内相对关系原样保留。
    """
    created = [node for node in nodes if node is not None]
    if not created:
        return None
    frame = _ensure_render_frame(node_tree, "NORMAL")
    if frame is None:
        return None
    # 目标相对坐标要在挂 parent 之前算好：Blender 设 parent 时会保持节点的视觉
    # 位置、把 location 重解释成相对值，挂完再基于 location 做偏移就会算偏。
    min_x = min(node.location.x for node in created)
    min_y = min(node.location.y for node in created)
    targets = [
        (node,
         node.location.x - min_x + _RENDER_TEX[0],
         node.location.y - min_y + _RENDER_TEX[1])
        for node in created
    ]
    for node, target_x, target_y in targets:
        node.parent = frame
        node.location = (target_x, target_y)
    return frame


def _truncate_name(name: str) -> str:
    encoded = str(name or "").encode("utf-8")
    if len(encoded) <= _MAX_MATERIAL_NAME_BYTES:
        return str(name or "")
    return encoded[:_MAX_MATERIAL_NAME_BYTES].decode("utf-8", "ignore")


def _safe_name_token(text: str) -> str:
    token = str(text or "").strip()
    for ch in _ILLEGAL_NAME_CHARS:
        token = token.replace(ch, "_")
    return token or "Texture"


def mark_material_name(mark_name: str, mesh_name: str) -> str:
    """拼出 MOD 规范材质名：``<类型名>_<网格名>``。

    首段必须**恰好**是类型名，导出侧才认（``material_name.split('_')[0]``）。
    """
    return _truncate_name(f"{_safe_name_token(mark_name)}_{_safe_name_token(mesh_name)}")


def render_material_name(mesh_name: str) -> str:
    """拼出渲染材质名：``IMGPV_<网格名>``（首段不是贴图类型名）。"""
    return _truncate_name(f"{RENDER_MATERIAL_PREFIX}{_safe_name_token(mesh_name)}")


def mark_socket_type(mark_name: str) -> str:
    """标记名 -> 通道接法；未知类型返回空串（只挂图不接）。"""
    return MARK_SOCKET_TYPE_DICT.get(str(mark_name or "").strip().lower(), "")


#: 标记字段在原始 dict（SubmeshJson 大小写形态）与标记对象上的键名。
_MARK_FIELD_KEYS = {
    "mark_name": ("MarkName", "mark_name"),
    "mark_type": ("MarkType", "mark_type"),
    "mark_hash": ("MarkHash", "mark_hash"),
    "mark_slot": ("MarkSlot", "mark_slot"),
    "mark_filename": ("MarkFileName", "mark_filename"),
}


def get_mark_field(mark, field_name: str, default=""):
    """取标记字段，同时兼容原始 dict 与 normalize 后的标记对象。"""
    if isinstance(mark, dict):
        for key in _MARK_FIELD_KEYS.get(field_name, (field_name,)):
            if key in mark:
                return mark[key]
        return default
    return getattr(mark, field_name, default)


def is_slot_mark(mark) -> bool:
    return str(get_mark_field(mark, "mark_type") or "").strip() in SLOT_MARK_TYPES


def unique_str_from_json_path(json_file_path: str) -> str:
    """由 SubmeshJson 路径反推工作空间身份（``LOD0.xxx-1-0`` 或裸身份）。

    与 ``SSMTImportHelper._build_workspace_unique_str_from_json_path`` 同口径：
    ``<LODx>/<子网格>/TYPE_<类型>/<子网格>.json``。
    """
    json_dir = os.path.dirname(str(json_file_path or ""))
    submesh_dir = os.path.basename(os.path.dirname(json_dir))
    lod_dir = os.path.basename(os.path.dirname(os.path.dirname(json_dir)))

    if lod_dir.upper().startswith("LOD") and lod_dir[3:].isdigit():
        return lod_dir + "." + submesh_dir
    return submesh_dir


def read_texture_marks(unique_str: str):
    """读取该子网格的贴图标记，返回 ``(标记列表, 贴图源目录)``。

    标记来自工作空间 SubmeshJson 的 ``TextureMarkUpInfoList``——正是导出 mod 时
    ``M_IniHelper`` 使用的那一份。任何解析失败都返回 ``([], "")``，由调用方回退。
    """
    key = str(unique_str or "").strip()
    if not key:
        return [], ""

    cached = _MARK_CACHE.get((GlobalConfig.path_workspace_folder(), key))
    if cached is not None:
        return cached

    marks = []
    extract_folder = ""
    try:
        metadata = SubmeshMetadataResolver.resolve(key)
        # SubmeshMetadata.texture_markup_info_list 里是 SubmeshJson 的原始 dict
        # （大写键 MarkName/MarkType/MarkFileName），导出侧同样先过一次 normalize
        # 才开始用——这里保持同一处理链，否则拿到的是 dict 而不是标记对象。
        marks = TextureMetadataResolver.normalize_texture_markup_info_list(
            list(getattr(metadata, "texture_markup_info_list", []) or [])
        )
        marks = TextureMetadataResolver._dedupe_texture_markup_info_list(marks)
        extract_folder = str(getattr(metadata, "extract_gametype_folder_path", "") or "")
    except Exception as ex:
        print("[贴图标记] 读取失败，回退文件名搜索: " + key + "，原因: " + str(ex))
        marks, extract_folder = [], ""

    result = (marks, extract_folder)
    _MARK_CACHE[(GlobalConfig.path_workspace_folder(), key)] = result
    return result


#: 导入批次内的标记读取缓存（同一身份只解析一次 SubmeshJson）。
_MARK_CACHE = {}


def clear_mark_cache() -> None:
    """工作空间切换 / 重新导入后清空缓存，避免旧工作空间的标记继续生效。"""
    _MARK_CACHE.clear()


def resolve_mark_texture_path(extract_folder: str, mark) -> str:
    """按标记解析贴图文件路径。

    ``mark_filename`` 指向工作空间里的原始文件（通常是 ``.dds``）。若同目录已经存在
    同名 ``.png``（「导入后自动转PNG」的产物、或用户手工编辑过的无损版本），优先用
    png——这样重复导入不会再让 Blender 去解压排不掉的 BC7/BC6H DDS，也保留用户的编辑。
    """
    filename = str(get_mark_field(mark, "mark_filename") or "").strip()
    if not filename:
        return ""

    stem, extension = os.path.splitext(filename)
    candidates = []
    for preferred in _PREFERRED_TEXTURE_EXTENSIONS:
        if preferred != extension.lower():
            candidates.append(stem + preferred)
    candidates.append(filename)

    for candidate in candidates:
        candidate_path = os.path.join(str(extract_folder or ""), candidate)
        if os.path.isfile(candidate_path):
            return candidate_path
    return ""


def _ignore_texture_alpha() -> bool:
    """「导入贴图时忽略透明度通道」开关（轻量宿主缺访问器时按关处理）。"""
    try:
        return bool(GlobalProterties.ignore_texture_alpha())
    except Exception:
        return False


# ---------------------------------------------------------------
# 节点基础
# ---------------------------------------------------------------
def _clear_nodes(node_tree) -> None:
    for node in list(node_tree.nodes):
        try:
            node_tree.nodes.remove(node)
        except Exception:
            continue


def _make_principled_material(material):
    """把材质清成「Principled + 输出」的最小结构，返回 (节点树, 原理化BSDF)。"""
    material.use_nodes = True
    node_tree = material.node_tree
    _clear_nodes(node_tree)

    principled = node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = _RENDER_PRINCIPLED
    output = node_tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = _RENDER_OUTPUT
    node_tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    return node_tree, principled


def _new_texture_node(node_tree, image, location):
    tex_image = node_tree.nodes.new("ShaderNodeTexImage")
    tex_image.image = image
    tex_image.location = location
    return tex_image


def _link_texture_image(texture_path: str, colorspace: str):
    image = bpy.data.images.load(texture_path)
    try:
        if image.colorspace_settings.name != colorspace:
            image.colorspace_settings.name = colorspace
    except Exception as ex:
        print("[贴图标记] 设置色彩空间失败 " + colorspace + ": " + str(ex))
    return image


# ---------------------------------------------------------------
# 通道接线（都只往已建好的节点树上加，可在同一材质里累加）
# ---------------------------------------------------------------
_SRGB_GROUP = "sRGB->Non-Color"
_NORMAL_GROUP_Z = "法线贴图_补Z"

#: 法线组里补 Z 那两个节点的标记，便于识别与清理（与工作文件同名）
_ZREBUILD_LABEL = "IMGPV_ZREBUILD"

#: sRGB 编解码用到的常量（与工作文件里的取值逐位一致）
_SRGB_POWER = 1.0 / 2.4        # 0.416667
_SRGB_MUL_HIGH = 1.055
_SRGB_SUB_HIGH = 0.055
_SRGB_MUL_LOW = 12.92
_SRGB_THRESHOLD = 0.003131


def _build_srgb_group():
    """用节点搭出「sRGB->Non-Color」组：把 sRGB 读数还原回 Non-Color 数值。

    逐通道做 ``x <= t ? 12.92·x : 1.055·x^(1/2.4) − 0.055``，合并成
    ``a + cond·(b − a)`` 的形式，与工作文件里那个组的数值结果逐位一致。
    """
    group = bpy.data.node_groups.new(_SRGB_GROUP, "ShaderNodeTree")
    group.interface.new_socket("Color", in_out="INPUT", socket_type="NodeSocketColor")
    group.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")

    nodes = group.nodes
    links = group.links

    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-400, 0)
    separate = nodes.new("ShaderNodeSeparateColor")
    separate.location = (-200, 0)
    combine = nodes.new("ShaderNodeCombineColor")
    combine.location = (1100, 0)
    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (1300, 0)
    links.new(group_in.outputs[0], separate.inputs["Color"])
    links.new(combine.outputs[0], group_out.inputs[0])

    for index in range(3):
        column = -140 - index * 380

        power = nodes.new("ShaderNodeMath")
        power.operation = "POWER"
        power.location = (column, 120)
        power.inputs[1].default_value = _SRGB_POWER

        mul_high = nodes.new("ShaderNodeMath")
        mul_high.operation = "MULTIPLY"
        mul_high.location = (column + 180, 120)
        mul_high.inputs[1].default_value = _SRGB_MUL_HIGH

        sub_high = nodes.new("ShaderNodeMath")
        sub_high.operation = "SUBTRACT"
        sub_high.location = (column + 360, 120)
        sub_high.inputs[1].default_value = _SRGB_SUB_HIGH

        mul_low = nodes.new("ShaderNodeMath")
        mul_low.operation = "MULTIPLY"
        mul_low.location = (column, -80)
        mul_low.inputs[1].default_value = _SRGB_MUL_LOW

        sub_delta = nodes.new("ShaderNodeMath")
        sub_delta.operation = "SUBTRACT"
        sub_delta.location = (column + 540, 20)

        greater = nodes.new("ShaderNodeMath")
        greater.operation = "GREATER_THAN"
        greater.location = (column, -280)
        greater.inputs[1].default_value = _SRGB_THRESHOLD

        mul_cond = nodes.new("ShaderNodeMath")
        mul_cond.operation = "MULTIPLY"
        mul_cond.location = (column + 720, 20)

        add = nodes.new("ShaderNodeMath")
        add.operation = "ADD"
        add.location = (column + 900, -80)

        source = separate.outputs[index]
        links.new(source, power.inputs[0])
        links.new(power.outputs[0], mul_high.inputs[0])
        links.new(mul_high.outputs[0], sub_high.inputs[0])
        links.new(source, mul_low.inputs[0])
        links.new(sub_high.outputs[0], sub_delta.inputs[0])
        links.new(mul_low.outputs[0], sub_delta.inputs[1])
        links.new(source, greater.inputs[0])
        links.new(greater.outputs[0], mul_cond.inputs[0])
        links.new(sub_delta.outputs[0], mul_cond.inputs[1])
        links.new(mul_low.outputs[0], add.inputs[0])
        links.new(mul_cond.outputs[0], add.inputs[1])
        links.new(add.outputs[0], combine.inputs[index])

    return group


def _build_normal_group_z():
    """用节点搭出「法线贴图_补Z」组：R 直通、G 反相、B 固定 1，再过法线贴图节点。"""
    group = bpy.data.node_groups.new(_NORMAL_GROUP_Z, "ShaderNodeTree")
    group.interface.new_socket("Color", in_out="INPUT", socket_type="NodeSocketColor")
    group.interface.new_socket("Normal", in_out="OUTPUT", socket_type="NodeSocketVector")

    nodes = group.nodes
    links = group.links

    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-400, 0)
    separate = nodes.new("ShaderNodeSeparateColor")
    separate.location = (-200, 0)
    invert = nodes.new("ShaderNodeMath")
    invert.operation = "SUBTRACT"
    invert.label = _ZREBUILD_LABEL
    invert.location = (0, -160)
    invert.inputs[0].default_value = 1.0
    combine = nodes.new("ShaderNodeCombineColor")
    combine.label = _ZREBUILD_LABEL
    combine.location = (200, 0)
    combine.inputs["Blue"].default_value = 1.0
    normal_map = nodes.new("ShaderNodeNormalMap")
    normal_map.location = (400, 0)
    normal_map.uv_map = ""
    normal_map.inputs["Strength"].default_value = 1.0
    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (600, 0)

    links.new(group_in.outputs[0], separate.inputs["Color"])
    links.new(separate.outputs[0], combine.inputs["Red"])       # R 直通
    links.new(separate.outputs[1], invert.inputs[1])            # 1 − G
    links.new(invert.outputs[0], combine.inputs["Green"])
    links.new(combine.outputs[0], normal_map.inputs["Color"])   # B 用常量 1，不连线
    links.new(normal_map.outputs["Normal"], group_out.inputs[0])
    return group


#: 组名 -> 构造函数。两个组都由本模块用节点现搭，不依赖任何外部资产文件。
_GROUP_BUILDERS = {
    _SRGB_GROUP: _build_srgb_group,
    _NORMAL_GROUP_Z: _build_normal_group_z,
}


def _ensure_node_group(name: str):
    """取得节点组：当前 .blend 里已有同名组就复用，否则用节点现搭一个。"""
    group = bpy.data.node_groups.get(name)
    if group is not None:
        return group
    builder = _GROUP_BUILDERS.get(name)
    if builder is None:
        return None
    try:
        return builder()
    except Exception as ex:
        print("[贴图标记] 建立节点组 " + name + " 失败: " + str(ex))
        return None


def _ensure_normal_group_z():
    """取得「法线贴图_补Z」组（没有就地搭一个）。"""
    return _ensure_node_group(_NORMAL_GROUP_Z)


def _wire_normal(node_tree, principled, texture_path: str, logic_name: str) -> None:
    """法线：贴图 → 「sRGB->Non-Color」→ 「法线贴图_补Z」→ Normal。

    2026-10-09 起与工作文件里的 MOD 规范材质完全统一：不再按游戏类型分流，
    统一走「sRGB 读 + sRGB->Non-Color 还原组 + 补 Z 法线组」这一条链。
    """
    image = _link_texture_image(texture_path, "sRGB")

    target = principled.inputs.get("Normal")
    if target is None:
        return
    for link in list(target.links):
        node_tree.links.remove(link)

    tex_image = _new_texture_node(node_tree, image, _RENDER_TEX)
    _attach_to_frame(node_tree, tex_image, "NORMAL")

    srgb_group = _ensure_node_group(_SRGB_GROUP)
    normal_group = _ensure_normal_group_z()

    source = tex_image.outputs["Color"]
    if srgb_group is None or normal_group is None:
        # 拿不到节点组资产时退回直连，至少不把材质留成断的
        node_tree.links.new(source, target)
        return

    srgb_node = node_tree.nodes.new("ShaderNodeGroup")
    srgb_node.node_tree = srgb_group
    srgb_node.label = srgb_group.name
    _attach_to_frame(node_tree, srgb_node, "NORMAL", _RENDER_EXTRA)

    normal_node = node_tree.nodes.new("ShaderNodeGroup")
    normal_node.node_tree = normal_group
    normal_node.label = normal_group.name
    _attach_to_frame(node_tree, normal_node, "NORMAL", _RENDER_EXTRA_2)

    node_tree.links.new(source, srgb_node.inputs["Color"])
    node_tree.links.new(srgb_node.outputs["Color"], normal_node.inputs["Color"])
    node_tree.links.new(normal_node.outputs["Normal"], target)


def _wire_lightmap(node_tree, principled, texture_path: str) -> None:
    """光照贴图：贴图 → Specular IOR Level（直连，不过转换组）。

    2026-10-09 实机确认：这张图的语义是折射相关，既不是自发光也不是金属度，
    与工作文件里的 MOD 规范材质一致，直接接「高光 IOR 级别」。
    绝区零也不再走 G/B 通道拆分——那套拆法已删除。
    """
    image = _link_texture_image(texture_path, "sRGB")

    target = principled.inputs.get("Specular IOR Level")
    if target is None:
        target = principled.inputs.get("Specular")
    if target is None:
        return

    # 旧版把这张图接在自发光 / 金属度上，换接法时一并断开，避免残留
    for key in ("Emission Color", "Emission", "Metallic"):
        socket = principled.inputs.get(key)
        if socket is None:
            continue
        for link in list(socket.links):
            node_tree.links.remove(link)

    tex_image = _new_texture_node(node_tree, image, _RENDER_TEX)
    _attach_to_frame(node_tree, tex_image, "LIGHTMAP")
    node_tree.links.new(tex_image.outputs["Color"], target)


def _wire_surface(node_tree, principled, texture_path: str) -> None:
    """质感图（MaterialMap）：粗糙度，贴图数值直连、不过转换组。"""
    image = _link_texture_image(texture_path, "sRGB")
    tex_image = _new_texture_node(node_tree, image, _RENDER_TEX)
    _attach_to_frame(node_tree, tex_image, "SURFACE")
    node_tree.links.new(tex_image.outputs["Color"], principled.inputs["Roughness"])


def _wire_bodymask(node_tree, principled, texture_path: str) -> None:
    """身体发光遮罩（BodyMaskMap）：直接接到自发光强度，中间不过缩放节点。

    黑白遮罩，白色=该处发光、黑色=不发光，数值原样进 Emission Strength。
    2026-10-09 起与工作文件的 MOD 规范材质统一：旧版在中间插了一个 ×0.35 的
    Math 节点，换成直连后不再需要。
    """
    image = _link_texture_image(texture_path, "sRGB")
    tex_image = _new_texture_node(node_tree, image, _RENDER_TEX)

    strength = principled.inputs.get("Emission Strength")
    if strength is None:
        return

    _attach_to_frame(node_tree, tex_image, "BODYMASK")
    node_tree.links.new(tex_image.outputs["Color"], strength)


def _wire_channel(node_tree, principled, socket_type: str, texture_path: str, logic_name: str) -> None:
    """把一张贴图接到已建好的原理化 BSDF 的对应通道上。"""
    if socket_type == "NORMAL":
        _wire_normal(node_tree, principled, texture_path, logic_name)
    elif socket_type == "LIGHTMAP":
        _wire_lightmap(node_tree, principled, texture_path)
    elif socket_type == "SURFACE":
        _wire_surface(node_tree, principled, texture_path)
    elif socket_type == "BODYMASK":
        _wire_bodymask(node_tree, principled, texture_path)


def _wire_render_diffuse(node_tree, principled, texture_path: str) -> None:
    """渲染材质的漫反射：原理化 BSDF 的 Base Color（+ 透明度）。

    和规范材质不同——规范材质的 DiffuseMap 单独占一个材质，可以继续用原来的
    漫反射 BSDF + 透明混合；渲染材质要跟金属度/粗糙度/法线共存，只能用原理化 BSDF。
    透明度沿用导入现有语义：受「导入贴图时忽略透明度通道」约束，忽略时不连 Alpha。
    """
    image = _link_texture_image(texture_path, "sRGB")
    tex_image = _new_texture_node(node_tree, image, _RENDER_TEX)
    _attach_to_frame(node_tree, tex_image, "DIFFUSE")
    node_tree.links.new(tex_image.outputs["Color"], principled.inputs["Base Color"])

    # 与工作文件里的 MOD 规范材质一致：漫反射只进 Base Color，Alpha 保持 1.0
    # （「导入贴图时忽略透明度通道」的旧语义随之一并移除）。
    image.alpha_mode = "NONE"


# ---------------------------------------------------------------
# 材质构建
# ---------------------------------------------------------------
#: 规范材质里各节点的坐标（照搬工作文件的排版）
_SPEC_PRINCIPLED = {
    "DIFFUSE": (180, 0), "NORMAL": (0, -160), "LIGHTMAP": (40, 0),
    "SURFACE": (120, 0), "BODYMASK": (240, 0),
}
_SPEC_OUTPUT = {
    "DIFFUSE": (480, 0), "NORMAL": (280, -160), "LIGHTMAP": (320, 0),
    "SURFACE": (420, 0), "BODYMASK": (540, 0),
}
_SPEC_TEX = {
    "DIFFUSE": (-120, 0), "NORMAL": (-820, -160), "LIGHTMAP": (-260, 0),
    "SURFACE": (-160, 0), "BODYMASK": (-120, 0),
}
_SPEC_SRGB = {"NORMAL": (-480, -160)}
_SPEC_NMAP = {"NORMAL": (-240, -160)}


def _wire_spec_channel(node_tree, principled, socket_type: str, texture_path: str) -> None:
    """规范材质：一种贴图接一个原理化通道，只有法线需要转换组。"""
    if socket_type == "NORMAL":
        image = _link_texture_image(texture_path, "sRGB")
        tex_image = _new_texture_node(node_tree, image, _SPEC_TEX["NORMAL"])
        target = principled.inputs.get("Normal")
        if target is None:
            return
        srgb_group = _ensure_node_group(_SRGB_GROUP)
        normal_group = _ensure_normal_group_z()
        if srgb_group is None or normal_group is None:
            node_tree.links.new(tex_image.outputs["Color"], target)
            return
        srgb_node = node_tree.nodes.new("ShaderNodeGroup")
        srgb_node.node_tree = srgb_group
        srgb_node.label = srgb_group.name
        srgb_node.location = _SPEC_SRGB["NORMAL"]
        normal_node = node_tree.nodes.new("ShaderNodeGroup")
        normal_node.node_tree = normal_group
        normal_node.label = normal_group.name
        normal_node.location = _SPEC_NMAP["NORMAL"]
        node_tree.links.new(tex_image.outputs["Color"], srgb_node.inputs["Color"])
        node_tree.links.new(srgb_node.outputs["Color"], normal_node.inputs["Color"])
        node_tree.links.new(normal_node.outputs["Normal"], target)
        return

    image = _link_texture_image(texture_path, "sRGB")
    image.alpha_mode = "NONE"
    tex_image = _new_texture_node(node_tree, image, _SPEC_TEX.get(socket_type, (-120, 0)))

    if socket_type == "DIFFUSE":
        target = principled.inputs.get("Base Color")
    elif socket_type == "LIGHTMAP":
        target = principled.inputs.get("Specular IOR Level")
        if target is None:
            target = principled.inputs.get("Specular")
    elif socket_type == "SURFACE":
        target = principled.inputs.get("Roughness")
    elif socket_type == "BODYMASK":
        target = principled.inputs.get("Emission Strength")
    else:
        target = None
    if target is not None:
        node_tree.links.new(tex_image.outputs["Color"], target)


def _apply_spec_material(material, socket_type: str, texture_path: str,
                         logic_name: str) -> None:
    """规范材质：原理化 BSDF + 单通道，与工作文件里的 MOD 规范材质完全一致。

    规范材质（``<类型名>_<网格名>``）本来只用于让导出侧按材质名识别贴图类型，
    但它同样会被直接渲染，所以按类型接通道——与渲染材质（见
    :func:`_build_render_material`）共用同一套语义与坐标。
    """
    material.use_nodes = True
    node_tree = material.node_tree
    _clear_nodes(node_tree)

    principled = node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = _SPEC_PRINCIPLED.get(socket_type, (0, 0))
    output = node_tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = _SPEC_OUTPUT.get(socket_type, (300, 0))
    node_tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    material.blend_method = "HASHED"
    if hasattr(material, "use_transparency_overlap"):
        material.use_transparency_overlap = True

    _wire_spec_channel(node_tree, principled, socket_type, texture_path)


def _build_render_material(material, entries, logic_name: str) -> None:
    """渲染材质：把各类型贴图接在同一个原理化 BSDF 上。

    ``entries`` 是 ``[(通道接法, 贴图路径), ...]``。
    """
    node_tree, principled = _make_principled_material(material)

    has_diffuse = False
    for socket_type, texture_path in entries:
        if socket_type == "DIFFUSE":
            _wire_render_diffuse(node_tree, principled, texture_path)
            has_diffuse = True
        elif socket_type:
            _wire_channel(node_tree, principled, socket_type, texture_path, logic_name)

    # 与工作文件里的 MOD 规范材质一致：HASHED + 允许透明重叠，Alpha 不接线。
    material.blend_method = "HASHED"
    if hasattr(material, "use_transparency_overlap"):
        material.use_transparency_overlap = True


# ---------------------------------------------------------------
# 材质槽分配
# ---------------------------------------------------------------
def _assign_material_slots(
    obj,
    render_material,
    spec_materials,
    keep_leftovers: bool = False,
) -> None:
    """渲染材质放槽 0（导入的网格面 material_index 都是 0），规范材质依次排后。

    ``keep_leftovers``：物体原有、但不属于本次布局的材质是否追加到末尾保留。
    给旧物体补齐时必须保留（不能弄丢用户手工建的材质）；导入新物体时不用，那些
    槽本来就该是本次建出来的。

    用「替换槽位内容」而不是先清空再追加，避免材质槽索引前移导致面的材质索引错位。
    """
    data = getattr(obj, "data", None)
    if data is None or not hasattr(data, "materials"):
        return

    ordered = []
    seen = set()
    for material in ([render_material] if render_material is not None else []) + list(
        spec_materials
    ):
        if material is None or id(material) in seen:
            continue
        seen.add(id(material))
        ordered.append(material)

    if keep_leftovers:
        for material in list(data.materials):
            if material is None or id(material) in seen:
                continue
            seen.add(id(material))
            ordered.append(material)

    for index, material in enumerate(ordered):
        if index < len(data.materials):
            data.materials[index] = material
        else:
            data.materials.append(material)


def build_marked_materials(
    obj,
    mesh_name: str,
    directory: str,
    logic_name: str | None = None,
) -> int:
    """按该物体的 SSMT 贴图标记建立全部类型的贴图材质（渲染材质 + 规范材质）。

    ``directory`` 是该部件 SubmeshJson 所在的 ``TYPE_<类型>`` 目录，身份
    （``LOD0.xxx-1-0``）由它反推——与导入时写入 ``3DMigoto:WorkspaceUniqueStr``
    的口径同源。

    返回成功建立的**规范材质**数量；返回 0 表示「没有可用标记」，调用方应回退到
    原有的按文件名搜索逻辑（旧工作空间 / 没有标记的部件保持原行为不变）。
    """
    if obj is None:
        return 0

    if logic_name is None:
        logic_name = GlobalConfig.logic_name

    json_file_path = os.path.join(str(directory or ""), str(mesh_name or "") + ".json")
    unique_str = unique_str_from_json_path(json_file_path)
    if not unique_str:
        return 0

    marks, extract_folder = read_texture_marks(unique_str)
    if not marks or not extract_folder:
        return 0

    entries = []
    spec_materials = []
    failures = []
    for mark in marks:
        if not is_slot_mark(mark):
            continue

        mark_name = str(get_mark_field(mark, "mark_name") or "").strip()
        if not mark_name:
            continue

        texture_path = resolve_mark_texture_path(extract_folder, mark)
        if not texture_path:
            failures.append(
                f"{mark_name}({get_mark_field(mark, 'mark_filename')})"
            )
            continue

        socket_type = mark_socket_type(mark_name)
        material = bpy.data.materials.new(name=mark_material_name(mark_name, mesh_name))
        try:
            _apply_spec_material(material, socket_type, texture_path, logic_name)
        except Exception as ex:
            print("[贴图标记] 建立材质失败 " + mark_name + ": " + str(ex))
            failures.append(mark_name)
            try:
                bpy.data.materials.remove(material)
            except Exception:
                pass
            continue

        entries.append((socket_type, texture_path))
        spec_materials.append(material)

    if not spec_materials:
        if failures:
            print("[贴图标记] " + unique_str + " 标记的贴图文件均不可用: " + "，".join(failures))
        return 0

    render_material = None
    try:
        render_material = bpy.data.materials.new(name=render_material_name(mesh_name))
        _build_render_material(render_material, entries, logic_name)
    except Exception as ex:
        print("[贴图标记] 建立渲染材质失败，仅保留规范材质: " + str(ex))
        if render_material is not None:
            try:
                bpy.data.materials.remove(render_material)
            except Exception:
                pass
        render_material = None

    _assign_material_slots(obj, render_material, spec_materials)
    print(
        "[贴图标记] "
        + str(mesh_name)
        + " 按标记建立材质 "
        + str(len(spec_materials))
        + " 个（渲染材质: "
        + (render_material.name if render_material is not None else "无")
        + "）: "
        + "，".join(material.name for material in spec_materials)
    )
    if failures:
        print("[贴图标记] " + str(mesh_name) + " 跳过的标记: " + "，".join(failures))
    return len(spec_materials)


# ---------------------------------------------------------------
# 给已有物体补齐材质
# ---------------------------------------------------------------
#: 导入时写在物体上的工作空间身份（``ssmt_import_helper`` 写入）。旧物体靠它
#: 直接查 SSMT 标记，不需要再按目录反推。
WORKSPACE_UNIQUE_STR_PROP = "3DMigoto:WorkspaceUniqueStr"


def bare_name_from_unique_str(unique_str: str) -> str:
    """去掉 ``LODx.`` 前缀，得到与导入时 ``mesh_name`` 同口径的裸身份。"""
    text = str(unique_str or "").strip()
    if text.upper().startswith("LOD") and "." in text:
        head, _, rest = text.partition(".")
        if head[3:].isdigit() and rest:
            return rest
    return text


def object_workspace_unique_str(obj) -> str:
    """取物体的工作空间身份：优先导入时写入的自定义属性，其次退回网格名/物体名。"""
    try:
        raw = str(obj.get(WORKSPACE_UNIQUE_STR_PROP, "") or "").strip()
    except Exception:
        raw = ""
    if raw:
        return raw

    for candidate in (
        getattr(getattr(obj, "data", None), "name", ""),
        getattr(obj, "name", ""),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _existing_material_by_name(obj, name: str):
    """在物体**自己的材质槽**里按精确名字找材质。

    只在同一物体内复用：``mesh_name`` 不含 LOD 前缀，跨 LOD 的同身份部件材质名
    会完全相同，用 ``bpy.data.materials.get`` 复用会让两个物体共享同一个材质、
    指向同一张贴图。
    """
    try:
        slots = list(obj.material_slots)
    except Exception:
        return None
    for slot in slots:
        material = slot.material
        if material is not None and str(material.name) == name:
            return material
    return None


def _existing_material_by_type(obj, mark_name: str):
    """在物体**自己的材质槽**里按「类型前缀」找同类型材质。

    用户 2026-10-07 反馈：物体上已经有对应类型的材质时，补齐仍会重复添加。
    原因是补齐期的网格名与导入期口径可能不同（例如物体/网格被复制后 mesh
    数据名带上 ``.001`` 后缀），按全名比对找不到旧材质，于是又建一份。
    这里退一步按 ``<类型名>_`` 前缀匹配——用户语义是「物体本身已经有对应的
    材质了就不再添加」，仍严格限制在同一物体内，不会跨物体共享材质。
    """
    prefix = _safe_name_token(mark_name) + "_"
    try:
        slots = list(obj.material_slots)
    except Exception:
        return None
    for slot in slots:
        material = getattr(slot, "material", None)
        if material is None:
            continue
        name = str(getattr(material, "name", "") or "")
        if name == prefix or name.startswith(prefix):
            return material
    return None


def _existing_render_material(obj):
    """在物体**自己的材质槽**里找已有的渲染材质（``IMGPV_`` 前缀）。"""
    try:
        slots = list(obj.material_slots)
    except Exception:
        return None
    for slot in slots:
        material = getattr(slot, "material", None)
        if material is None:
            continue
        if str(getattr(material, "name", "") or "").startswith(RENDER_MATERIAL_PREFIX):
            return material
    return None


def build_missing_marked_materials(
    obj,
    unique_str: str | None = None,
    mesh_name: str | None = None,
    logic_name: str | None = None,
):
    """给**已有**物体补齐缺失的贴图材质（渲染材质 + 各类型规范材质）。

    与 :func:`build_marked_materials` 的区别是「补齐」语义：物体上已经有同名词同
    类型的材质时**原样保留**（不重建节点，避免抹掉用户的手工修改），只新建缺的
    类型，并保证渲染材质排在槽 0（网格面 ``material_index`` 都是 0，靠它渲染）。

    返回 ``(新建数, 复用数, 警告列表)``；警告列表为空表示全部成功。
    """
    if obj is None:
        return 0, 0, ["物体为空"]

    if logic_name is None:
        logic_name = GlobalConfig.logic_name

    resolved_unique_str = str(unique_str or "").strip() or object_workspace_unique_str(obj)
    if not resolved_unique_str:
        return 0, 0, ["无法确定物体身份（缺少 WorkspaceUniqueStr）"]

    resolved_mesh_name = str(mesh_name or "").strip() or bare_name_from_unique_str(
        resolved_unique_str
    )

    marks, extract_folder = read_texture_marks(resolved_unique_str)
    if not marks or not extract_folder:
        return 0, 0, [f"读不到贴图标记（{resolved_unique_str}）"]

    entries = []
    spec_materials = []
    warnings = []
    created = 0
    reused = 0

    for mark in marks:
        if not is_slot_mark(mark):
            continue

        mark_name = str(get_mark_field(mark, "mark_name") or "").strip()
        if not mark_name:
            continue

        socket_type = mark_socket_type(mark_name)
        target_name = mark_material_name(mark_name, resolved_mesh_name)
        texture_path = resolve_mark_texture_path(extract_folder, mark)

        existing = _existing_material_by_name(obj, target_name) or _existing_material_by_type(
            obj, mark_name
        )
        if existing is not None:
            # 已有同类型材质：原样保留，只把它纳入渲染材质的接线来源。
            spec_materials.append(existing)
            reused += 1
            if texture_path:
                entries.append((socket_type, texture_path))
            else:
                warnings.append(f"{mark_name} 的贴图文件已不在工作空间，渲染材质缺此通道")
            continue

        if not texture_path:
            warnings.append(f"{mark_name} 的贴图文件不存在，跳过")
            continue

        material = bpy.data.materials.new(name=target_name)
        try:
            _apply_spec_material(material, socket_type, texture_path, logic_name)
        except Exception as ex:
            print("[贴图标记] 补齐材质失败 " + mark_name + ": " + str(ex))
            warnings.append(f"{mark_name} 建材质失败: {ex}")
            try:
                bpy.data.materials.remove(material)
            except Exception:
                pass
            continue

        entries.append((socket_type, texture_path))
        spec_materials.append(material)
        created += 1

    render_material = _existing_material_by_name(
        obj, render_material_name(resolved_mesh_name)
    ) or _existing_render_material(obj)
    if render_material is None and entries:
        render_material = bpy.data.materials.new(
            name=render_material_name(resolved_mesh_name)
        )
        try:
            _build_render_material(render_material, entries, logic_name)
        except Exception as ex:
            print("[贴图标记] 补齐渲染材质失败: " + str(ex))
            warnings.append(f"渲染材质建失败: {ex}")
            try:
                bpy.data.materials.remove(render_material)
            except Exception:
                pass
            render_material = None

    if not spec_materials and render_material is None:
        warnings.append("没有可补齐的材质")
        return 0, 0, warnings

    _assign_material_slots(obj, render_material, spec_materials, keep_leftovers=True)
    print(
        "[贴图标记] 补齐 "
        + str(getattr(obj, "name", ""))
        + "：新建 "
        + str(created)
        + " 个、复用 "
        + str(reused)
        + " 个（渲染材质: "
        + (render_material.name if render_material is not None else "无")
        + "）"
    )
    return created, reused, warnings

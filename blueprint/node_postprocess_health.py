# -*- coding: utf-8 -*-
"""血量检测（仅限ZZZ）后处理节点。

数据通路移植自 ZZMI 的 ``Libraries\\HP bar``（hp.ini / characters.ini /
OrderIds.CS.hlsl / SummHp.CS.hlsl），落地方式：

- 钩住游戏自己的队伍 UI draw：图标与血条的 4 个 shader 哈希给 draw 打标签，
  图标贴图哈希识别「谁」，血条 PS 的 ``ps-cb1`` 给出
  「多少血」，两个 CS 把前台血量和按首顶点 X 排序的角色 id 打包进一条
  ``StructuredBuffer``，``[Present]`` 里用 ``store`` 回读并解包。仅当前台角色
  与目标角色一致时更新血量和档位，目标退到后台或 UI 消失时保持上次输出。
- 产物分两处：mod 根目录 ``hp_detect.ini``（整文件重写，内容不变不落盘）
  + 主 ini 尾部一个 ``include`` 尾块。尾块按基类的自动追加标记切段重写，
  因此改参数后重新导出会更新，不需要手删。
- 角色识别表不进 Python 硬编码：``Toolset/hp_detect/characters_source.ini``
  基于 ZZMI ``characters.ini`` 维护中文名与识别条目，import 时解析成 Enum 与
  ``[TextureOverrideHpDetectChar N]`` 段。

关于删掉的旧属性（health_character_hash / health_combat_ui_hash /
health_bar_hash）：这三个名字对应的只是旧模板里的 3 个哈希，新实现改用
「4 个 shader 哈希 + 角色表」。``bpy.props`` 删除后旧 .blend 里残留的是
IDProperty，Blender 不会因为 RNA 属性消失而报错（它只是不再暴露），
所以这里直接删除、不保留同名占位属性。
"""

import bpy
import glob
import os
import re

from ..common.safe_write import (
    copy_file_if_changed,
    write_bytes_if_changed,
    write_text_if_changed,
)
from .node_postprocess_base import SSMTNode_PostProcess_Base

HEALTH_MODULE_MARKER = "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---"
HP_DETECT_INI_NAME = "hp_detect.ini"
CHARACTERS_SOURCE_NAME = "characters_source.ini"
SHADER_FILES = ("hp_detect_order_ids.hlsl", "hp_detect_summ_hp.hlsl")

# 参考 hp.ini:50 / :59 / :158 / :162。游戏更新后这 4 个哈希最先失效。
DEFAULT_ICON_VS_HASH = "cdc90aee00e7900d"
DEFAULT_ICON_PS_HASH = "857cd16250c6142e"
DEFAULT_BAR_VS_HASH = "cdc7f730110fde36"
DEFAULT_BAR_PS_HASH = "bc8c64835ea6beed"

DEFAULT_HEALTH_PARAM = "$HealthLevel"
DEFAULT_HEALTH_LEVELS = 10

# 角色表里的段名有 ``[TextureOverrided Ellen Skin]`` 这种笔误（多一个 d），
# 用 \w* 吃掉，保证这些皮肤条目也能解析到。
_SECTION_RE = re.compile(r"^\s*\[TextureOverride\w*\s+(.+?)\]\s*$", re.MULTILINE)
_HASH_RE = re.compile(r"^\s*hash\s*=\s*([0-9A-Fa-f]+)\s*$", re.MULTILINE)
_CHARACTER_ID_RE = re.compile(r"^\s*\$character_id\s*=\s*(\d+)\s*$", re.MULTILINE)
# 段名已换成游戏内中文名，英文原名留在段内注释 `; 原名 Alice（id 1）` 里。
_ALIAS_RE = re.compile(r"^\s*;\s*原名\s+(.+?)\s*$", re.MULTILINE)


def _toolset_dir():
    addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(addon_dir, "Toolset", "hp_detect")


def parse_character_table(text):
    """把 characters.ini 解析成 [{"name", "alias", "hash", "character_id"}, ...]。

    ``name`` 是段名（现在是游戏内简体中文名），``alias`` 是段内 ``; 原名 X`` 注释里的
    英文原名，只用于导出时写注释，不参与任何匹配。
    """
    entries = []
    headers = list(_SECTION_RE.finditer(text))
    for index, match in enumerate(headers):
        block_end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = text[match.end():block_end]
        hash_match = _HASH_RE.search(block)
        id_match = _CHARACTER_ID_RE.search(block)
        if not hash_match or not id_match:
            continue
        alias_match = _ALIAS_RE.search(block)
        alias = alias_match.group(1).strip() if alias_match else ""
        # 表里写成 `; 原名 Alice（id 1）`，id 那截是给人看的，别名只留英文
        alias = re.sub(r"（id\s*\d+）\s*$", "", alias).strip()
        entries.append({
            "name": match.group(1).strip(),
            "alias": alias,
            "hash": hash_match.group(1).lower(),
            "character_id": int(id_match.group(1)),
        })
    return entries


def _load_character_table():
    path = os.path.join(_toolset_dir(), CHARACTERS_SOURCE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as file_obj:
            text = file_obj.read()
    except OSError as exc:
        return [], f"无法读取角色表 {path}: {exc}"

    entries = parse_character_table(text)
    if not entries:
        return [], f"角色表 {path} 里没有解析到任何 [TextureOverride ...] 段"
    bad_ids = sorted({entry["character_id"] for entry in entries if entry["character_id"] > 255})
    if bad_ids:
        return [], f"角色表 {path} 的 character_id 超出 8bit 打包范围: {bad_ids}"
    return entries, ""


def _build_character_enum(entries):
    # 同一角色的多套皮肤共用一个 id，Enum 只保留第一次出现的名字。
    by_id = {}
    for entry in entries:
        by_id.setdefault(entry["character_id"], entry["name"])
    return [(str(character_id), f"{name} (id {character_id})", "")
            for character_id, name in sorted(by_id.items())]


CHARACTER_ENTRIES, CHARACTERS_ERROR = _load_character_table()

_CHARACTER_ITEMS = _build_character_enum(CHARACTER_ENTRIES)
if not _CHARACTER_ITEMS:
    _CHARACTER_ITEMS = [("0", "（角色表不可用）", "")]
_CHARACTER_ITEM_DEFAULT = _CHARACTER_ITEMS[0][0]


def _health_param_token(raw):
    token = str(raw or "").strip() or DEFAULT_HEALTH_PARAM
    if not token.startswith("$"):
        token = "$" + token
    return token


class SSMTNode_PostProcess_HealthDetection(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_HealthDetection'
    bl_label = '血量检测（仅限ZZZ）'
    bl_description = '目标角色处于前台时更新血量映射，退到后台后保持上次输出 (适配ZZZ)'

    health_target_character: bpy.props.EnumProperty(
        name="目标角色",
        description="仅当该角色处于前台时更新血量；后台保持上次输出",
        items=_CHARACTER_ITEMS,
        default=_CHARACTER_ITEM_DEFAULT,
    )
    health_param_name: bpy.props.StringProperty(
        name="参数名称",
        description="血量映射输出的档位变量名（会自动加 $ 并在 [Constants] 里 global 声明）",
        default=DEFAULT_HEALTH_PARAM,
        maxlen=256,
        update=lambda self, context: self.update_node_width([self.health_param_name])
    )
    health_levels: bpy.props.IntProperty(
        name="级数",
        description="血量映射的级数",
        default=DEFAULT_HEALTH_LEVELS,
        min=2,
        max=100
    )
    health_icon_vs_hash: bpy.props.StringProperty(
        name="图标 VS 哈希",
        description="队伍 UI 角色图标顶点着色器哈希（游戏更新后先改这里）",
        default=DEFAULT_ICON_VS_HASH,
        maxlen=32,
        update=lambda self, context: self.update_node_width([self.health_icon_vs_hash])
    )
    health_icon_ps_hash: bpy.props.StringProperty(
        name="图标 PS 哈希",
        description="队伍 UI 角色图标像素着色器哈希（游戏更新后先改这里）",
        default=DEFAULT_ICON_PS_HASH,
        maxlen=32,
        update=lambda self, context: self.update_node_width([self.health_icon_ps_hash])
    )
    health_bar_vs_hash: bpy.props.StringProperty(
        name="血条 VS 哈希",
        description="队伍 UI 血条顶点着色器哈希（游戏更新后先改这里）",
        default=DEFAULT_BAR_VS_HASH,
        maxlen=32,
        update=lambda self, context: self.update_node_width([self.health_bar_vs_hash])
    )
    health_bar_ps_hash: bpy.props.StringProperty(
        name="血条 PS 哈希",
        description="队伍 UI 血条像素着色器哈希（游戏更新后先改这里）",
        default=DEFAULT_BAR_PS_HASH,
        maxlen=32,
        update=lambda self, context: self.update_node_width([self.health_bar_ps_hash])
    )

    def draw_buttons(self, context, layout):
        if CHARACTERS_ERROR:
            layout.label(text="角色表解析失败（见控制台）", icon='ERROR')
            layout.label(text=CHARACTERS_ERROR[:64])
        layout.prop(self, "health_target_character")
        layout.prop(self, "health_param_name")
        layout.prop(self, "health_levels")
        box = layout.box()
        box.label(text="游戏更新后先改这 4 个哈希：")
        box.prop(self, "health_icon_vs_hash")
        box.prop(self, "health_icon_ps_hash")
        box.prop(self, "health_bar_vs_hash")
        box.prop(self, "health_bar_ps_hash")

    # ------------------------------------------------------------------
    # 路径 / 着色器
    # ------------------------------------------------------------------

    def _get_toolset_dir(self):
        return _toolset_dir()

    def _copy_shaders(self, res_dir):
        """把两个 CS 写进 mod 的 ``res/``；内容没变就不写，保住 mtime 让
        3DMigoto 的编译缓存继续命中（与 drag_interaction 节点同一约定）。"""
        toolset = self._get_toolset_dir()
        copied = 0
        for name in SHADER_FILES:
            source = os.path.join(toolset, name)
            if not os.path.isfile(source):
                raise FileNotFoundError(f"缺少血量检测着色器: {source}")
            if copy_file_if_changed(source, os.path.join(res_dir, name)):
                copied += 1
        return copied

    # ------------------------------------------------------------------
    # 生成 hp_detect.ini
    # ------------------------------------------------------------------

    def _target_character_id(self):
        try:
            return int(self.health_target_character)
        except (TypeError, ValueError):
            return 0

    def _build_hp_detect_ini(self):
        param = _health_param_token(self.health_param_name)
        levels = int(self.health_levels)
        levels = min(100, max(2, levels))
        target_id = self._target_character_id()

        icon_vs = str(self.health_icon_vs_hash or DEFAULT_ICON_VS_HASH).strip()
        icon_ps = str(self.health_icon_ps_hash or DEFAULT_ICON_PS_HASH).strip()
        bar_vs = str(self.health_bar_vs_hash or DEFAULT_BAR_VS_HASH).strip()
        bar_ps = str(self.health_bar_ps_hash or DEFAULT_BAR_PS_HASH).strip()

        lines = [
            "; ==============================================================================",
            "; 血量检测模块（由 TheHerta4「血量检测（仅限ZZZ）」节点自动生成，勿手改）",
            "; 数据通路移植自 ZZMI 的 HP bar 库；游戏更新后先改节点的 4 个 shader 哈希属性。",
            "; 本文件每次导出整文件重写；主 ini 只保留一行 include。",
            "; ==============================================================================",
            "",
            "[Constants]",
            "; 总开关（本节点自己的，不依赖 d3dx.ini 里的外部变量）",
            "global $hpb_enabled = 1",
            "; 目标角色 id（节点 Enum 属性的值）",
            f"global $hpb_target_id = {target_id}",
            "; 输出：目标角色血量比例 0..1（仅前台更新，后台或无 UI 时保持上次值）",
            "global $hpb_health = 1.0",
            "; 调试：本次采样的前台角色匹配目标 = 1，否则 = 0",
            "global $hpb_found = 0",
            f"; 输出档位变量（节点「参数名称」属性）",
            f"global {param} = 0",
            "",
            "; 内部状态",
            "global $hpb_store_temp = 0",
            "global $hpb_sample_valid = 0",
            "global $hpb_bar_x = 0",
            "global $hpb_icon_x = 0",
            "global $hpb_active = 0",
            "global $hpb_character_counter = 0",
            "global $hpb_icon_counter = 0",
            "global $hpb_hp_main = -1",
            "global $hpb_next_update = 0",
            "; 冷启动就要有窗口：Clear 到点后也会把它重置成 3",
            "global $hpb_update_request = 3",
            "global $hpb_width = 0",
            "global $hpb_height = 0",
            "global $hpb_character_id = 0",
            "global $hpb_character_id_0 = 0",
            "",
            "; 32 字节/元素 × 2：元素 0 = 血量，元素 1 = 角色 id（见两个 CS 的 struct）",
            "[Resource.HpbUAV]",
            "type = StructuredBuffer",
            "stride = 32",
            "array = 2",
            "",
            "[CustomShader.HpbOrderIds]",
            "flags = optimization_level3 all_resources_bound skip_validation",
            "cs = ./res/hp_detect_order_ids.hlsl",
            "",
            "local $x1 = x1",
            "local $y1 = y1",
            "local $z1 = z1",
            "local $w1 = w1",
            "",
            "x1 = first_vertex // 1",
            "y1 = $hpb_character_counter // 1",
            "z1 = $hpb_character_id // 1",
            "w1 = vb0->Stride",
            "",
            "cs-u0 = ref Resource.HpbUAV",
            "cs-t0 = vb0",
            "",
            "Dispatch = 1, 1, 1",
            "",
            "cs-u0 = null",
            "cs-t0 = null",
            "",
            "x1 = $x1",
            "y1 = $y1",
            "z1 = $z1",
            "w1 = $w1",
            "",
            "[CustomShader.HpbSummHp]",
            "flags = optimization_level3 all_resources_bound skip_validation",
            "cs = ./res/hp_detect_summ_hp.hlsl",
            "",
            "local $x1 = x1",
            "local $y1 = y1",
            "local $z1 = z1",
            "",
            "x1 = first_vertex // 1",
            "y1 = $hpb_character_counter // 1",
            "z1 = vb0->Stride",
            "",
            "cs-u0 = ref Resource.HpbUAV",
            "cs-t0 = vb0",
            "cs-cb1 = ps-cb1",
            "",
            "Dispatch = 1, 1, 1",
            "",
            "cs-u0 = null",
            "cs-t0 = null",
            "cs-cb1 = null",
            "",
            "x1 = $x1",
            "y1 = $y1",
            "z1 = $z1",
            "",
            "; 回读 + 解包；只匹配前台，血量包 bit 21 表示已采到前台血条",
            "[CommandList.HpbSetValue]",
            "store = $hpb_store_temp, Resource.HpbUAV, 7",
            "$hpb_sample_valid = ($hpb_store_temp >> 21) & 1",
            "$hpb_hp_main = (($hpb_store_temp >> 14) & 127) * 0.01",
            "",
            "store = $hpb_store_temp, Resource.HpbUAV, 15",
            "$hpb_character_id_0 = ($hpb_store_temp >> 16) & 255",
            "store = $hpb_bar_x, Resource.HpbUAV, 3",
            "store = $hpb_icon_x, Resource.HpbUAV, 11",
            "",
            "; 前台头像在前台血条左侧；前台头像未知时，禁止把已知后台头像当成前台",
            "$hpb_found = 0",
            "if $hpb_sample_valid && ($hpb_character_id_0 == $hpb_target_id) && ($hpb_icon_x < $hpb_bar_x)",
            "\t$hpb_health = $hpb_hp_main",
            "\t$hpb_found = 1",
            "endif",
            "",
            "; 帧首：递减更新窗口、结算图标计数、节流清空 UAV",
            "[CommandList.HpbClear]",
            "if $hpb_active",
            "\t$hpb_active = $hpb_active - 1",
            "\tif $hpb_update_request",
            "\t\t$hpb_update_request = $hpb_update_request - 1",
            "\tendif",
            "\tif $hpb_icon_counter",
            "\t\t$hpb_character_counter = $hpb_icon_counter",
            "\t\t$hpb_icon_counter = 0",
            "\tendif",
            "\tif time > $hpb_next_update",
            "\t\tclear = Resource.HpbUAV",
            "\t\t$hpb_hp_main = -1",
            "\t\t$hpb_next_update = time + 0.35",
            "; 清空与点火同帧做，下一帧的 UI draw 才落在窗口里",
            "\t\t$hpb_update_request = 3",
            "\tendif",
            "elif $hpb_character_counter",
            "\t$hpb_character_counter = 0",
            "; 血条暂时消失：留一次待更新的窗口，等 UI 回来再采",
            "\t$hpb_update_request = 1",
            "endif",
            "",
            f"[ShaderOverrideHpbIconsVS]",
            f"hash = {icon_vs}",
            "allow_duplicate_hash = true",
            "filter_index = 909.709",
            "if $hpb_enabled",
            "\tif ps == 857.625",
            "; 无条件补纹理扫描：不依赖外部库的开关状态",
            "\t\tCheckTextureOverride = ps-t0",
            "\tendif",
            "endif",
            "",
            "[ShaderOverrideHpbIconsPS]",
            f"hash = {icon_ps}",
            "allow_duplicate_hash = true",
            "filter_index = 857.625",
            "if $hpb_enabled && $hpb_character_id",
            "\tif vs == 909.709",
            "\t\t$hpb_icon_counter = $hpb_icon_counter + 1",
            "\t\tif $hpb_update_request && $hpb_hp_main == -1",
            "\t\t\trun = CustomShader.HpbOrderIds",
            "\t\tendif",
            "\tendif",
            "\t$hpb_character_id = 0",
            "endif",
            "",
            "[ShaderOverrideHpbBarVS]",
            f"hash = {bar_vs}",
            "allow_duplicate_hash = true",
            "filter_index = 8681.594",
            "",
            "[ShaderOverrideHpbBarPS]",
            f"hash = {bar_ps}",
            "allow_duplicate_hash = true",
            "if $hpb_enabled && (index_count == 6 || index_count == 54)",
            "\tif vs == 8681.594",
            "\t\t$hpb_height = ps-t0->Height",
            "\t\t$hpb_width = ps-t0->Width",
            "\t\tif (($hpb_width == 1024 && $hpb_height == 1024) || ($hpb_width == 1024 && $hpb_height == 512)) && ps-t0->Format == DXGI_FORMAT_BC3_UNORM_SRGB && ps-t0->Mips == 1",
            "\t\t\t$hpb_active = 3",
            "\t\t\tif $hpb_update_request && $hpb_hp_main == -1 && ($hpb_character_counter > $hpb_icon_counter)",
            "\t\t\t\trun = CustomShader.HpbSummHp",
            "\t\t\tendif",
            "\t\tendif",
            "\tendif",
            "endif",
            "",
            f"; 角色识别：图标贴图哈希 → 角色 id（{len(CHARACTER_ENTRIES)} 条，多皮肤共用 id）",
        ]

        for index, entry in enumerate(CHARACTER_ENTRIES, start=1):
            label = f"{entry['name']}（id {entry['character_id']}"
            if entry.get("alias"):
                label += f"，原名 {entry['alias']}"
            label += "）"
            lines.extend([
                "",
                f"[TextureOverrideHpDetectChar {index}]",
                f"; {label}",
                "match_priority = -9999",
                f"hash = {entry['hash']}",
                f"$hpb_character_id = {entry['character_id']}",
            ])

        present = [
            "",
            "; 两相位：pre 里读回并解包，段体里更新 $hpb_health 并做档位赋值，post 里清状态",
            "[Present]",
            "if $hpb_enabled && $hpb_character_counter && $hpb_hp_main == -1",
            "\tpre run = CommandList.HpbSetValue",
            "endif",
            "",
            f"; 血量映射：$hpb_health (0.0-1.0) → {param} (0-{levels - 1})，每级 {1.0 / levels:.4f}",
            "if $hpb_found",
        ]
        for i in range(levels):
            lower_bound = i / levels
            upper_bound = (i + 1) / levels
            if i == 0:
                present.append(f"\tif $hpb_health >= {lower_bound:.4f} && $hpb_health < {upper_bound:.4f}")
            elif i == levels - 1:
                # 最后一档只判下界：>= 1.0 的钳位值也落到最高级。
                present.append("\telse")
            else:
                present.append(f"\telif $hpb_health >= {lower_bound:.4f} && $hpb_health < {upper_bound:.4f}")
            present.append(f"\t\t{param} = {i}")
        present.append("\tendif")
        present.append("endif")
        present.append("post run = CommandList.HpbClear")

        lines.extend(present)
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 主 ini 尾块
    # ------------------------------------------------------------------

    @staticmethod
    def _tail_block():
        return (
            "; ==============================================================================\n"
            f"{HEALTH_MODULE_MARKER}\n"
            "; ==============================================================================\n"
            f"include = ./{HP_DETECT_INI_NAME}\n"
        )

    @staticmethod
    def _is_block_decoration(line):
        stripped = str(line or "").strip()
        if not stripped:
            return True
        return set(stripped) <= set(";=-\t ")

    @classmethod
    def _strip_health_blocks(cls, content):
        """删掉本节点写过的所有尾块（只删自己的，别的节点块原样保留）。"""
        text = str(content or "")
        for _ in range(32):
            lines = text.splitlines(keepends=True)
            marker_index = None
            for index, line in enumerate(lines):
                if HEALTH_MODULE_MARKER in line:
                    marker_index = index
                    break
            if marker_index is None:
                return text

            start_index = marker_index
            while start_index > 0 and cls._is_block_decoration(lines[start_index - 1]):
                start_index -= 1

            end_index = len(lines)
            for index in range(marker_index + 1, len(lines)):
                if cls.is_known_auto_appended_marker(lines[index]):
                    end_index = index
                    break
            while end_index > marker_index + 1 and cls._is_block_decoration(lines[end_index - 1]):
                end_index -= 1

            before = "".join(lines[:start_index]).rstrip("\r\n")
            after = "".join(lines[end_index:]).lstrip("\r\n")
            text = "\n\n".join(part for part in (before, after) if part)
        return text

    @classmethod
    def replace_health_tail(cls, content):
        """把主 ini 里的旧尾块换成新的 include 尾块；没有则追加。"""
        head = cls._strip_health_blocks(content).rstrip("\r\n")
        block = cls._tail_block()
        return (head + "\n\n" + block) if head else block

    # ------------------------------------------------------------------
    # 导出入口
    # ------------------------------------------------------------------

    def execute_postprocess(self, mod_export_path):
        print(f"血量检测后处理节点开始执行，Mod导出路径: {mod_export_path}")

        if CHARACTERS_ERROR:
            raise RuntimeError(f"血量检测节点角色表不可用: {CHARACTERS_ERROR}")

        ini_files = sorted(glob.glob(os.path.join(mod_export_path, "*.ini")))
        # 上一次导出生成的 hp_detect.ini 也在根目录，不能把它当主 ini。
        ini_files = [path for path in ini_files
                     if os.path.basename(path).lower() != HP_DETECT_INI_NAME]
        if not ini_files:
            print("路径中未找到任何.ini文件，血量检测模块未生成")
            return False

        target_ini_file = ini_files[0]
        self._create_cumulative_backup(target_ini_file, mod_export_path)

        module_text = self._build_hp_detect_ini()
        write_text_if_changed(os.path.join(mod_export_path, HP_DETECT_INI_NAME), module_text)
        self._copy_shaders(os.path.join(mod_export_path, "res"))

        with open(target_ini_file, "rb") as file_obj:
            raw = file_obj.read()
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        if has_bom:
            raw = raw[3:]
        content = raw.decode("utf-8")
        new_content = self.replace_health_tail(content)
        payload = new_content.encode("utf-8")
        if has_bom:
            payload = b"\xef\xbb\xbf" + payload
        changed = write_bytes_if_changed(target_ini_file, payload)

        print(f"血量检测模块已写入: {HP_DETECT_INI_NAME}；"
              f"主 ini {'已更新' if changed else '无变化'}: {os.path.basename(target_ini_file)}")
        print(f"目标角色 id: {self._target_character_id()}；血量级数: {self.health_levels}")
        return True


classes = (
    SSMTNode_PostProcess_HealthDetection,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

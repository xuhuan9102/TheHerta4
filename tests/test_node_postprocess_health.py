# -*- coding: utf-8 -*-
"""血量检测后处理节点（blueprint/node_postprocess_health.py）测试。

覆盖：
- 角色表解析：80 条 TextureOverride → 59 个唯一 id；
- hp_detect.ini 的结构契约：[Constants] 里档位变量有 global、[Resource] 带
  type/stride/array、两个 [CustomShader] 指向 ./res/*.hlsl、[Present] 只出现一次、
  80 条角色段；
- 两个 CS 被复制到 mod 的 res/；
- 仅前台目标更新血量和档位；后台、无有效数据时保持上次输出；
- 同一目录跑两次输出逐字节相同（幂等）；
- 主 ini 尾部只有一个本节点标记块，旧的 470 行尾块被整块替换成 include，
  别的节点的自动追加块不受影响。
"""
import importlib.util
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PKG = "_health_test_pkg"
for _package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    _package = types.ModuleType(_package_name)
    _package.__path__ = []
    sys.modules[_package_name] = _package


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


# ---------------------------------------------------------------------------
# 假 bpy（props 返回「默认值载体」，加载模块后再把注解还原成类属性）
#
# 节点用的是仓库统一的注解式写法 ``x: bpy.props.StringProperty(...)``（没有 ``=``）。
# 真 Blender 里 ``bpy.props.X()`` 返回 _PropertyDeferred，由 RNA 元类从
# ``__annotations__`` 注册；假 bpy 如果不还原，实例上就取不到这些属性。
# ---------------------------------------------------------------------------

class _PropDefault:
    __slots__ = ("default",)

    def __init__(self, default):
        self.default = default


_fake_bpy = _install_module(
    "bpy",
    types=types.SimpleNamespace(Node=type("Node", (), {}), NodeSocket=type("NodeSocket", (), {})),
    props=types.SimpleNamespace(
        StringProperty=lambda **kwargs: _PropDefault(kwargs.get("default", "")),
        IntProperty=lambda **kwargs: _PropDefault(kwargs.get("default", 0)),
        BoolProperty=lambda **kwargs: _PropDefault(kwargs.get("default", False)),
        EnumProperty=lambda **kwargs: _PropDefault(kwargs.get("default", "")),
    ),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)


# ---------------------------------------------------------------------------
# 基类桩：只提供本节点用到的标记识别与备份
# ---------------------------------------------------------------------------

class _Base:
    AUTO_APPENDED_SECTION_MARKERS = (
        "; --- AUTO-APPENDED SLIDER CONTROL PANEL ---",
        "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
        "; --- AUTO-APPENDED DRAG INTERACTION MODULE ---",
    )
    AUTO_APPENDED_SECTION_MARKER_PREFIXES = (
        "; --- AUTO-APPENDED UI PANEL ",
        "; --- AUTO-APPENDED CUSTOM TEXT ",
        "; --- AUTO-APPENDED SKIP ",
    )

    def _create_cumulative_backup(self, _ini_file_path, _mod_export_path):
        return None

    def update_node_width(self, _values=None):
        return None

    @classmethod
    def is_known_auto_appended_marker(cls, line):
        stripped = str(line or "").strip()
        if stripped in cls.AUTO_APPENDED_SECTION_MARKERS:
            return True
        return any(stripped.startswith(prefix) for prefix in cls.AUTO_APPENDED_SECTION_MARKER_PREFIXES)


_install_module(f"{PKG}.blueprint.node_postprocess_base", SSMTNode_PostProcess_Base=_Base)

# common.safe_write 没有 bpy 依赖，直接按文件加载真实实现，测的就是节点真正用的写盘语义。
_safe_write_spec = importlib.util.spec_from_file_location(
    f"{PKG}.common.safe_write", REPO_ROOT / "common" / "safe_write.py"
)
_safe_write_module = importlib.util.module_from_spec(_safe_write_spec)
sys.modules[_safe_write_spec.name] = _safe_write_module
_safe_write_spec.loader.exec_module(_safe_write_module)

_module_path = REPO_ROOT / "blueprint" / "node_postprocess_health.py"
_spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_health", _module_path)
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)

NODE = module.SSMTNode_PostProcess_HealthDetection
MARKER = module.HEALTH_MODULE_MARKER

# 注解式属性在真 Blender 里由 RNA 元类注册；假 bpy 里把 _PropDefault 还原成
# 普通类属性，这样实例上能读到真实默认值（不是硬塞的测试值）。
for _prop_name, _prop_value in list(NODE.__annotations__.items()):
    if isinstance(_prop_value, _PropDefault):
        setattr(NODE, _prop_name, _prop_value.default)


def _section(text, name):
    return re.search(r"^\[" + re.escape(name) + r"\]\s*\n(.*?)(?=^\[|\Z)",
                     text, re.MULTILINE | re.DOTALL).group(1)


def _run_commands(text, values, resource):
    """执行生成的回读/映射命令，用输入缓冲验证输出是否按前台状态保持。"""
    def evaluate(expression):
        expression = expression.replace("&&", " and ").replace("||", " or ")
        expression = re.sub(r"\$\w+", lambda match: repr(values[match.group()]), expression)
        return eval(expression, {"__builtins__": {}}, {})

    stack = []
    active = True
    for line in text.splitlines():
        line = line.split(";", 1)[0].strip()
        if not line or line.startswith(("pre ", "post ")):
            continue
        if line.startswith("if "):
            selected = active and bool(evaluate(line[3:]))
            stack.append([active, selected])
            active = selected
        elif line.startswith("elif "):
            parent, taken = stack[-1]
            active = parent and not taken and bool(evaluate(line[5:]))
            stack[-1][1] |= active
        elif line == "else":
            parent, taken = stack[-1]
            active = parent and not taken
            stack[-1][1] = True
        elif line == "endif":
            active = stack.pop()[0]
        elif active and line.startswith("store = "):
            variable, buffer_name, offset = line[8:].split(", ")
            assert buffer_name == "Resource.HpbUAV"
            values[variable] = resource[int(offset)]
        elif active:
            variable, expression = line.split(" = ", 1)
            values[variable] = evaluate(expression)
    assert not stack


class HealthDetectionNodeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mod_dir = Path(self._tmp.name)
        self.main_ini = self.mod_dir / "mod.ini"
        self.main_ini.write_text("; 主配置表\n[TextureOverrideBody]\nhash = 01234567\n", encoding="utf-8")
        self.node = NODE()

    def tearDown(self):
        self._tmp.cleanup()

    # -- 角色表 ---------------------------------------------------------

    def test_character_table_parses_all_texture_overrides(self):
        entries = module.CHARACTER_ENTRIES
        self.assertEqual(len(entries), 80)
        self.assertEqual(len({entry["character_id"] for entry in entries}), 59)
        alice = [entry for entry in entries if entry["hash"] == "93ef2127"][0]
        self.assertEqual(alice["hash"], "93ef2127")
        self.assertEqual(alice["character_id"], 1)
        # 段名已是游戏内简体中文名，英文原名留在 alias（`; 原名 Alice（id 1）`）
        self.assertEqual(alice["name"], "爱丽丝")
        self.assertEqual(alice["alias"], "Alice")
        # 参考实现里的笔误段名（TextureOverrided Ellen Skin）也要解析到，且换成中文 + 皮肤后缀
        self.assertTrue(any(entry["name"] == "艾莲·皮肤" for entry in entries))
        # 所有 id 都必须有中文名（不许留 ? / 英文名）
        self.assertEqual(
            sorted({entry["character_id"] for entry in entries
                    if not re.search(r"[\u4e00-\u9fff]", entry["name"])}),
            [],
        )
        # 每个 id 至少有一条英文别名，保证导出注释能同时给出原名
        self.assertEqual(
            sorted({entry["character_id"] for entry in entries if not entry["alias"]}),
            [],
        )

    def test_new_characters_and_skin_are_selectable_and_exported(self):
        expected = {
            "35ac8a9c": ("菲欧妮", "Phoenix", 58),
            "5a7797c1": ("菲欧妮·皮肤", "Phoenix Skin", 58),
            "ecc1074a": ("洛克希", "Roxy", 59),
        }
        for texture_hash, (name, alias, character_id) in expected.items():
            with self.subTest(name=name):
                entry, = [entry for entry in module.CHARACTER_ENTRIES
                          if entry["hash"] == texture_hash]
                self.assertEqual((entry["name"], entry["alias"], entry["character_id"]),
                                 (name, alias, character_id))
                self.node.health_target_character = str(character_id)
                text = self.node._build_hp_detect_ini()
                self.assertIn(f"global $hpb_target_id = {character_id}\n", text)
                self.assertIn(f"hash = {texture_hash}\n$hpb_character_id = {character_id}\n", text)
        for character_id, name in ((58, "菲欧妮"), (59, "洛克希")):
            self.assertEqual([item[1] for item in module._CHARACTER_ITEMS
                              if item[0] == str(character_id)], [f"{name} (id {character_id})"])

    def test_health_and_level_update_only_when_target_is_in_front(self):
        for target_id in (58, 59):
            with self.subTest(target_id=target_id):
                self.node.health_target_character = str(target_id)
                text = self.node._build_hp_detect_ini()
                values = {name: float(value) for name, value in re.findall(
                    r"^global (\$\w+) = ([\d.-]+)$", text, re.MULTILINE)}
                readback = _section(text, "CommandList.HpbSetValue")
                present = _section(text, "Present")

                def sample(ids, hp, icon_x=-839.1, valid=True):
                    packed_ids = (ids[0] << 16) | (ids[1] << 8) | ids[2]
                    resource = {7: ((1 << 21) | (hp << 14)) if valid else 0,
                                15: packed_ids, 3: -699.9, 11: icon_x}
                    _run_commands(readback, values, resource)
                    _run_commands(present, values, resource)
                    return (values["$hpb_health"], values["$HealthLevel"], values["$hpb_found"])

                # 从未采到前台目标时，输出档位也保持初值。
                self.assertEqual(sample((56, target_id, 53), 100), (1.0, 0.0, 0))
                self.assertEqual(sample((target_id, 56, 53), 42), (0.42, 4, 1))
                # 切到后台、离队、前台头像未知、无有效血条时均不刷新输出。
                for ids, hp, icon_x, valid in (
                    ((56, target_id, 53), 90, -839.1, True),
                    ((56, 53, target_id), 90, -839.1, True),
                    ((56, 53, 0), 90, -839.1, True),
                    ((target_id, 53, 0), 90, -398.2, True),
                    ((target_id, 56, 53), 0, -839.1, False),
                    ((0, 0, 0), 0, 0, False),
                ):
                    self.assertEqual(sample(ids, hp, icon_x, valid), (0.42, 4, 0))
                # 回到前台后恢复刷新；合法的零血量不能与未采到数据混淆。
                self.assertEqual(sample((target_id, 56, 53), 0), (0.0, 0, 1))
                self.assertEqual(sample((target_id, 56, 53), 100), (1.0, 9, 1))

    # -- hp_detect.ini 结构 ---------------------------------------------

    def test_generated_ini_contract(self):
        self.assertTrue(self.node.execute_postprocess(str(self.mod_dir)))
        text = (self.mod_dir / "hp_detect.ini").read_text(encoding="utf-8")

        self.assertIn("[Constants]", text)
        self.assertIn("\nglobal $HealthLevel = 0\n", text)

        self.assertIn("[Resource.HpbUAV]", text)
        self.assertIn("type = StructuredBuffer", text)
        self.assertIn("stride = 32", text)
        self.assertIn("array = 2", text)

        self.assertIn("[CustomShader.HpbOrderIds]", text)
        self.assertIn("[CustomShader.HpbSummHp]", text)
        self.assertIn("cs = ./res/hp_detect_order_ids.hlsl", text)
        self.assertIn("cs = ./res/hp_detect_summ_hp.hlsl", text)

        # 只允许一个 [Present]
        self.assertEqual(text.count("[Present]"), 1)
        # 两相位：pre 读回、post 清状态
        self.assertIn("pre run = CommandList.HpbSetValue", text)
        self.assertIn("post run = CommandList.HpbClear", text)

        # 所有用到全局变量都必须先 global 声明（现状缺声明就是加载报错的原因）
        declared = set()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("global "):
                declared.add(stripped.split()[1].split("=")[0].strip())
        used = set(re.findall(r"\$hpb_[A-Za-z0-9_]+", text))
        self.assertTrue(used)
        self.assertEqual(sorted(used - declared), [])
        self.assertIn("$hpb_health", declared)
        self.assertIn("$hpb_character_id", declared)
        self.assertIn("$hpb_hp_main", declared)

        # 冷启动自点火：初值必须是 3，Clear 到点无条件清空 + 重点火，
        # 否则 update_request 永远起不来、整条链路死锁。
        self.assertIn("global $hpb_update_request = 3", text)
        self.assertIn("if time > $hpb_next_update", text)
        self.assertNotIn("if $hpb_hp_main != -1 && (time > $hpb_next_update)", text)

        self.assertEqual(text.count("[TextureOverrideHpDetectChar "), 80)
        # 总开关不依赖 ZZMI 的 $costume_mods
        self.assertNotIn("$costume_mods", text)
        self.assertNotIn("check_textures", text)

    def test_shaders_copied_to_res(self):
        self.node.execute_postprocess(str(self.mod_dir))
        for name in module.SHADER_FILES:
            source = Path(module._toolset_dir()) / name
            target = self.mod_dir / "res" / name
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), source.read_bytes())

    # -- 幂等 -----------------------------------------------------------

    def test_second_run_is_byte_identical(self):
        self.node.execute_postprocess(str(self.mod_dir))
        first_module = (self.mod_dir / "hp_detect.ini").read_bytes()
        first_main = self.main_ini.read_bytes()

        self.node.execute_postprocess(str(self.mod_dir))
        self.assertEqual((self.mod_dir / "hp_detect.ini").read_bytes(), first_module)
        self.assertEqual(self.main_ini.read_bytes(), first_main)

    # -- 主 ini 尾块 -----------------------------------------------------

    def test_main_ini_has_exactly_one_health_tail_block(self):
        self.node.execute_postprocess(str(self.mod_dir))
        text = self.main_ini.read_text(encoding="utf-8")
        self.assertEqual(text.count(MARKER), 1)
        self.assertEqual(text.count("include = ./hp_detect.ini"), 1)

    def test_legacy_tail_block_is_replaced_and_other_nodes_kept(self):
        legacy = (
            "; 主配置表\n[TextureOverrideBody]\nhash = 01234567\n\n\n"
            "; ==============================================================================\n"
            f"{MARKER}\n"
            "; ==============================================================================\n\n"
            "[Constants]\n"
            "global $Health = 1.0\n"
            "global $IsTargetOnScreen = 0\n"
            "\n[Present]\n"
            "$HealthLevel = 0\n"
        )
        other_node = (
            "\n\n; --- AUTO-APPENDED CUSTOM TEXT other-node ---\n"
            "[Present]\n"
            "post $other = 1\n"
        )
        self.main_ini.write_text(legacy + other_node, encoding="utf-8")

        self.node.execute_postprocess(str(self.mod_dir))
        text = self.main_ini.read_text(encoding="utf-8")

        self.assertEqual(text.count(MARKER), 1)
        self.assertNotIn("$IsTargetOnScreen", text)
        self.assertNotIn("global $Health = 1.0", text)
        self.assertIn("; --- AUTO-APPENDED CUSTOM TEXT other-node ---", text)
        self.assertIn("post $other = 1", text)
        self.assertIn("include = ./hp_detect.ini", text)


if __name__ == "__main__":
    unittest.main()

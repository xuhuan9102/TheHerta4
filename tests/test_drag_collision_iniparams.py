"""回归：拖拽节点的 IniParams 必须「一个分量一行」。

背景（notes/50）：碰撞检测那一段原本写成

    x101 = 1 0.002 0 0.9
    x102 = 1.5 -2.5 3.5 0.01
    x103 = 12 13 14 0.05
    x104 = $mode 3 4 5

这不是简写，是**语法错误**。`x101` 只取一个 float（IniParams[101].x），右边整串被
CommandListExpression::parse 当一个表达式分词；tokenise() 在遇到连续两个操作数时
直接抛 `Unexpected identifier`（CommandList.cpp 的 import_operand 标号）：

    import_operand:
        tree->tokens.emplace_back(std::move(operand));
        if (last_was_operand)
            throw CommandListSyntaxError(L"Unexpected identifier", friendly_pos);
        last_was_operand = true;

于是这一行不被任何解析器接受、x101~x104 全部保持 0，着色器的门控
`if (COLLISION_PARAMS.x > 0.5)` 永远为假 —— 碰撞解算一次都不执行，
而且不报错、不提示。同一分支的「关」一侧本来就是一分量一行，所以这是纯粹的不一致。

本文件锁定两件事：
  1. 碰撞段生成的四个槽，每个都必须恰好有 x/y/z/w 四个分量，且每个分量只有一个值；
  2. **整个节点生成的所有段**都不允许出现「一行多值」的 IniParams —— 这才是真正的回归网，
     同样的错误将来若出现在别处也会被抓住。

修复前（四行合并写法）测试 1 与 2 都会失败。
"""

import re
import unittest

from tests.test_node_postprocess_draginteraction import (
    _base_sections,
    _load_drag_module,
    _make_node,
)


# 引擎表达式里的操作符（CommandList.cpp: operator_tokens[]）
_OPERATOR_CHARS = set("+-*/%^&|!~<>()=,[]")

# 一个「裸原子」：数字字面量或 $变量。用于第二种判定。
_ATOM = re.compile(r"^(\$[A-Za-z0-9_.\\\[\]]+|[+-]?\d*\.?\d+(?:[eE][+-]?\d+)?|[+-]?0[xX][0-9a-fA-F]+)$")

# 一行 IniParams：<缩进><分量><索引> = <值>
_INIPARAM_LINE = re.compile(r"^\s*([xyzw])(\d+)\s*=\s*(.*?)\s*$")


def _is_value_list(value):
    """值是否像「一串并列的值」而不是「一个表达式」。

    两条判定，取并集：

    (a) 分词后多于一个记号，且整串不含任何操作符字符。
        这样 `$a / $b`、`(0.96 * $x)` 不会误判。
    (b) **每一个**记号都是裸原子（数字字面量或 `$变量`），且至少两个。
        (a) 必须放行 `-` / `+`（它们是操作符），于是形如
        `1.5 -2.5 3.5 0.01` 的列表会从 (a) 漏过去 —— 实测就是这么漏掉
        上游生成器的 `x102` 那一行的。(b) 补这个洞：真正的表达式在操作数
        之间一定有操作符，不可能是一串相邻的原子。
    """
    if not value:
        return False
    tokens = value.split()
    if len(tokens) < 2:
        return False
    if all(_ATOM.match(t) for t in tokens):
        return True
    if any(ch in _OPERATOR_CHARS for ch in value):
        return False
    return True


class CollisionIniParamEmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _emit_with_collision(self, **node_props):
        node = _make_node(self.mod, **node_props)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        comp = comps[0]
        # 模拟「已经成功烘出碰撞网格」的状态 —— 这正是生成器走「开」分支的条件。
        comp["collision_grid"] = {
            "bmin": (1.5, -2.5, 3.5),
            "h": 0.01,
            "h_c": 0.05,
            "dims": (12, 13, 14),
            "cdims": (3, 4, 5),
        }
        node._emit_sections(sections, comps, "testns")
        return comp, sections

    def _collision_components(self, sections, comp):
        """收集碰撞段里 slot 101..104 的四个分量。"""
        sec = f"[CustomShaderDragJiggle{comp['comp_name']}_testns]"
        self.assertIn(sec, sections, "碰撞参数应当发射进 jiggle 段")
        found = {}
        for line in sections[sec]:
            m = _INIPARAM_LINE.match(line)
            if not m:
                continue
            comp_ch, idx, value = m.group(1), m.group(2), m.group(3)
            if idx in ("101", "102", "103", "104"):
                found.setdefault(idx, {})[comp_ch] = value
        return found

    # ------------------------------------------------------------------

    def test_each_collision_slot_has_four_single_valued_components(self):
        """核心断言：四个槽各自恰好有 x/y/z/w，且每个分量只有一个值。"""
        comp, sections = self._emit_with_collision()
        found = self._collision_components(sections, comp)

        self.assertEqual(
            set(found), {"101", "102", "103", "104"},
            f"碰撞槽应当四个都在，实际 {sorted(found)}")

        for slot, comps in sorted(found.items()):
            self.assertEqual(
                set(comps), set("xyzw"),
                f"IniParams[{slot}] 缺少分量：有 {sorted(comps)}")
            for ch, value in sorted(comps.items()):
                self.assertFalse(
                    _is_value_list(value),
                    f"{ch}{slot} 的值 '{value}' 是一串并列的值 —— "
                    "引擎会抛 Unexpected identifier，整行作废（notes/50）")

    def test_collision_values_match_the_shader_contract(self):
        """值本身也要对：着色器按 [101].xyzw / [102].xyzw … 解读。"""
        comp, sections = self._emit_with_collision()
        found = self._collision_components(sections, comp)

        def f(slot, ch):
            return float(found[slot][ch])

        self.assertEqual(f("101", "x"), 1.0, "[101].x 是启用标志，必须为 1")
        self.assertAlmostEqual(f("101", "y"), 0.002, places=6, msg="[101].y = 碰撞裕量")
        self.assertEqual(f("101", "z"), 0.0, "[101].z = 模式（SOFT=0）")
        self.assertAlmostEqual(f("101", "w"), 0.9, places=6, msg="[101].w = 安全系数")

        # [102] = 网格原点 xyz + 细格边长 w
        self.assertAlmostEqual(f("102", "x"), 1.5, places=6)
        self.assertAlmostEqual(f("102", "y"), -2.5, places=6)
        self.assertAlmostEqual(f("102", "z"), 3.5, places=6)
        self.assertAlmostEqual(f("102", "w"), 0.01, places=6)

        # [103] = 细格维度 xyz + 粗格边长 w
        self.assertEqual((f("103", "x"), f("103", "y"), f("103", "z")), (12.0, 13.0, 14.0))
        self.assertAlmostEqual(f("103", "w"), 0.05, places=6)

        # [104] = 拖拽模式变量(x) + 粗格维度 yzw
        self.assertTrue(found["104"]["x"].startswith("$"),
                        "[104].x 应当是运行期变量名（$…）")
        self.assertEqual((f("104", "y"), f("104", "z"), f("104", "w")), (3.0, 4.0, 5.0))

    def test_hard_clamp_mode_maps_to_one(self):
        comp, sections = self._emit_with_collision(collision_mode="HARD")
        found = self._collision_components(sections, comp)
        self.assertEqual(float(found["101"]["z"]), 1.0,
                         "HARD 模式应当编码为 [101].z = 1")

    def test_disabled_path_still_writes_one_value_per_line(self):
        """没有碰撞网格时走「关」分支，也必须一分量一行。"""
        node = _make_node(self.mod)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        comps[0].pop("collision_grid", None)
        node._emit_sections(sections, comps, "testns")
        found = self._collision_components(sections, comps[0])

        self.assertEqual(set(found), {"101", "102", "103", "104"})
        for slot, comps4 in sorted(found.items()):
            for ch, value in sorted(comps4.items()):
                self.assertFalse(_is_value_list(value),
                                 f"{ch}{slot} = '{value}' 是一串值")

    # ------------------------------------------------------------------

    def test_no_section_emits_a_multi_value_iniparam(self):
        """回归网：**任何**段都不允许出现「一行多值」的 IniParams。

        覆盖整个节点，不只碰撞那一段 —— 同类错误若将来出现在别处，这里会先响。
        """
        comp, sections = self._emit_with_collision()
        offenders = []
        for sec_name, lines in sections.items():
            for n, line in enumerate(lines, 1):
                m = _INIPARAM_LINE.match(line)
                if m and _is_value_list(m.group(3)):
                    offenders.append(f"{sec_name}:{n}  {line.strip()}")
        self.assertEqual(
            offenders, [],
            "发现「一行多值」的 IniParams（引擎会整行作废，notes/50）：\n  "
            + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()

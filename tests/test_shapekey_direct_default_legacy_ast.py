"""生成端裁定（直出默认勾选 + 经典形态键发射器 legacy 标注）的静态证据用例。

覆盖三件事：
  ① `SSMTNode_PostProcess_ShapeKey.direct_export_mode` 的默认值是 **True**（对
     `bpy.props.BoolProperty` 调用做真实求值，不是字符串匹配）；且仓库里不存在第二处
     硬编码默认值，也没有任何地方用字面量赋值覆盖它。
  ② 非直出（标准）路线的诊断调用**排在静默空返回之前** —— 顺序回退即红。
  ③ 经典发射器 `M_IniHelper.add_shapekey_ini_sections` 在 ZZMI 侧被显式标注
     `legacy / never-fires-on-ZZMI`，同时它仍被其它 8 个游戏族的导出器调用（不是全局死
     代码）⇒ 采用「标注」而非「删除」这一决策可被复核。

证据口径见 review-reports/t84-direct-default-and-legacy-shapekey.md。
"""

import ast
import re
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SHAPEKEY_NODE_PATH = REPO_ROOT / "blueprint" / "node_postprocess_shapekey.py"
MULTIFILE_NODE_PATH = REPO_ROOT / "blueprint" / "node_multifile_export.py"
DIRECT_EXPORT_PATH = REPO_ROOT / "blueprint" / "direct_export.py"
M_INI_HELPER_PATH = REPO_ROOT / "common" / "m_ini_helper.py"
ZZMI_PATH = REPO_ROOT / "ui" / "universal" / "zzmi.py"

# 仓库里有多个非生产目录（.agent-teams/.scratch/.venv/…）与带 BOM 的脚本，
# 一律跳过：本文件的断言只针对生产源码。
# `reports/` 与 `review-reports/` 同理：它们存的是**取证快照**（例：
# reports/zzmi-fix/evidence/pre-fix-files/blueprint__node_postprocess_shapekey.py
# 是修复前的冻结副本），不是构建输入。
SKIP_DIR_PARTS = ("__pycache__", "node_modules", "dist", "build", "reports", "review-reports")


def _read_source(path):
    """读源码并归一化换行（CRLF → LF），保证 AST 行列号可精确切片。"""
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def _is_production_python_file(path):
    relative = path.relative_to(REPO_ROOT)
    if any(part.startswith(".") for part in relative.parts):
        return False
    return not any(part in SKIP_DIR_PARTS for part in relative.parts)


def _iter_repo_python_files():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if _is_production_python_file(path):
            yield path


def _parse(path):
    """解析生产源码；不可解析（BOM/语法）时返回 None（由调用方跳过）。"""
    try:
        return ast.parse(_read_source(path), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return None


def _iter_repo_trees():
    for path in _iter_repo_python_files():
        tree = _parse(path)
        if tree is not None:
            yield path, tree


# 全仓扫描只做一次（三个用例共用），避免把 3 次 AST 扫描都算进测试时长。
_REPO_TREES = None


def _repo_trees():
    global _REPO_TREES
    if _REPO_TREES is None:
        _REPO_TREES = list(_iter_repo_trees())
    return _REPO_TREES


def _source_segment(source, node):
    """按行列号精确切出节点源码。

    不用 `ast.get_source_segment`：它对含非 ASCII 的源码会因 UTF-8 字节偏移切片
    而对不齐（本仓库注释/字符串里有中文），直接返回 None。
    """
    lines = source.splitlines(keepends=True)
    if node.end_lineno == node.lineno:
        return lines[node.lineno - 1][node.col_offset : node.end_col_offset]
    first = lines[node.lineno - 1][node.col_offset :]
    middle = "".join(lines[node.lineno : node.end_lineno - 1])
    last = lines[node.end_lineno - 1][: node.end_col_offset]
    return first + middle + last


def _eval_direct_export_mode_property(path, class_name, update_symbol):
    """在 `bpy.props.BoolProperty` 被替换成记录器后，真实求值该属性定义。

    Returns:
        (kwargs_dict, update_sentinel, lineno)
    """
    source = _read_source(path)
    tree = ast.parse(source, filename=str(path))
    sentinel = object()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for stmt in node.body:
            if (
                isinstance(stmt, ast.AnnAssign)
                and getattr(stmt.target, "id", "") == "direct_export_mode"
            ):
                recorded = {}

                def _record(*_args, **kwargs):
                    recorded.update(kwargs)
                    return recorded

                namespace = {
                    "bpy": types.SimpleNamespace(props=types.SimpleNamespace(BoolProperty=_record)),
                    update_symbol: sentinel,
                }
                # Blender 的惯用写法是 `prop: bpy.props.XxxProperty(...)`（属性定义在**注解**里，
                # 没有 `=` 赋值）⇒ value 为 None，必须回退到 annotation。
                definition_expr = stmt.value if stmt.value is not None else stmt.annotation
                value_source = _source_segment(source, definition_expr)
                if value_source is None or not value_source.startswith("bpy.props."):
                    raise AssertionError(
                        f"取不到 {path.name}:{stmt.lineno} 的属性表达式源码: {value_source!r}"
                    )
                eval(compile(value_source, str(path), "eval"), namespace)
                return recorded, sentinel, stmt.lineno
        raise AssertionError(f"{path.name} 的类 {class_name} 里找不到 direct_export_mode 定义")
    raise AssertionError(f"{path} 里找不到类 {class_name}")


def _iter_classic_shapekey_emitter_calls(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "add_shapekey_ini_sections":
            yield node
        elif isinstance(func, ast.Name) and func.id == "add_shapekey_ini_sections":
            yield node


class ShapeKeyDirectExportDefaultTests(unittest.TestCase):
    """① 形态键节点的直出开关默认勾选。"""

    def test_direct_export_mode_default_is_true(self):
        recorded, sentinel, lineno = _eval_direct_export_mode_property(
            SHAPEKEY_NODE_PATH,
            "SSMTNode_PostProcess_ShapeKey",
            "sync_shapekey_direct_mode",
        )

        self.assertIs(
            recorded.get("default"),
            True,
            f"默认值来自 {SHAPEKEY_NODE_PATH.relative_to(REPO_ROOT)}:{lineno}",
        )
        # 同步回调必须还在（否则新建/切换时同蓝图节点会不同步）
        self.assertIs(recorded.get("update"), sentinel)
        self.assertGreater(lineno, 0)

    def test_direct_export_mode_default_line_is_reported_for_the_record(self):
        """把「默认值来自哪一行」钉成断言，避免报告与代码漂移。"""
        _, _, lineno = _eval_direct_export_mode_property(
            SHAPEKEY_NODE_PATH,
            "SSMTNode_PostProcess_ShapeKey",
            "sync_shapekey_direct_mode",
        )
        source_lines = SHAPEKEY_NODE_PATH.read_text(encoding="utf-8-sig").splitlines()
        window = "\n".join(source_lines[lineno - 1 : lineno + 8])
        self.assertIn("direct_export_mode", window)
        self.assertIn("default=True", window)

    def test_multifile_direct_export_mode_keeps_false_out_of_scope(self):
        """本次裁定只针对形态键导出；多文件节点默认保持不变（记录为未做项）。"""
        recorded, sentinel, _ = _eval_direct_export_mode_property(
            MULTIFILE_NODE_PATH,
            "SSMTNode_MultiFile_Export",
            "sync_multifile_direct_mode",
        )

        self.assertIs(recorded.get("default"), False)
        self.assertIs(recorded.get("update"), sentinel)

    def test_direct_export_mode_property_is_defined_in_exactly_two_nodes(self):
        definitions = []
        for path, tree in _repo_trees():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.AnnAssign)
                    and getattr(node.target, "id", "") == "direct_export_mode"
                ):
                    definitions.append(path.relative_to(REPO_ROOT).as_posix())

        self.assertEqual(
            sorted(definitions),
            [
                "blueprint/node_multifile_export.py",
                "blueprint/node_postprocess_shapekey.py",
            ],
        )

    def test_no_module_shadows_direct_export_mode_with_a_literal(self):
        """除属性定义外，任何地方都不得用字面量赋值覆盖直出开关。"""
        # #92 原生表情：导出管线在检测到原生表情键时自动挂一个已开启直出的
        # 形态键节点。这两个位置是白名单（有注释说明），其它模块仍不得硬编码。
        allowed = {
            "blueprint/direct_export.py",
            "blueprint/export_background.py",
        }
        offenders = []
        for path, tree in _repo_trees():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "direct_export_mode"
                        and isinstance(node.value, ast.Constant)
                        and path.relative_to(REPO_ROOT).as_posix() not in allowed
                    ):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

        self.assertEqual(offenders, [])

    def test_readers_of_direct_export_mode_are_still_getattr_based(self):
        """直出判定必须读活属性（getattr 默认 False），不得改成硬编码常量。"""
        tree = _parse(DIRECT_EXPORT_PATH)
        attribute_nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "direct_export_mode"
        ]
        self.assertTrue(attribute_nodes)

        getattr_reads = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
                continue
            if len(node.args) >= 2 and (
                isinstance(node.args[1], ast.Constant) and node.args[1].value == "direct_export_mode"
            ):
                getattr_reads += 1

        self.assertGreaterEqual(getattr_reads, 5, "direct_export.py 的直出判定读点不应减少")


class ClassicShapeKeyEmitterLegacyTests(unittest.TestCase):
    """③ 经典（非直出）发射器：ZZMI 侧显式 legacy 标注 + 全局非死代码的双证。"""

    def test_zzmi_has_exactly_one_classic_shapekey_emitter_call(self):
        calls = list(_iter_classic_shapekey_emitter_calls(_parse(ZZMI_PATH)))
        self.assertEqual(len(calls), 1, f"zzmi.py 的经典发射器调用点={[c.lineno for c in calls]}")

    def test_zzmi_classic_emitter_call_is_marked_legacy_and_never_fires(self):
        source = _read_source(ZZMI_PATH)
        call = next(iter(_iter_classic_shapekey_emitter_calls(_parse(ZZMI_PATH))))
        lines = source.splitlines()
        # 调用点前 20 行内的注释块必须自带 legacy 结论与出处
        comment_window = "\n".join(lines[max(call.lineno - 20, 0) : call.lineno - 1])

        self.assertIn("legacy", comment_window)
        self.assertIn("never-fires-on-ZZMI", comment_window)
        self.assertIn("t82-shapekey-drag-retest.md", comment_window)

    def test_zzmi_never_emits_classic_shapekey_sections_itself(self):
        """ZZMI 自身不发射 `CustomShaderComputeShapes`/$shapekey* —— 经典段只可能来自发射器。

        用 AST 常量而不是源码文本：注释里提到这些名字（本轮新增的 legacy 说明）不算发射。
        """
        emission_markers = (
            "CustomShaderComputeShapes",
            "$shapekey_first_run",
            "shapekeyname_mkey_dict",
        )
        found = sorted(
            {
                node.value
                for node in ast.walk(_parse(ZZMI_PATH))
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in emission_markers
            }
        )
        self.assertEqual(found, [], f"zzmi.py 出现了经典形态键发射字面量: {found}")

    def test_classic_emitter_is_not_dead_code_globally(self):
        """其它游戏族的导出器仍在调用 ⇒ 只标注、不删除。"""
        callers = {}
        for path, tree in _repo_trees():
            relative = path.relative_to(REPO_ROOT)
            if relative.parts and relative.parts[0] == "tests":
                continue
            calls = list(_iter_classic_shapekey_emitter_calls(tree))
            if calls:
                callers[relative.as_posix()] = len(calls)

        expected_callers = {
            "ui/universal/unity.py",
            "ui/universal/gimi.py",
            "ui/universal/srmi.py",
            "ui/universal/identityv.py",
            "ui/universal/yysls.py",
            "ui/universal/snowbreak.py",
            "ui/universal/zzmidx12.py",
            "ui/universal/zzmi.py",
        }
        self.assertTrue(
            expected_callers.issubset(set(callers)),
            f"缺少调用点: {sorted(expected_callers - set(callers))}；实际={callers}",
        )
        self.assertGreaterEqual(sum(callers.values()), 9, callers)

    def test_classic_emitter_definition_keeps_legacy_docstring(self):
        for node in ast.walk(_parse(M_INI_HELPER_PATH)):
            if isinstance(node, ast.ClassDef) and node.name == "M_IniHelper":
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.FunctionDef)
                        and stmt.name == "add_shapekey_ini_sections"
                    ):
                        docstring = ast.get_docstring(stmt) or ""
                        self.assertIn("legacy", docstring)
                        self.assertIn("从不产出", docstring)
                        return
        self.fail("common/m_ini_helper.py 里找不到 add_shapekey_ini_sections 定义")

    def test_diagnostic_call_precedes_the_silent_empty_return(self):
        """R-A 的落点必须在 `shapekeyname_mkey_dict` 空返回**之前**。"""
        source = _read_source(M_INI_HELPER_PATH)
        tree = _parse(M_INI_HELPER_PATH)

        diagnostic_lineno = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "add_shapekey_ini_sections":
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and (
                    isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "_report_standard_route_baked_shapekeys"
                ):
                    diagnostic_lineno = inner.lineno
            break

        self.assertIsNotNone(diagnostic_lineno, "缺少 _report_standard_route_baked_shapekeys 调用")
        empty_return = re.search(r"if len\(shapekeyname_mkey_dict\.keys\(\)\) == 0:", source)
        self.assertIsNotNone(empty_return)
        empty_return_lineno = source[: empty_return.start()].count("\n") + 1
        self.assertLess(diagnostic_lineno, empty_return_lineno)


if __name__ == "__main__":
    unittest.main()

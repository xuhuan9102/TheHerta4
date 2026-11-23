"""Check the exported Glow FX parameters across draw calls and node instances."""

import difflib
import os
import re
import tempfile
import unittest
from pathlib import Path

from test_node_postprocess_rabbitfx_pro import (
    _fake_object,
    _fake_tree,
    _item,
    _load_glowfx_module,
    _make_glowfx_node,
    material,
)


SECTION = "TextureOverride_GlowTest"
HASH = "fd054d1d"
OBJECT_FMT = "LOD0.fd054d1d-30030-0.{prefix}{index:02d}"


def _marker(prefix, index):
    return f"; MaterialGlow {prefix}{index:02d}"


def _record(prefix, index):
    return {
        "object": OBJECT_FMT.format(prefix=prefix, index=index),
        "type": "Glowmap",
        "marker": _marker(prefix, index),
        "section": f"[{SECTION}]",
        "lines": [rf"Resource\RabbitFX\Glowmap = ref Resource_Glowmap_{prefix}{index:02d}"],
    }


def _unit_lines(prefix, index, efmi=False):
    object_name = OBJECT_FMT.format(prefix=prefix, index=index)
    lines = [f"; [mesh:{object_name}]", _marker(prefix, index)]
    if efmi:
        lines.extend([
            r"Resource\EFMITTL\Mask = ref Resource_TTLMap_Silk",
            r"$\EFMITTL\alpha = 0.6",
            r"$\EFMITTL\count = 52688",
            r"$\EFMITTL\first = 10",
            r"run = CommandList\EFMITTL\Draw",
        ])
    else:
        lines.append(f"drawindexed = {100 + index},0,0")
    return lines


def _build_ini(groups=(("Body", 10),), efmi=False):
    """groups=[(prefix, count), ...] 依次排进同一个 TextureOverride 段。"""
    lines = ["[Constants]", "global $active0 = 1", "", f"[{SECTION}]", f"hash = {HASH}"]
    records = []
    for prefix, count in groups:
        for index in range(count):
            lines.extend(_unit_lines(prefix, index, efmi=efmi))
            records.append(_record(prefix, index))
    lines.append("")
    return "\n".join(lines), records


def _param_block(text):
    """所有 ; === GlowFX Params === 块（不含 Call/Reset 块）。"""
    blocks = []
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "; === GlowFX Params ===":
            current = []
        elif stripped == "; === End GlowFX Params ===" and current is not None:
            blocks.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def _unit_blocks(text):
    """按 ; [mesh:...] 切出的绘制单元文本（到下一个 mesh 或文件尾）。"""
    blocks = []
    current_name = None
    current = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("; [mesh:"):
            if current_name is not None:
                blocks.append((current_name, "\n".join(current)))
            current_name = stripped[len("; [mesh:"):-1]
            current = []
        elif stripped.startswith("[") and stripped.endswith("]"):
            if current_name is not None:
                blocks.append((current_name, "\n".join(current)))
            current_name = None
            current = []
        elif current_name is not None:
            current.append(line)
    if current_name is not None:
        blocks.append((current_name, "\n".join(current)))
    return blocks


class GlowFXParamTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.export_dir = os.path.join(self._temp.name, "mod")
        os.makedirs(self.export_dir)
        self._outputs_backup = dict(material._rabbitfx_outputs)
        material._rabbitfx_outputs.clear()
        self.addCleanup(self._restore_outputs)

    def _restore_outputs(self):
        material._rabbitfx_outputs.clear()
        material._rabbitfx_outputs.update(self._outputs_backup)

    def _write(self, text):
        path = os.path.join(self.export_dir, "mod.ini")
        Path(path).write_text(text, encoding="utf-8")
        return path

    def _read(self):
        return Path(self.export_dir, "mod.ini").read_text(encoding="utf-8")

    def _register(self, records):
        material._rabbitfx_outputs[
            os.path.normcase(os.path.abspath(os.path.join(self.export_dir, "mod.ini")))
        ] = records

    def _run(self, node, records):
        self._register(records)
        node.execute_postprocess(self.export_dir)

    def _tag(self, node):
        return node._var_tag()

    def test_single_node_ten_objects_declares_each_param_once(self):
        text, records = _build_ini()
        self._write(text)
        node = _make_glowfx_node(name="GlowFX", target_items=[_item(_fake_object(r["object"])) for r in records])
        self._run(node, records)
        result = self._read()
        tag = self._tag(node)

        for param in ("h", "s", "v", "brightness", "interpolate"):
            pattern = rf"^\s*global\s+\$glowfx_{re.escape(tag)}_{param}\s*="
            self.assertEqual(
                len(re.findall(pattern, result, re.MULTILINE)), 1,
                f"{param} 声明必须恰好 1 条",
            )
            self.assertEqual(
                result.count(f"$glowfx_{param} = $glowfx_{tag}_{param}"), 10,
                f"{param} 的 10 段 draw 都必须只做变量 = 变量",
            )

        # Draw parameters reference the declarations. Reset blocks may write literals.
        inline = re.compile(
            r"\$glowfx_(h|s|v|brightness|interpolate)\s*=\s*[-+]?\d"
        )
        for block in _param_block(result):
            self.assertIsNone(inline.search(block), block)
        # 绘制单元里去掉复位块后，也不允许再有内联参数赋值。
        unit_text = "\n".join(block for _name, block in _unit_blocks(result))
        unit_text = re.sub(
            r"; === GlowFX Reset ===.*?; === End GlowFX Reset ===", "",
            unit_text, flags=re.DOTALL,
        )
        self.assertIsNone(inline.search(unit_text))

        # The draw range remains specific to each object.
        for index in range(10):
            self.assertIn(f"$glowfx_drawcount = {100 + index}", result)
        self.assertEqual(
            sum(block.count("$glowfx_drawstart = 0") for _name, block in _unit_blocks(result)),
            10,
        )

        # 非 EFMI 路径不生成 only/radius/normalization。
        for param in ("only", "radius", "normalization"):
            self.assertNotIn(f"$glowfx_{tag}_{param}", result)

    def test_efmi_only_radius_normalization_are_variables(self):
        text, records = _build_ini(groups=(("Efmi", 1),), efmi=True)
        self._write(text)
        node = _make_glowfx_node(
            name="GlowFX", emission_only=True, halo_radius=18,
            target_items=[_item(_fake_object(r["object"])) for r in records],
        )
        self._run(node, records)
        result = self._read()
        tag = self._tag(node)

        expected = {
            "only": "1",
            "radius": "18",
            "normalization": f"{_load_glowfx_module()._halo_normalization(18):.8f}",
        }
        for param, value in expected.items():
            decl = rf"^\s*global\s+\$glowfx_{re.escape(tag)}_{param}\s*=\s*{re.escape(value)}\s*$"
            self.assertEqual(len(re.findall(decl, result, re.MULTILINE)), 1)
            self.assertIn(
                rf"$\EFMITTL\glowfx_{param} = $glowfx_{tag}_{param}", result
            )
        self.assertNotRegex(
            result, r"\\EFMITTL\\glowfx_(only|radius|normalization)\s*=\s*[-+]?\d"
        )

    def test_two_nodes_keep_their_own_brightness_group(self):
        text, records = _build_ini(groups=(("A", 10), ("B", 10)))
        self._write(text)
        first = _make_glowfx_node(
            name="GlowFX", glow_brightness=8,
            target_items=[_item(_fake_object(r["object"])) for r in records[:10]],
        )
        second = _make_glowfx_node(
            name="GlowFX.001", glow_brightness=5,
            target_items=[_item(_fake_object(r["object"])) for r in records[10:]],
        )
        tree = _fake_tree([first, second])
        first.id_data = second.id_data = tree
        self._run(first, records)
        self._run(second, records)
        result = self._read()
        tag_a, tag_b = self._tag(first), self._tag(second)
        self.assertNotEqual(tag_a, tag_b)

        declarations = re.findall(
            r"^\s*global\s+\$glowfx_[a-z0-9_]+_brightness\s*=\s*([0-9.]+)\s*$",
            result, re.MULTILINE,
        )
        self.assertEqual(sorted(declarations), ["5.0", "8.0"])
        self.assertEqual(len(declarations), 2)

        blocks = dict(_unit_blocks(result))
        for prefix, tag in (("A", tag_a), ("B", tag_b)):
            for index in range(10):
                name = OBJECT_FMT.format(prefix=prefix, index=index)
                block = blocks[name]
                self.assertIn(f"$glowfx_brightness = $glowfx_{tag}_brightness", block)
                other = tag_b if tag == tag_a else tag_a
                self.assertNotIn(f"$glowfx_{other}_brightness", block)
        self.assertEqual(result.count(f"$glowfx_{tag_a}_brightness"), 11)
        self.assertEqual(result.count(f"$glowfx_{tag_b}_brightness"), 11)

    def test_generated_variable_names_are_safe(self):
        text, records = _build_ini(groups=(("Bad", 1),))
        self._write(text)
        node = _make_glowfx_node(
            name="GlowFX", buffer_tag="Bad-Tag.1 中文",
            target_items=[_item(_fake_object(r["object"])) for r in records],
        )
        self._run(node, records)
        result = self._read()
        tag = self._tag(node)
        self.assertEqual(tag, "bad_tag_1")
        self.assertIn(f"$glowfx_{tag}_brightness", result)

        names = re.findall(r"\$glowfx_[A-Za-z0-9_]+", result)
        self.assertTrue(names)
        for name in names:
            self.assertRegex(name, r"^\$?[A-Za-z0-9_]+$")
            self.assertNotIn(" ", name)
            self.assertNotIn(".", name)
            self.assertNotIn("-", name)
            self.assertIsNone(re.search(r"[^\x00-\x7f]", name))
        self.assertNotRegex(result, r"\$glowfx_[^ \r\n]*[.\-\u4e00-\u9fff]")

    def test_brightness_change_only_touches_declaration(self):
        text, records = _build_ini()
        self._write(text)
        node = _make_glowfx_node(
            name="GlowFX", glow_brightness=8,
            target_items=[_item(_fake_object(r["object"])) for r in records],
        )
        self._run(node, records)
        before = self._read()
        node.glow_brightness = 5
        self._run(node, records)
        after = self._read()

        diff = [
            line for line in difflib.ndiff(before.splitlines(), after.splitlines())
            if not line.startswith("  ") and not line.startswith("? ")
        ]
        self.assertTrue(diff, "改亮度必须至少改一行")
        tag = self._tag(node)
        declaration = f"global $glowfx_{tag}_brightness"
        changed_declarations = [line for line in diff if declaration in line and line[0] in "+-"]
        changed_others = [line for line in diff if declaration not in line and line[0] in "+-"]
        self.assertEqual(len(changed_others), 0, "\n".join(changed_others))
        self.assertEqual(len(changed_declarations), 2, "\n".join(diff))  # - 8.0 / + 5.0

    def test_glow_disabled_keeps_legacy_output(self):
        text, records = _build_ini()
        self._write(text)
        disabled = _make_glowfx_node(
            name="GlowFX", enable_glow=False,
            target_items=[_item(_fake_object(r["object"])) for r in records],
        )
        self._run(disabled, records)
        disabled_text = self._read()
        tag = self._tag(disabled)
        # 关闭时不得引入任何新的 tagged 声明/参数赋值。
        self.assertNotIn(f"$glowfx_{tag}_", disabled_text)
        self.assertTrue(
            all(not block.strip() for block in _param_block(disabled_text)),
            _param_block(disabled_text),
        )

        # The same input produces a brightness declaration when enabled.
        enabled_dir = os.path.join(self._temp.name, "enabled")
        os.makedirs(enabled_dir)
        Path(enabled_dir, "mod.ini").write_text(text, encoding="utf-8")
        material._rabbitfx_outputs.clear()
        material._rabbitfx_outputs[
            os.path.normcase(os.path.abspath(os.path.join(enabled_dir, "mod.ini")))
        ] = records
        enabled = _make_glowfx_node(
            name="GlowFX", target_items=[_item(_fake_object(r["object"])) for r in records],
        )
        enabled.execute_postprocess(enabled_dir)
        enabled_text = Path(enabled_dir, "mod.ini").read_text(encoding="utf-8")
        self.assertIn(f"global $glowfx_{tag}_brightness = 8.0", enabled_text)

    def test_second_export_does_not_duplicate_declarations(self):
        text, records = _build_ini()
        self._write(text)
        node = _make_glowfx_node(name="GlowFX", target_items=[_item(_fake_object(r["object"])) for r in records])
        self._run(node, records)
        first = self._read()
        tag = self._tag(node)
        pattern = rf"^\s*global\s+\$glowfx_{re.escape(tag)}_(h|s|v|brightness|interpolate)\s*="
        first_count = len(re.findall(pattern, first, re.MULTILINE))
        self.assertEqual(first_count, 5)

        self._run(node, records)
        second = self._read()
        second_count = len(re.findall(pattern, second, re.MULTILINE))
        self.assertEqual(second_count, first_count)
        self.assertEqual(second, first)


if __name__ == "__main__":
    unittest.main()

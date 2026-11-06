# -*- coding: utf-8 -*-
"""blueprint/flow_light_core.py 的纯函数测试。

不依赖 bpy，普通 Python 环境下 `python -m pytest tests/test_flow_light_core.py -q` 即可跑。
"""

import importlib.util
import sys
import unittest
from collections import OrderedDict
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "blueprint" / "flow_light_core.py"
_module_spec = importlib.util.spec_from_file_location("flow_light_core", _MODULE_PATH)
flow_light_core = importlib.util.module_from_spec(_module_spec)
sys.modules["flow_light_core"] = flow_light_core
_module_spec.loader.exec_module(flow_light_core)

PARAMS = flow_light_core.DEFAULT_PARAMS


class ShaderConstantsTests(unittest.TestCase):
    def test_required_macros_present(self):
        constants = flow_light_core.shader_constants(PARAMS)
        self.assertEqual(set(constants), {"FL_" + key.upper() for key in PARAMS})
        self.assertAlmostEqual(constants["FL_SCAN_SPEED"], 0.0405, places=12)

    def test_values_round_trip_through_define_text(self):
        constants = flow_light_core.shader_constants(PARAMS)
        text = "\n".join("#define %s 0" % name for name in constants) + "\n"
        injected = flow_light_core.inject_shader_constants(text, constants)
        for line in injected.splitlines():
            name, raw_value = line.split(" ", 2)[1:]
            self.assertAlmostEqual(float(raw_value), constants[name], places=12)

    def test_inject_replaces_known_macros_and_keeps_others(self):
        text = "\n".join([
            "// FL_SCAN_SPEED 注释不该被动",
            "#define FL_SCAN_SPEED 0.1",
            "#define FL_RIM_POWER 1",
            "#define FL_NOT_IN_DICT 7",
            "float3 main_ps() { return 0; }",
        ])
        constants = {"FL_SCAN_SPEED": 0.0405, "FL_RIM_POWER": 4.0}
        injected = flow_light_core.inject_shader_constants(text, constants)
        self.assertIn("#define FL_SCAN_SPEED 0.0405", injected)
        self.assertIn("#define FL_RIM_POWER 4", injected)
        self.assertIn("#define FL_NOT_IN_DICT 7", injected)
        self.assertIn("// FL_SCAN_SPEED 注释不该被动", injected)
        self.assertEqual(len(injected.splitlines()), len(text.splitlines()))

    def test_inject_is_idempotent(self):
        text = "#define FL_SCAN_SPEED 0.1\n#define FL_INTENSITY 1\n"
        constants = flow_light_core.shader_constants(PARAMS)
        once = flow_light_core.inject_shader_constants(text, constants)
        twice = flow_light_core.inject_shader_constants(once, constants)
        self.assertEqual(once, twice)


class DrawTargetTests(unittest.TestCase):
    def test_parse_draw_targets(self):
        sections = OrderedDict()
        sections["[TextureOverride_body]"] = [
            "hash = 1234abcd",
            "if $swap_var == 1",
            "[mesh:Body]",
            "drawindexed = 10, 0, 0",
            "endif",
        ]
        targets = flow_light_core.parse_draw_targets(sections)
        self.assertEqual(len(targets), 1)
        draw = targets[0]
        self.assertEqual(draw["section_name"], "[TextureOverride_body]")
        self.assertEqual(draw["mesh_name"], "Body")
        self.assertEqual(draw["draw_type"], "drawindexed")
        self.assertEqual(draw["draw_params"], (10, 0, 0))
        self.assertEqual(draw["run_path"], [])
        self.assertEqual(
            sections["[TextureOverride_body]"][draw["line_index"]],
            "drawindexed = 10, 0, 0",
        )

    def test_parse_draw_targets_follows_run_chain(self):
        sections = OrderedDict()
        sections["[TextureOverride_body]"] = [
            "hash = 1234abcd",
            "[mesh:Body]",
            "run = CommandList_FLDraw",
        ]
        sections["[CommandList_FLDraw]"] = ["draw = 6, 0"]
        targets = flow_light_core.parse_draw_targets(sections)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["draw_type"], "draw")
        self.assertEqual(targets[0]["draw_params"], (6, 0))
        self.assertEqual(targets[0]["line_index"], 0)
        self.assertEqual(targets[0]["run_path"], [("[TextureOverride_body]", 2)])


class PurityTests(unittest.TestCase):
    def test_module_does_not_import_bpy(self):
        source = _MODULE_PATH.read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            self.assertNotIn(stripped, ("import bpy", "from bpy import *"), "纯函数层不许碰 bpy")
            self.assertFalse(stripped.startswith("import bpy"), "纯函数层不许碰 bpy")


if __name__ == "__main__":
    unittest.main()

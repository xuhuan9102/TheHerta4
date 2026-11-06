"""Native draw contracts: isolate surface lighting from screen/outline passes."""
import importlib.util
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('flow_light_material', ROOT / 'blueprint/flow_light_material.py')
material = importlib.util.module_from_spec(spec)
spec.loader.exec_module(material)


class MaterialProfileTests(unittest.TestCase):


    def test_gate_is_per_tag_and_restores_borrowed_parameters(self):
        self.assertNotEqual(material.material_magic('one'), material.material_magic('two'))
        lines = material.overlay_scope('one', 0)
        self.assertIn('    x244 = %d' % material.material_magic('one'), lines)
        self.assertIn('    x244 = $fl_one_0_x', lines)
        self.assertIn('    y244 = $fl_one_0_y', lines)

    @unittest.skipUnless(os.name == 'nt', 'Windows SM5 compiler')
    def test_all_modes_keep_native_input_registers_and_four_mrt_outputs(self):
        source = (ROOT / 'Toolset/flow_light/material.hlsl').read_text()
        for mode in range(3):
            for axis in range(3):
                text = source.replace('#define FL_EFFECT_MODE 2', f'#define FL_EFFECT_MODE {mode}')
                text = text.replace('#define FL_SCAN_AXIS 2', f'#define FL_SCAN_AXIS {axis}')
                bytecode, assembly = material.compile_overlay(text)
                self.assertTrue(bytecode.startswith(b'DXBC'))
                self.assertIn('dcl_output o3.xyzw', assembly)

    def test_exported_overlay_uses_hash_lookup_without_global_shader_patch(self):
        sections = material.build_overlay_sections('one', [{'draw_type': 'drawindexed', 'draw_params': (12, 3, 0)}])
        self.assertFalse(any(name.startswith('[ShaderRegex') for name in sections))
        normal = sections['[ShaderOverrideFLMaterialPass_one]']
        self.assertIn('hash = ' + material.ZZZ_NORMAL_VERTEX_SHADER, normal)
        self.assertFalse(any('filter_index' in line for line in normal))
        overlay = sections['[CustomShaderFLMaterial_one_0]']
        self.assertIn('depth_func = equal', overlay)
        self.assertIn('drawindexed = 12,3,0', overlay)
        self.assertFalse(any(line.startswith(('vs =', 'o0 =', 'od =')) for line in overlay))


if __name__ == '__main__':
    unittest.main()

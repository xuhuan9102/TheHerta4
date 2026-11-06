"""流光描边节点（node_postprocess_flow_light）的 fake-bpy 集成测试。

套路照 tests/test_node_postprocess_uv_offset.py：造空 __path__ 的假包 + fake bpy，
真 safe_write / object_prefix_helper 按假包前缀注册，被测模块用 spec_from_file_location
加载。flow_light_core 是纯 Python 叶子模块，直接按路径加载后登记成
``{PKG}.blueprint.flow_light_core``，节点里的 ``from . import flow_light_core`` 就命中它。

模板目录用 monkeypatch 指到临时目录：绝不往仓库 Toolset/flow_light 写测试夹具。
"""

import importlib.util
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tests import _real_modules


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_node_postprocess_flow_light_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []

# 真 common/safe_write.py 与 utils/*（含 format_utils）按假包前缀登记
_real_modules.register_real_common_modules(f"{PKG}.common")


class _FakeItem:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeCollection(list):
    def add(self):
        item = _FakeItem()
        self.append(item)
        return item

    def remove(self, index):
        del self[index]


_fake_bpy = _install_module("bpy", **{
    "types": types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object),
    "props": types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        FloatProperty=lambda **_kwargs: None,
        EnumProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
    "data": types.SimpleNamespace(objects={}),
    "utils": types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
})
_install_module("bpy.types", PropertyGroup=object, Operator=object, UIList=object, NodeSocket=object)

# 基类桩：只提供本节点用到的三个钩子（真实现见 blueprint/node_postprocess_base.py）
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "split_anim_driver_block_content": staticmethod(lambda content: ("", content)),
            "split_auto_appended_tail_content": staticmethod(lambda content: (content, "")),
            "_create_cumulative_backup": lambda self, ini_file_path, mod_export_path: None,
        },
    ),
)


def _load_real_module(module_name, relative_path):
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# object_prefix_helper 依赖 ..utils.ssmt_error_utils → .format_utils（后者已由 _real_modules 登记）
_load_real_module(f"{PKG}.utils.ssmt_error_utils", "utils/ssmt_error_utils.py")
_object_prefix_helper_module = _load_real_module(
    f"{PKG}.common.object_prefix_helper",
    "common/object_prefix_helper.py",
)

# 纯函数层：直接按路径加载，再登记成节点模块相对 import 时要找的那个名字
_core_module = _load_real_module(f"{PKG}.blueprint.flow_light_core", "blueprint/flow_light_core.py")
setattr(sys.modules[f"{PKG}.blueprint"], "flow_light_core", _core_module)

_material_module = _load_real_module(f"{PKG}.blueprint.flow_light_material", "blueprint/flow_light_material.py")
setattr(sys.modules[f"{PKG}.blueprint"], "flow_light_material", _material_module)

module = _load_real_module(
    f"{PKG}.blueprint.node_postprocess_flow_light",
    "blueprint/node_postprocess_flow_light.py",
)

MESH_NAME = "LOD0.a913e9a9-56682-0.Mesh"
OBJECT_NAME = "LOD0.a913e9a9-56682-0.Mesh"
TEXOVERRIDE_SECTION = "[TextureOverride_LOD0.a913e9a9_56682_0]"

@unittest.skipUnless(os.name == "nt", "Windows SM5 compiler")
class FlowLightNodeTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.mod_dir = self._tmp_dir.name
        self.ini_path = os.path.join(self.mod_dir, "mod.ini")
        self._write_sample_mod()


        self.toolset_dir = os.path.join(self.mod_dir, "_toolset")
        self._write_template()

        node_class = module.SSMTNode_PostProcess_FlowLight
        original_toolset_dir = node_class.__dict__["_toolset_dir"]
        node_class._toolset_dir = classmethod(lambda cls: self.toolset_dir)
        self.addCleanup(setattr, node_class, "_toolset_dir", original_toolset_dir)


    def _write_sample_mod(self):
        meshes_dir = os.path.join(self.mod_dir, "Meshes0000")
        os.makedirs(meshes_dir, exist_ok=True)
        with open(os.path.join(meshes_dir, "LOD0.a913e9a9-56682-0-Index.buf"), "wb") as file_obj:
            file_obj.write(struct.pack("<10I", *range(10)))

        ini_text = (
            "[Constants]\n"
            "global $active0 = 1\n"
            "\n"
            f"{TEXOVERRIDE_SECTION}\n"
            "hash = a913e9a9\n"
            "ib = Resource_LOD0.a913e9a9_56682_0_Index\n"
            "\n"
            f"; [mesh:{MESH_NAME}]\n"
            "drawindexed = 10,0,0\n"
            "\n"
            "[Present]\n"
            "\n"
            "[Resource_LOD0.a913e9a9_56682_0_Index]\n"
            "type = Buffer\n"
            "format = DXGI_FORMAT_R32_UINT\n"
            "filename = Meshes0000/LOD0.a913e9a9-56682-0-Index.buf\n"
        )
        with open(self.ini_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(ini_text)


    def _make_node(self, object_names=(OBJECT_NAME,), tag="flowlight", key_toggle=""):
        node = module.SSMTNode_PostProcess_FlowLight()
        node.flow_objects = _FakeCollection()
        for object_name in object_names:
            node.flow_objects.add().object_name = object_name
        if tag is not None:
            node.tag = tag
        # fake bpy 的 StringProperty 返回 None，不会生成真实属性；显式设上，
        # 默认值与真 Blender 里的 default='' 保持一致。
        node.key_toggle = key_toggle
        return node


    def _read_ini_text(self):
        with open(self.ini_path, "r", encoding="utf-8") as file_obj:
            return file_obj.read()


    def _section_lines(self, section_name):
        """取某个段的行（到下一个段头为止），段不存在返回 None。"""
        lines = self._read_ini_text().splitlines()
        try:
            start = lines.index(section_name)
        except ValueError:
            return None
        collected = []
        for line in lines[start + 1:]:
            if line.startswith("[") and line.endswith("]"):
                break
            collected.append(line)
        return collected


    def test_second_run_is_byte_identical(self):
        node = self._make_node()
        node.execute_postprocess(self.mod_dir)
        first_text = self._read_ini_text()
        first_mtime = os.stat(self.ini_path).st_mtime_ns

        node.execute_postprocess(self.mod_dir)
        second_text = self._read_ini_text()

        self.assertEqual(first_text, second_text)
        self.assertEqual(first_mtime, os.stat(self.ini_path).st_mtime_ns)
        self.assertEqual(second_text.count("; FLOWLIGHT_MATERIAL_POST_BEGIN flowlight"), 1)


    def test_clearing_objects_removes_generated_parts(self):
        node = self._make_node()
        node.execute_postprocess(self.mod_dir)
        self.assertIn("FLMaterial_flowlight", self._read_ini_text())

        cleared_node = self._make_node(object_names=())
        cleared_node.execute_postprocess(self.mod_dir)
        ini_text = self._read_ini_text()

        self.assertNotIn("CustomShaderFLMaterial_flowlight", ini_text)
        self.assertNotIn("CustomShaderFLMaterial_flowlight", ini_text)
        self.assertNotIn("FLOWLIGHT_MATERIAL_POST_BEGIN", ini_text)
        self.assertNotIn("run = CustomShaderFLMaterial", ini_text)
        # 游戏原生 draw 必须留着
        self.assertIn("drawindexed = 10,0,0", ini_text)


    def test_tag_sanitized_for_invalid_characters(self):
        node = self._make_node(tag="my-glow 1")
        node.execute_postprocess(self.mod_dir)
        self.assertIn("my_glow_1", self._read_ini_text())
        self.assertIn("[CustomShaderFLMaterial_my_glow_1_0]", self._read_ini_text())


    def test_idempotent_when_present_and_constants_missing(self):
        """首次执行就新建 [Present]/[Constants] 的导出也必须幂等。

        回归点：这两个段若在 FL 段之后才 setdefault，首轮它们排在文件末尾、次轮却
        排在 FL 段之前，段序一变 ini 就不再逐字节相同（真实导出样例上没有 [Constants]，
        踩过一次）。
        """
        with open(self.ini_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(
                f"{TEXOVERRIDE_SECTION}\n"
                "hash = a913e9a9\n"
                "ib = Resource_LOD0.a913e9a9_56682_0_Index\n"
                "\n"
                f"; [mesh:{MESH_NAME}]\n"
                "drawindexed = 10,0,0\n"
                "\n"
                "[Resource_LOD0.a913e9a9_56682_0_Index]\n"
                "type = Buffer\n"
                "format = DXGI_FORMAT_R32_UINT\n"
                "filename = Meshes0000/LOD0.a913e9a9-56682-0-Index.buf\n"
            )
        node = self._make_node()
        node.execute_postprocess(self.mod_dir)
        first_text = self._read_ini_text()
        first_mtime = os.stat(self.ini_path).st_mtime_ns

        node.execute_postprocess(self.mod_dir)
        second_text = self._read_ini_text()

        self.assertEqual(first_text, second_text)
        self.assertEqual(first_mtime, os.stat(self.ini_path).st_mtime_ns)
        self.assertIn("[Present]", first_text)
        self.assertIn("[Constants]", first_text)
        self.assertIn("global $fl_enable = 1", first_text)
        # 段序：Present/Constants 必须在 FL 资源段之前，否则次轮会被搬到别处
        self.assertLess(first_text.index("[Present]"), first_text.index("[CustomShaderFLMaterial_flowlight_0]"))
        self.assertLess(first_text.index("[Constants]"), first_text.index("[CustomShaderFLMaterial_flowlight_0]"))


    def test_host_ini_selected_when_fluid_probe_ini_exists(self):
        """流体探针共存：目录里多出 fluid_probe.ini 时，只能写角色自己的 ini。

        回归点：旧实现按字母序取 ``ini_files[0]``，``fluid_probe.ini`` 排在中文名之前，
        会被当成宿主；随后整份重写它（重复的 ``[Constants]`` 被折叠 → 流体变量丢失），
        同时流光段也写进了一个没有 mesh draw 的文件 → 两边一起消失。
        """
        fluid_path = os.path.join(self.mod_dir, "fluid_probe.ini")
        fluid_text = (
            "namespace = Fluid_deadbeef\n"
            "[Constants]\n"
            "global $fluid_present = 0\n"
            "global $seen = 0\n"
            "\n"
            "[Constants]\n"
            "global $emitting = 0\n"
            "\n"
            "[Present]\n"
            "post $fluid_present = 0\n"
        )
        with open(fluid_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(fluid_text)
        # 宿主 ini 前面加一段 preamble（真实导出里的注释），验证不会被丢
        host_text = self._read_ini_text()
        with open(self.ini_path, "w", encoding="utf-8") as file_obj:
            file_obj.write("; host preamble\n" + host_text)

        node = self._make_node()
        node.execute_postprocess(self.mod_dir)

        host_after = self._read_ini_text()
        with open(fluid_path, "r", encoding="utf-8") as file_obj:
            fluid_after = file_obj.read()

        self.assertEqual(fluid_after, fluid_text, "流体 ini 必须一个字节都不动")
        self.assertIn("[CustomShaderFLMaterial_flowlight_0]", host_after)
        self.assertIn("global $fluid_present", fluid_after)
        self.assertTrue(host_after.startswith("; host preamble"), "宿主 ini 的 preamble 不能被丢")


    def test_duplicate_constants_sections_are_merged_not_dropped(self):
        """宿主 ini 自己有重复 [Constants] 时，两段内容都要留下（不能后者覆盖前者）。"""
        with open(self.ini_path, "a", encoding="utf-8") as file_obj:
            file_obj.write("\n[Constants]\nglobal $late_flag = 1\n")
        node = self._make_node()
        node.execute_postprocess(self.mod_dir)
        text = self._read_ini_text()
        self.assertIn("global $active0 = 1", text)
        self.assertIn("global $late_flag = 1", text)
        self.assertIn("[CustomShaderFLMaterial_flowlight_0]", text)


    @unittest.skipUnless(os.name == 'nt', 'Windows SM5 compiler')
    def test_native_material_replaces_old_screen_chain_and_is_idempotent(self):
        node = self._make_node()
        # Removed backend values saved in an existing scene cannot select screen rendering.
        node.backend = 'SCREEN'
        text = self._read_ini_text().replace('drawindexed = 10,0,0',
            'drawindexed = 10,0,0\n; FLOWLIGHT_MASK\nif z248 == 0\nclear = ResourceFLMask_flowlight 0.0 0.0 0.0 0.0\nendif\nz248 = 1\nrun = CustomShaderFLMask_flowlight_0')
        text += '\n[CustomShaderFLMask_flowlight_0]\nps = removed.mask.hlsl\n[ResourceFLMask_flowlight]\ntype = Texture2D\n'
        Path(self.ini_path).write_text(text, encoding='utf-8')
        node.execute_postprocess(self.mod_dir)
        first = self._read_ini_text()
        self.assertNotIn('CustomShaderFLMask', first)
        self.assertNotIn('ResourceFLMask', first)
        self.assertNotIn('FLOWLIGHT_BEGIN', first)
        self.assertNotIn('[ShaderRegexFLMaterial_', first)
        self.assertIn('[ShaderOverrideFLMaterialPass_flowlight]', first)
        self.assertIn('[CustomShaderFLMaterial_flowlight_0]', first)
        self.assertEqual(first.count('drawindexed = 10,0,0'), 2)
        self.assertIn('y244 = time', first)
        self.assertIn('x244 = $fl_flowlight_0_x', first)
        self.assertIn('depth_func = equal', first)
        self.assertIn('depth_write_mask = zero', first)
        self.assertNotIn('o0 = set_viewport', first)
        node.execute_postprocess(self.mod_dir)
        self.assertEqual(self._read_ini_text(), first)
        self._make_node(object_names=()).execute_postprocess(self.mod_dir)
        self.assertNotIn('ShaderRegexFLMaterial', self._read_ini_text())
        self.assertNotIn('FLOWLIGHT_MATERIAL_', self._read_ini_text())
        self.assertIn('drawindexed = 10,0,0', self._read_ini_text())


    @unittest.skipUnless(os.name == 'nt', 'Windows SM5 compiler')
    def test_native_material_scopes_shared_command_list_at_call_site(self):
        text = self._read_ini_text().replace('drawindexed = 10,0,0', 'run = CommandListShared')
        text += '\n[CommandListShared]\ndrawindexed = 10,0,0\n'
        Path(self.ini_path).write_text(text, encoding='utf-8')
        node = self._make_node()
        node.execute_postprocess(self.mod_dir)
        caller = '\n'.join(self._section_lines(TEXOVERRIDE_SECTION))
        callee = '\n'.join(self._section_lines('[CommandListShared]'))
        self.assertIn('FLOWLIGHT_MATERIAL_POST_BEGIN', caller)
        self.assertNotIn('FLOWLIGHT_MATERIAL_', callee)
        self.assertIn('run = CommandListShared', caller)


    @unittest.skipUnless(os.name == 'nt', 'Windows SM5 compiler')
    def test_material_missing_template_does_not_rewrite_mod(self):
        node = self._make_node()
        first = Path(self.ini_path).read_bytes()
        os.remove(os.path.join(self.toolset_dir, 'material.hlsl'))
        with self.assertRaises(OSError):
            node.execute_postprocess(self.mod_dir)
        self.assertEqual(Path(self.ini_path).read_bytes(), first)


    def test_material_cleanup_does_not_remove_a_longer_tag(self):
        node = self._make_node()
        sections = {
            '[ShaderRegexFLMaterial_one_3]': [],
            '[ShaderRegexFLMaterial_one_3.Pattern.Replace]': [],
            '[ShaderRegexFLMaterial_one_two_3]': [],
        }
        node._strip_old_flowlight(sections, 'one')
        self.assertEqual(list(sections), ['[ShaderRegexFLMaterial_one_two_3]'])

    def test_screen_migration_preserves_other_tags_and_active_material_cache(self):
        node = self._make_node()
        other_block = ['; FLOWLIGHT_MASK', 'if z248 == 0',
            'clear = ResourceFLMask_one_two 0.0 0.0 0.0 0.0', 'endif',
            'z248 = 1', 'run = CustomShaderFLMask_one_two_0']
        sections = {
            '[TextureOverride_body]': list(other_block),
            '[ResourceFLMask_one]': ['type = Texture2D'],
            '[ResourceFLMask_one_two]': ['type = Texture2D'],
        }
        node._strip_old_flowlight(sections, 'one')
        self.assertNotIn('[ResourceFLMask_one]', sections)
        self.assertIn('[ResourceFLMask_one_two]', sections)
        self.assertEqual(sections['[TextureOverride_body]'], other_block)
        res = Path(self.mod_dir) / 'res/flow_light'
        res.mkdir(parents=True)
        for name in ('one.mask.ps_5_0.8000.bin', 'one.sdf_jfa.hlsl',
                     'one.material.hlsl', 'one.material.ps_5_0.8000.bin', 'one_two.mask.hlsl'):
            (res / name).write_text('fixture')
        node._remove_shader_files(self.mod_dir, 'one', ['one.material.hlsl'])
        self.assertEqual({p.name for p in res.iterdir()},
            {'one.material.hlsl', 'one.material.ps_5_0.8000.bin', 'one_two.mask.hlsl'})


    @unittest.skipUnless(os.name == 'nt', 'Windows SM5 compiler')
    def test_native_effect_needs_character_and_material_pass_in_current_draw(self):
        node = self._make_node()
        node.execute_postprocess(self.mod_dir)
        caller = '\n'.join(self._section_lines(TEXOVERRIDE_SECTION))
        self.assertIn('$fl_character_seen_flowlight = 1', caller)
        self.assertIn('if $fl_enable && $fl_character_seen_flowlight && $fl_material_pass_flowlight', caller)
        self.assertIn('post $fl_character_seen_flowlight = 0', '\n'.join(self._section_lines('[Present]')))
        self.assertIn('post $fl_material_pass_flowlight = 0', '\n'.join(self._section_lines('[ShaderOverrideFLMaterialPass_flowlight]')))
        constants = '\n'.join(self._section_lines('[Constants]'))
        self.assertIn('global $fl_character_seen_flowlight = 0', constants)
        self.assertIn('global $fl_material_pass_flowlight = 0', constants)


    def test_blank_key_toggle_emits_no_section(self):
        """留空 = 不绑键：不生成 [KeyFlowLightToggle]，变量仍声明为 1。"""
        node = self._make_node()
        node.execute_postprocess(self.mod_dir)
        text = self._read_ini_text()

        self.assertEqual(text.count("[KeyFlowLightToggle]"), 0)
        self.assertNotIn("$fl_enable = 1,0", text)
        self.assertIn("global $fl_enable = 1", text)


    def test_custom_toggle_key_and_repeat_export_are_idempotent(self):
        """VK_F9 → 补修饰词、一段；再导逐字节相同；清空后再导段消失。"""
        node = self._make_node(key_toggle="VK_F9")
        node.execute_postprocess(self.mod_dir)
        first_text = self._read_ini_text()
        first_mtime = os.stat(self.ini_path).st_mtime_ns

        lines = [line.strip() for line in self._section_lines("[KeyFlowLightToggle]")]
        self.assertIn("key = no_alt no_ctrl no_shift VK_F9", lines)

        node.execute_postprocess(self.mod_dir)
        second_text = self._read_ini_text()
        self.assertEqual(second_text, first_text)
        self.assertEqual(os.stat(self.ini_path).st_mtime_ns, first_mtime)
        self.assertEqual(second_text.count("[KeyFlowLightToggle]"), 1)

        # 清空面板再导：上一次的段必须消失，变量声明还在。
        node.key_toggle = ""
        node.execute_postprocess(self.mod_dir)
        cleared_text = self._read_ini_text()
        self.assertEqual(cleared_text.count("[KeyFlowLightToggle]"), 0)
        self.assertNotIn("$fl_enable = 1,0", cleared_text)
        self.assertIn("global $fl_enable = 1", cleared_text)

        node.execute_postprocess(self.mod_dir)
        self.assertEqual(self._read_ini_text(), cleared_text)


    def test_fl_enable_declared_once_and_off_seed_preserved(self):
        """$fl_enable 只声明一次、永远不带 persist；宿主预置的关态 0 不被改写。"""
        node = self._make_node()
        node.execute_postprocess(self.mod_dir)
        node.execute_postprocess(self.mod_dir)
        text = self._read_ini_text()
        self.assertEqual(text.count("global $fl_enable"), 1)
        self.assertIn("global $fl_enable = 1", text)
        self.assertNotIn("persist $fl_enable", text)

        # 重新造一份宿主，预置旧写法的关态种子（persist + 0），导两次：
        # 值保留，persist 被摘掉，仍然只有一行。
        self._write_sample_mod()
        seeded = self._read_ini_text().replace(
            "global $active0 = 1",
            "global $active0 = 1\nglobal persist $fl_enable = 0",
        )
        Path(self.ini_path).write_text(seeded, encoding="utf-8")

        node.execute_postprocess(self.mod_dir)
        node.execute_postprocess(self.mod_dir)
        after = self._read_ini_text()
        self.assertEqual(after.count("global $fl_enable"), 1)
        self.assertIn("global $fl_enable = 0", after)
        self.assertNotIn("global $fl_enable = 1", after)
        self.assertNotIn("persist $fl_enable", after)


    def _write_template(self):
        import shutil
        os.makedirs(self.toolset_dir, exist_ok=True)
        shutil.copyfile(REPO_ROOT / 'Toolset/flow_light/material.hlsl', os.path.join(self.toolset_dir, 'material.hlsl'))


class ToggleKeyNormalisationTests(unittest.TestCase):
    """纯函数：不碰 SM5 编译器/ini，非 Windows 也能跑。"""

    def test_blank_returns_empty_string(self):
        for value in ("", "   ", None):
            self.assertEqual(module.normalise_toggle_key(value), "")

    def test_single_token_gets_modifiers(self):
        self.assertEqual(
            module.normalise_toggle_key("  VK_F9  "),
            "no_alt no_ctrl no_shift VK_F9",
        )

    def test_full_spec_is_kept_with_collapsed_whitespace(self):
        self.assertEqual(
            module.normalise_toggle_key("no_alt   VK_F9"),
            "no_alt VK_F9",
        )

    def test_non_string_input_does_not_raise(self):
        self.assertEqual(
            module.normalise_toggle_key(123),
            "no_alt no_ctrl no_shift 123",
        )


if __name__ == "__main__":
    unittest.main()

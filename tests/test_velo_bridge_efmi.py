# -*- coding: utf-8 -*-
import importlib.util
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

# 模拟 Velo 桥接节点在导出期间写入的运行时标记（真实实现见 BlueprintExportHelper）。
_VELO_MARKER = {'game': ''}


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fake_bpy():
    module = types.ModuleType('bpy')
    module.types = types.SimpleNamespace(Node=object, Operator=object, PropertyGroup=object)
    prop = lambda **_kwargs: None
    props = types.ModuleType('bpy.props')
    props.StringProperty = prop
    props.BoolProperty = prop
    props.IntProperty = prop
    props.FloatProperty = prop
    props.CollectionProperty = prop
    # 切换面板新增了枚举（字体/按钮对齐）与颜色（FloatVectorProperty）属性，
    # 桩必须补齐，否则模块级类体求值就会 AttributeError，整个文件加载失败。
    props.EnumProperty = prop
    props.FloatVectorProperty = prop
    module.props = props
    module.path = types.SimpleNamespace(abspath=lambda path: path)
    return module


class _VeloStyleFormatter:
    @staticmethod
    def format_ini_swapvar(name):
        text = str(name or '').replace('$', '').replace('-', ' ').replace('.', ' ').replace('_', ' ')
        parts = (part.strip().lower() for part in text.split(' '))
        return '$swapvar_' + '_'.join(part for part in parts if part and part not in ('var', 'swap'))


class VeloBridgeEFMITests(unittest.TestCase):
    @staticmethod
    def _load_bridge():
        fake_bpy = _fake_bpy()
        with mock.patch.dict(sys.modules, {'bpy': fake_bpy, 'bpy.props': fake_bpy.props}):
            return _load_module(
                '_test_velo_bridge', ROOT / 'TheHerta4_Velo_Bridge' / '__init__.py'
            )

    def test_non_ascii_draw_variables_are_replaced_everywhere(self):
        bridge = self._load_bridge()

        source = (
            'global $draw_component_4_丝袜下身 = 1\n'
            '[CommandListProcessToggles]\n'
            '$draw_component_4_丝袜下身 = ($swapkey0 == 1)\n'
            '[Draw]\n'
            'if $DRAW_COMPONENT_4_丝袜下身\n'
            '    ; Draw object "Component 4 丝袜下身"\n'
            'endif\n'
        )
        result = bridge._normalize_draw_variables(source)
        variables = re.findall(r'\$draw_[a-z_0-9]+', result, re.IGNORECASE)
        self.assertEqual(len({value.casefold() for value in variables}), 1)
        self.assertTrue(all(value.isascii() for value in variables))
        self.assertIn('Component 4 丝袜下身', result)
        self.assertNotIn('$draw_component_4_丝袜下身', result.casefold())

    def test_wwmi_and_efmi_use_their_own_formatter_modules(self):
        bridge = self._load_bridge()
        loaded = []

        def fake_import(module_name):
            loaded.append(module_name)
            return types.SimpleNamespace(TextFormatter=lambda: module_name)

        with mock.patch.object(bridge.importlib, 'import_module', side_effect=fake_import):
            wwmi = bridge._velo_text_formatter('WUTHERING')
            efmi = bridge._velo_text_formatter('ENDFIELD')

        self.assertIn('wuthering_waves._wwmi_core', wwmi)
        self.assertIn('arknights_endfield._efmi_core', efmi)
        self.assertEqual(loaded, [wwmi, efmi])
        with self.assertRaisesRegex(ValueError, '不支持此游戏'):
            bridge._velo_text_formatter('UNKNOWN')

    def test_wwmi_and_efmi_swap_variables_use_exact_blueprint_names(self):
        bridge = self._load_bridge()
        source = (
            '[Constants]\n'
            'global persist $swapvar_legs_outfit = 0\n'
            'global persist $swapvar_swapkey0 = 0\n'
            '[KeySwapLegsOutfit]\n'
            '$swapvar_legs_outfit = 0, 1\n'
            '[CommandListProcessToggles]\n'
            '$draw_body = ($SWAPVAR_LEGS_OUTFIT == 1) && ($swapvar_swapkey0 == 0)\n'
            '[Draw]\n'
            'if $swapvar_legs_outfit_extra == 1\n'
            'endif\n'
        )
        with mock.patch.object(bridge, '_velo_text_formatter', return_value=_VeloStyleFormatter()):
            for game_value in ('WUTHERING', 'ENDFIELD'):
                with self.subTest(game_value=game_value), tempfile.TemporaryDirectory() as folder:
                    ini = Path(folder) / 'mod.ini'
                    ini.write_text(source, encoding='utf-8')
                    bridge._restore_th4_swap_variable_names(
                        ini,
                        {101: 'Legs_Outfit', 102: 'swapkey0'},
                        game_value,
                    )
                    result = ini.read_text(encoding='utf-8')

                    self.assertIn('global persist $Legs_Outfit = 0', result)
                    self.assertIn('global persist $swapkey0 = 0', result)
                    self.assertIn('$Legs_Outfit = 0, 1', result)
                    self.assertIn('($Legs_Outfit == 1) && ($swapkey0 == 0)', result)
                    self.assertIn('$swapvar_legs_outfit_extra == 1', result)
                    self.assertNotIn('$swapvar_swapkey0', result.casefold())

    def test_efmi_swap_variable_format_collision_is_rejected(self):
        bridge = self._load_bridge()
        with tempfile.TemporaryDirectory() as folder:
            ini = Path(folder) / 'mod.ini'
            ini.write_text('global persist $swapvar_outfit = 0\n', encoding='utf-8')
            with mock.patch.object(bridge, '_velo_text_formatter', return_value=_VeloStyleFormatter()):
                with self.assertRaisesRegex(ValueError, '同一个变量'):
                    bridge._restore_th4_swap_variable_names(
                        ini,
                        {101: 'outfit', 102: 'swap_outfit'},
                        'ENDFIELD',
                    )

    def test_explicit_efmi_selection_keeps_all_blueprint_objects(self):
        adapter = _load_module(
            '_test_efmi_selection', ROOT / 'TheHerta4_Velo_Bridge' / 'efmi_selection.py'
        )

        class FakeMerger:
            __dataclass_fields__ = {
                key: object()
                for key in ('collection', 'component_id', 'force_object_name', 'allow_empty_components')
            }

            def import_objects_from_collection(self):
                raise AssertionError('default collection collector was used')

        original = FakeMerger.import_objects_from_collection
        core = types.ModuleType('object_merger')
        core.TempObject = lambda **kwargs: types.SimpleNamespace(**kwargs)
        core.copy_object = lambda _context, obj, **_kwargs: obj
        export_package = types.ModuleType(
            'velo_tools.games.arknights_endfield._efmi_core.blender_export'
        )
        export_package.__path__ = []
        export_package.object_merger = core
        export_module = types.ModuleType(export_package.__name__ + '.blender_export')
        export_module.ObjectMergerEFMI = FakeMerger
        stubs = {
            export_package.__name__: export_package,
            export_module.__name__: export_module,
        }
        prefix = ''
        for part in export_package.__name__.split('.')[:-1]:
            prefix = part if not prefix else prefix + '.' + part
            package = types.ModuleType(prefix)
            package.__path__ = []
            stubs.setdefault(prefix, package)

        collection = object()
        body = types.SimpleNamespace(name='Component 4 Body', velo_mmd_text='mapping')
        neck = types.SimpleNamespace(name='Component 4 Neck')
        component = types.SimpleNamespace(id=4, objects=[])
        merger = types.SimpleNamespace(
            collection=collection,
            force_object_name='',
            component_id=-1,
            extracted_object=types.SimpleNamespace(components=[None] * 5),
            context=None,
            components=[component],
            allow_empty_components=False,
        )
        with mock.patch.dict(sys.modules, stubs):
            with adapter.explicit_efmi_objects(collection, [body, neck]):
                FakeMerger.import_objects_from_collection(merger)

        self.assertEqual([item.name for item in component.objects], [body.name, neck.name])
        self.assertIs(FakeMerger.import_objects_from_collection, original)


def _make_export_helper_stub(package_name):
    """造一个 BlueprintExportHelper 桩：标记值由测试通过 _VELO_MARKER 控制。"""
    module = types.ModuleType(package_name + '.export_helper')
    seen = []

    class _ExportHelper:
        @staticmethod
        def get_velo_bridge_game(tree=None, use_blueprint_marker=False):
            seen.append(use_blueprint_marker)
            return _VELO_MARKER['game']

    module.BlueprintExportHelper = _ExportHelper
    module.seen = seen
    return module


class SwapPanelEFMITests(unittest.TestCase):
    @staticmethod
    def _load_panel():
        fake_bpy = _fake_bpy()
        package_name = '_test_swap_panel.blueprint'
        package = types.ModuleType(package_name)
        package.__path__ = []
        base = types.ModuleType(package_name + '.node_postprocess_base')

        class Base:
            @classmethod
            def split_anim_driver_block_content(cls, content):
                return '', content

            @classmethod
            def split_auto_appended_tail_content(cls, content):
                return content, ''

        base.SSMTNode_PostProcess_Base = Base
        # 面板在调用期才 import export_helper，桩模块必须常驻 sys.modules
        # （mock.patch.dict 退出时会还原到进入前的状态，所以这里先装再进 patch）。
        export_helper = _make_export_helper_stub(package_name)
        sys.modules[export_helper.__name__] = export_helper
        # 产品代码用相对导入 `from ..common.global_config import ...`：这里按**真实文件位置**
        # 把同级的 common 包挂进假包（不复制内容，避免随产品改动腐坏）。
        root_pkg = types.ModuleType('_test_swap_panel')
        root_pkg.__path__ = []
        common_pkg = types.ModuleType('_test_swap_panel.common')
        common_pkg.__path__ = [str(ROOT / 'common')]
        stubs = {
            'bpy': fake_bpy,
            '_test_swap_panel': root_pkg,
            '_test_swap_panel.common': common_pkg,
            package_name: package,
            base.__name__: base,
            export_helper.__name__: export_helper,
        }
        with mock.patch.dict(sys.modules, stubs):
            importlib.import_module('_test_swap_panel.common.global_config')
            return _load_module(
                package_name + '.node_postprocess_swap_panel',
                ROOT / 'blueprint' / 'node_postprocess_swap_panel.py',
            )

    def setUp(self):
        _VELO_MARKER['game'] = ''

    def test_repeated_efmi_sections_are_merged_without_deduplication(self):
        panel_module = self._load_panel()
        panel = panel_module.SSMTNode_PostProcess_SwapPanel()
        with tempfile.TemporaryDirectory() as folder:
            ini = Path(folder) / 'mod.ini'
            ini.write_text(
                '[Constants]\nglobal $vertex_count = 10\n'
                '[Present]\nrun = First\n'
                '[constants]\nglobal $bones_count = 20\n'
                '[Present]\nrun = First\nrun = Second\n',
                encoding='utf-8',
            )
            sections = panel._read_ini_to_ordered_dict(str(ini))[0]

        self.assertEqual(
            sections['[Constants]'],
            ['global $vertex_count = 10', 'global $bones_count = 20'],
        )
        self.assertEqual(sections['[Present]'].count('run = First'), 2)

    def test_gui_only_guards_both_velo_and_wwmi_keyswap_names(self):
        panel_module = self._load_panel()
        panel = panel_module.SSMTNode_PostProcess_SwapPanel()
        panel.gui_only = True
        sections = {
            '[KeySwapSwapkey0]': ['condition = $object_detected == 1', 'key = 1', '$swapkey0 = 0, 1'],
            '[KeySwap_Swapkey1]': ['condition = $object_detected == 1', 'key = 2', '$swapkey1 = 0, 1'],
            '[CommandListSwap1]': ['key = 1'],
        }
        buttons = [
            {'var_names': ['$swapkey0']},
            {'var_names': ['$swapkey1']},
        ]
        panel._apply_gui_only_guards(sections, 'swp_test', buttons)
        self.assertIn('&& $swp_test_gui_only == 0', sections['[KeySwapSwapkey0]'][0])
        self.assertIn('&& $swp_test_gui_only == 0', sections['[KeySwap_Swapkey1]'][0])
        self.assertEqual(sections['[CommandListSwap1]'], ['key = 1'])

    def test_named_velo_keyswap_sections_feed_exact_variables_to_panel(self):
        panel_module = self._load_panel()
        panel = panel_module.SSMTNode_PostProcess_SwapPanel()
        with tempfile.TemporaryDirectory() as folder:
            ini = Path(folder) / 'mod.ini'
            ini.write_text(
                '[KeySwapLegsOutfit]\n'
                '; Legs\nkey = 1\ntype = cycle\n$Legs_Outfit = 0, 1, 2\n'
                '[KeySwap_CoatMode]\n'
                'key = 2\ntype = cycle\n$Coat_Mode = 0, 1\n'
                '[KeyHelp]\nkey = F1\n$panel_help = 0, 1\n',
                encoding='utf-8',
            )
            swaps = panel._parse_ini_key_swaps(str(ini))

        self.assertEqual([item['var_name'] for item in swaps], ['$Legs_Outfit', '$Coat_Mode'])
        self.assertEqual([item['option_count'] for item in swaps], [3, 2])
        self.assertEqual(panel._cycle_command_lines('$Legs_Outfit', 3)[0], 'if $Legs_Outfit == 0')


NATIVE_EFMI_INI = (
    '[Constants]\n'
    'global $component_count = 4\n'
    'global persist $swapkey0 = 0\n'
    '\n'
    '[TextureOverride_Component0_ab12cd34]\n'
    'hash = ab12cd34\n'
    'match_index_count = 1296\n'
    'handling = skip\n'
    '$\\EFMIv1\\component_id = 0\n'
    'run = CommandList\\EFMIv1\\OverrideTextures\n'
    '\n'
    '[KeySwap_0]\n'
    'condition = $active0 == 1\n'
    'key = 1\n'
    'type = cycle\n'
    '$swapkey0 = 0, 1\n'
    '\n'
    '[Present]\n'
    'post $active0 = 0\n'
)

NATIVE_WWMI_INI = (
    '[Constants]\n'
    'global $mod_enabled = 0\n'
    'global $object_detected = 0\n'
    'global persist $swapkey0 = 0\n'
    '\n'
    '[TextureOverrideComponent0]\n'
    'hash = ab12cd34\n'
    'match_index_count = 1296\n'
    '$object_detected = 1\n'
    '\n'
    '[KeySwap_0]\n'
    'condition = $active0 == 1\n'
    'key = 1\n'
    'type = cycle\n'
    '$swapkey0 = 0, 1\n'
    '\n'
    '[Present]\n'
    'post $object_detected = 0\n'
)


class SwapPanelVeloBranchTests(unittest.TestCase):
    """切换面板只在 Velo 桥接驱动的导出上走 EFMI 分支。

    判据来自桥接节点写入的显式标记，不再嗅探 ini 文本——TheHerta4 自家的 EFMI 导出
    同样会写 ``\\EFMIv1\\`` 命名空间，文本判据会把原生导出误判成 Velo 导出。
    """

    _PANEL_DEFAULTS = {
        'create_cumulative_backup': False,
        'help_key': 'home', 'reset_key': 'ctrl home',
        'zoom_in_key': 'up', 'zoom_out_key': 'down', 'drag_key': 'VK_LBUTTON',
        'gui_only': False,
        'button_height': 0.05, 'button_width': 0.0, 'buttons_per_row': 2,
        'button_column_spacing': 0.02, 'button_row_spacing': 0.02,
        'button_top_padding': 0.03, 'panel_min_height': 0.75,
        'panel_default_scale': 1.0, 'background_opacity': 0.85,
        'background_image': '', 'button_image': '', 'button_border_image': '',
        'use_remark_as_icon': False, 'remark_font_family': 'msyh.ttc', 'remark_font_size': 36,
        'remark_text_color': (1.0, 1.0, 1.0), 'remark_stroke_color': (0.0, 0.0, 0.0),
        'remark_stroke_width': 2,
        'button_bg_color': (0.16, 0.22, 0.32), 'button_border_color': (0.59, 0.75, 0.94),
        'button_border_width': 2, 'button_opacity': 0.9, 'button_align': 'CENTER',
        'background_corner_radius': 24, 'background_border_color': (0.59, 0.75, 0.94),
        'background_border_width': 3,
        'target_object': 'Body', 'detect_hash': 'ab12cd34', 'detect_index_count': '',
        'check_hash': '', 'match_index_count': 0,
        'ini_file_path': '', 'namespace': 'swp_ab12cd34', 'last_mod_ini_path': '',
        'width': 600, 'swap_panel_entries': [], 'swap_panel_button_entries': [],
    }

    def setUp(self):
        _VELO_MARKER['game'] = ''

    def _run_export(self, ini_text, marker_game):
        _VELO_MARKER['game'] = marker_game
        panel_module = SwapPanelEFMITests._load_panel()
        panel = panel_module.SSMTNode_PostProcess_SwapPanel()
        for name, value in self._PANEL_DEFAULTS.items():
            setattr(panel, name, value)
        panel._collect_swap_nodes = lambda: []
        panel._collect_diffuse_groups = lambda: []
        panel._collect_custom_material_switch_groups = lambda: []

        with tempfile.TemporaryDirectory() as folder:
            mod_dir = Path(folder) / 'mod'
            (mod_dir / 'res').mkdir(parents=True)
            ini = mod_dir / 'mod.ini'
            ini.write_text(ini_text, encoding='utf-8')
            ok = panel.execute_postprocess(str(mod_dir))
            text = ini.read_text(encoding='utf-8')
        return ok, text

    def test_native_efmi_export_keeps_panel_detection_section(self):
        """原生 EFMI（含 \\EFMIv1\\ 命名空间）没有 Velo 标记，必须保留自己的检测段。"""
        ok, text = self._run_export(NATIVE_EFMI_INI, '')
        self.assertTrue(ok)
        self.assertIn('[TextureOverrideCheckHash_swp_ab12cd34]', text)
        self.assertIn('$swp_ab12cd34_ui_active = 1', text)
        self.assertNotIn('$swp_ab12cd34_ui_active = $object_detected', text)

    def test_velo_efmi_export_reuses_object_detected(self):
        ok, text = self._run_export(NATIVE_EFMI_INI, 'ENDFIELD')
        self.assertTrue(ok)
        self.assertNotIn('[TextureOverrideCheckHash_swp_ab12cd34]', text)
        self.assertIn('$swp_ab12cd34_ui_active = $object_detected && $mod_enabled', text)

    def test_velo_wwmi_export_keeps_panel_detection_section(self):
        ok, text = self._run_export(NATIVE_WWMI_INI, 'WUTHERING')
        self.assertTrue(ok)
        self.assertIn('[TextureOverrideCheckHash_swp_ab12cd34]', text)
        self.assertIn('$swp_ab12cd34_ui_active = 1', text)

    def test_native_wwmi_export_keeps_panel_detection_section(self):
        ok, text = self._run_export(NATIVE_WWMI_INI, '')
        self.assertTrue(ok)
        self.assertIn('[TextureOverrideCheckHash_swp_ab12cd34]', text)
        self.assertIn('$swp_ab12cd34_ui_active = 1', text)

    def test_panel_trusts_blueprint_marker_only_when_refreshing(self):
        """导出期间只认运行时标记；「原地刷新」才允许回落到蓝图树的持久标记。"""
        panel_module = SwapPanelEFMITests._load_panel()
        panel = panel_module.SSMTNode_PostProcess_SwapPanel()
        panel.id_data = None
        helper_stub = sys.modules['_test_swap_panel.blueprint.export_helper']
        helper_stub.seen.clear()

        panel._is_velo_efmi_export(False)
        panel._is_velo_efmi_export(True)

        self.assertEqual(helper_stub.seen, [False, True])


def _stub_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _load_export_helper():
    """按 tests/test_export_helper_shapekey_objects.py 的方式加载真实 export_helper。"""
    pkg = '_test_velo_marker_pkg'
    stubs = {}
    for name in (pkg, pkg + '.blueprint', pkg + '.common', pkg + '.utils'):
        package = types.ModuleType(name)
        package.__path__ = []
        stubs[name] = package
    fake_bpy = types.ModuleType('bpy')
    fake_bpy.data = types.SimpleNamespace(objects={}, node_groups=[], texts={})
    fake_bpy.types = types.SimpleNamespace(Object=object)
    stubs['bpy'] = fake_bpy
    stubs[pkg + '.common.global_config'] = _stub_module(
        pkg + '.common.global_config',
        GlobalConfig=types.SimpleNamespace(get_workspace_name=lambda: ''),
    )
    stubs[pkg + '.common.global_properties'] = _stub_module(
        pkg + '.common.global_properties',
        GlobalProterties=types.SimpleNamespace(ignore_muted_shape_keys=lambda: False),
    )
    stubs[pkg + '.common.m_key'] = _stub_module(pkg + '.common.m_key', M_Key=type('M_Key', (), {}))
    stubs[pkg + '.common.object_prefix_helper'] = _stub_module(
        pkg + '.common.object_prefix_helper',
        ObjectPrefixHelper=types.SimpleNamespace(resolve_source_object_name=lambda name: name),
    )
    stubs[pkg + '.utils.shapekey_utils'] = _stub_module(
        pkg + '.utils.shapekey_utils',
        ShapeKeyUtils=types.SimpleNamespace(),
    )
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            pkg + '.blueprint.export_helper', ROOT / 'blueprint' / 'export_helper.py'
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


class ExportHelperVeloMarkerTests(unittest.TestCase):
    """Velo 桥接标记的解析契约（下游节点据此判断后端）。"""

    @classmethod
    def setUpClass(cls):
        cls.helper = _load_export_helper().BlueprintExportHelper

    def setUp(self):
        self.helper.runtime_velo_bridge_game = ''

    def test_runtime_marker_wins_and_is_normalized(self):
        self.helper.set_runtime_velo_bridge_game('endfield')
        self.assertEqual(self.helper.get_velo_bridge_game(), 'ENDFIELD')
        marker_tree = types.SimpleNamespace(name='BT', nodes=[], get=lambda key, default=None: 'WUTHERING')
        self.assertEqual(self.helper.get_velo_bridge_game(tree=marker_tree), 'ENDFIELD')

    def test_native_export_without_marker_is_not_velo(self):
        tree = types.SimpleNamespace(name='BT', nodes=[], get=lambda key, default=None: 'ENDFIELD')
        self.assertEqual(self.helper.get_velo_bridge_game(tree=tree), '')

    def test_blueprint_marker_used_only_when_requested(self):
        tree = types.SimpleNamespace(name='BT', nodes=[], get=lambda key, default=None: 'endfield')
        self.assertEqual(self.helper.get_velo_bridge_game(tree=tree, use_blueprint_marker=True), 'ENDFIELD')
        self.assertEqual(self.helper.get_velo_bridge_game(tree=tree, use_blueprint_marker=False), '')

    def test_bridge_node_marker_is_the_fallback(self):
        node = types.SimpleNamespace(bl_idname='SSMTNode_VeloExportBridge', velo_game='ENDFIELD')
        other = types.SimpleNamespace(bl_idname='SSMTNode_Object_Group', velo_game='')
        tree = types.SimpleNamespace(name='BT', nodes=[other, node], get=lambda key, default=None: '')
        self.assertEqual(self.helper.get_velo_bridge_game(tree=tree, use_blueprint_marker=True), 'ENDFIELD')

    def test_unknown_blueprint_has_no_marker(self):
        tree = types.SimpleNamespace(name='BT', nodes=[], get=lambda key, default=None: '')
        self.assertEqual(self.helper.get_velo_bridge_game(tree=tree, use_blueprint_marker=True), '')

    def test_setter_returns_previous_value_for_restore(self):
        previous = self.helper.set_runtime_velo_bridge_game('ENDFIELD')
        self.assertEqual(previous, '')
        self.helper.set_runtime_velo_bridge_game(previous)
        self.assertEqual(self.helper.get_velo_bridge_game(), '')


class VeloDetectionSeedTests(unittest.TestCase):
    """导出后回填检测值：由 ini 形状决定，不按游戏分支（WWMI/EFMI 同一路径）。"""

    WWMI_INI = (
        '[Constants]\nglobal $mod_enabled = 0\n\n'
        '[TextureOverrideComponent4]\n'
        'hash = AB12CD34\n'
        'match_first_index = 1000\n'
        'match_index_count = 4242\n'
        '$object_detected = 1\n'
    )
    EFMI_INI = (
        '[Constants]\nglobal $component_count = 4\n\n'
        '[TextureOverride_Component4_ab12cd34]\n'
        'hash = ab12cd34\n'
        'match_index_count = 4242\n'
    )

    class _Node:
        bl_idname = 'SSMTNode_PostProcess_SwapPanel'
        mute = False
        target_object = 'Component 4 Body'

        def __init__(self, detect_hash='', detect_index_count=''):
            self.detect_hash = detect_hash
            self.detect_index_count = detect_index_count

    class _Tree:
        def __init__(self, nodes):
            self.nodes = nodes

    class _Cfg:
        def __init__(self, folder):
            self.mod_output_folder = folder

    def _seed(self, ini_text, **node_kwargs):
        bridge = VeloBridgeEFMITests._load_bridge()
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'mod.ini').write_text(ini_text, encoding='utf-8')
            node = self._Node(**node_kwargs)
            bridge._seed_detection_for_export(self._Tree([node]), self._Cfg(folder))
        return node

    def test_wwmi_component_section_is_seeded(self):
        node = self._seed(self.WWMI_INI)
        self.assertEqual(node.detect_hash, 'ab12cd34')
        self.assertEqual(node.detect_index_count, '4242')

    def test_efmi_section_shape_is_left_untouched(self):
        node = self._seed(self.EFMI_INI)
        self.assertEqual(node.detect_hash, '')
        self.assertEqual(node.detect_index_count, '')

    def test_existing_values_are_never_overwritten(self):
        node = self._seed(self.WWMI_INI, detect_hash='deadbeef', detect_index_count='7')
        self.assertEqual(node.detect_hash, 'deadbeef')
        self.assertEqual(node.detect_index_count, '7')

    def test_missing_output_folder_is_ignored(self):
        bridge = VeloBridgeEFMITests._load_bridge()
        node = self._Node()
        bridge._seed_detection_for_export(
            self._Tree([node]), types.SimpleNamespace(mod_output_folder='')
        )
        self.assertEqual(node.detect_hash, '')


if __name__ == '__main__':
    unittest.main()

"""RabbitFX贴图后处理pro：可串联的 FX 参数注入节点。

覆盖：
* 静态发光参数注入到材质转资源写好的 Glowmap 绑定之前；
* 绘制后复位块（且不与材质转资源已有的复位行重复）；
* ColorShift 在 Run 之后重绑 FXMap（Run 结尾会清空该别名）；
* 呼吸灯三角波 + [Constants] 变量声明；
* W-Engine 同步的缓冲资源、copy 行与提取源 ERun；
* 多个同类节点串联、只改各自列表里的物体；
* 没有 FX 绑定的物体不改文件；
* 同一物体被两个同类节点接管时导出前报错；
* HIMI（崩坏 3）走 HI3FX 命名空间：``$\\HI3FX\\h/s/v`` 注入、复位改走
  ``CommandList\\HI3FX\\Reset``，同步与 ColorShift 自动跳过。
* ini_scope 结构化圈定（2026-10-22 实机 bug 回归）：同一 IB 下近似部件名不误匹配、
  mesh 在条件块内且还有嵌套材质切换 if 时参数插在 draw 前而复位在最内层 endif
  之后、块内 run 排在 draw 之后时参数仍插在 draw 前、两个目标共享同一 IB 时各自
  单元都保留参数块（互不剥离）。
"""

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_rabbitfx_pro_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeProps:
    def __getattr__(self, _name):
        def _property(**_kwargs):
            return None

        return _property


class _FakeTypes:
    """按需造出 bpy.types.* 基类（Operator / PropertyGroup / Node …）。"""

    def __getattr__(self, name):
        return type(name, (), {})


_install_module(
    "bpy",
    types=_FakeTypes(),
    props=_FakeProps(),
    utils=types.SimpleNamespace(
        register_class=lambda _cls: None,
        unregister_class=lambda _cls: None,
    ),
    data=types.SimpleNamespace(
        objects=types.SimpleNamespace(get=lambda _name: None),
        node_groups=[],
    ),
)


class _FakeSocketCollection(list):
    def new(self, bl_idname, name):
        socket = types.SimpleNamespace(bl_idname=bl_idname, name=name)
        self.append(socket)
        return socket


class _FakePostProcessBase:
    """后处理基类替身：只保留本节点用到的读写与块切分接口。"""

    def init(self, context):
        self.inputs = _FakeSocketCollection()
        self.outputs = _FakeSocketCollection()
        self.inputs.new('SSMTSocketPostProcess', "Input")
        self.outputs.new('SSMTSocketPostProcess', "Output")
        self.width = 300

    def _create_cumulative_backup(self, ini_file_path, mod_export_path):
        pass

    @classmethod
    def split_auto_appended_tail_content(cls, content):
        return content, ""

    @classmethod
    def split_anim_driver_block_content(cls, content):
        return "", content


_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=_FakePostProcessBase,
)


def _extract_prefix_info(object_name):
    """与 ObjectPrefixHelper.extract_prefix_info 对连字符前缀等价的最小实现。"""
    import re

    clean = str(object_name or "").strip()
    match = re.match(r"^LOD\d+\.(.+)$", clean, re.IGNORECASE)
    bare = match.group(1) if match else clean
    head = bare.split(".", 1)[0]
    parts = [part for part in head.split("-") if part]
    if len(parts) < 2:
        return None
    return "-".join(parts[:3]), "-"


_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(extract_prefix_info=_extract_prefix_info),
)

_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(HIMI="HIMI", NTEMI="NTEMI"),
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(logic_name="ZZMI"),
)
# 材质转资源模块的导入级依赖：DDS 转换在记录驱动的测试里不会真正跑，
# 桩成「转换成功」即可（真实转换由 test_dds_conversion 覆盖）。
_install_module(
    f"{PKG}.toolkit.tt_dds_conversion",
    convert_texture_to_dds_cached=lambda *_args, **_kwargs: (True, ""),
    is_dds_file=lambda path: str(path or "").lower().endswith(".dds"),
)

ROOT = Path(__file__).resolve().parents[1]


def _load(module_name, relative_path):
    spec = importlib.util.spec_from_file_location(
        f"{PKG}.blueprint.{module_name}", ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# FX 命名空间档案要最先加载：材质转资源与 pro 节点都从它取名字。
fx_namespace = _load("fx_namespace", "blueprint/fx_namespace.py")
# 再加载旧的 RabbitFX 节点模块：新节点直接复用它导出的哈希/标识解析函数。
_load("node_postprocess_rabbitfx", "blueprint/node_postprocess_rabbitfx.py")
pro = _load("node_postprocess_rabbitfx_pro", "blueprint/node_postprocess_rabbitfx_pro.py")
# 记录来源必须能在这套 fake-package 下装载（PR #68 的 ImportError 回归点）。
material = pro._load_material_module()

OBJECT_NAME = "LOD0.fd054d1d-30030-0.Body"
MESH_NAME = "LOD0.fd054d1d-30030-0.Body"
GLOW_SECTION = "TextureOverride_LOD0.fd054d1d_30030_0"
SECOND_OBJECT = "LOD0.aa11bb22-30030-0.Body"
SECOND_SECTION = "TextureOverride_LOD0.aa11bb22_30030_0"

# 实机 bug（2026-10-22 导出 INI 实测）：同一个 IB 下的两个部件，旧口径按
# 「IB 后缀互相包含」匹配，导致「右手.001」的单元被当成「右乳球.001」的目标，
# 参数块插进了别的部件里。新口径 = IB 键一致 + 部件名完全相等。
SHARED_IB_TARGET = "LOD0.c209c22b-30030-0.右乳球.001"
SHARED_IB_NEAR = "LOD0.c209c22b-30030-0.右手.001"
SHARED_IB_SECTION = "TextureOverride_LOD0.c209c22b_30030_0"

# 实机 bug（4:15:54 导出）：同一个 `if $swapkey4 == 0` 块内相邻的多个目标单元，
# 前一个单元的可写范围被扩到共享的 endif，把兄弟单元整个吞进来后互相剥离参数块
# （音擎.002 / 珠子.002 / 肛塞.002 都没有 Params）。
CHAIN_UNIT_NAMES = (
    "LOD0.c209c22b-30030-0.乳钩.001",
    "LOD0.c209c22b-30030-0.音擎.002",
    "LOD0.c209c22b-30030-0.珠子.002",
)


class _FakeObject:
    """网格物体替身：真实 bpy.types.Object 可哈希，这里按身份实现哈希/相等。"""

    def __init__(self, name=OBJECT_NAME):
        self.name = name
        self.type = "MESH"

    def get(self, _key, _default=None):
        return ""

    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other


def _fake_object(name=OBJECT_NAME):
    return _FakeObject(name)


def _make_node(**overrides):
    node = pro.SSMTNode_PostProcess_RabbitFXPro()
    defaults = dict(
        name='RabbitFXPro',
        enable_glow=True,
        glow_h=0.0,
        glow_s=0.0,
        glow_v=0.0,
        glow_brightness=8.0,
        glow_interpolate=1.0,
        preview_use_color=False,
        preview_color=(1.0, 1.0, 1.0, 1.0),
        breath_mode='SINGLE',
        enable_breath=False,
        breath_fps=200,
        breath_step=0.3,
        breath_h=0.0,
        breath_s=0.0,
        breath_v=0.0,
        breath_brightness_min=1.0,
        breath_brightness_max=8.0,
        breath_interpolate=1.0,
        enable_sync=False,
        sync_blendmode=1,
        sync_brightness_only=False,
        sync_source_object=None,
        buffer_mode='COPY',
        enable_colorshift=False,
        cs_h=0.0,
        cs_s=0.0,
        cs_v=0.0,
        enable_reset=True,
        buffer_tag='abcd1234',
        active_target_index=0,
        show_targets=True,
        show_glow=True,
        show_breath=True,
        show_fx=True,
        show_sync=True,
        show_colorshift=True,
        target_items=[],
    )
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(node, key, value)
    node.as_pointer = lambda: id(node)
    node.target_items = _FakeCollection(node.target_items)
    # 挂在一棵含「材质转资源」的假蓝图里（本节点的贴图绑定来源）；必须在
    # name 等属性设置之后建树，节点集合是按名字索引的。
    node.id_data = _fake_tree([node])
    return node


def _item(obj):
    return types.SimpleNamespace(target_object=obj, has_glow=False, has_fx=False)


class _FakeCollection(list):
    """模拟 Blender CollectionProperty 的 add/remove(index)/clear 语义。"""

    def add(self):
        item = types.SimpleNamespace()
        self.append(item)
        return item

    def remove(self, index):
        del self[index]

    def clear(self):
        del self[:]


class _StubProbe:
    """材质前缀探测替身：真实实现要 import 材质转资源模块，测试里默认不匹配。"""

    def find_matching_materials(self, _obj, _texture_type):
        return []


def _material_node():
    return types.SimpleNamespace(
        bl_idname=pro.MATERIAL_NODE_IDNAME,
        name='材质转资源pro',
        mute=False,
        target_items=[],
        use_global_assign=False,
    )


class _FakeNodeCollection(dict):
    """模拟 bpy 节点集合：`.get(name)` 按名取，迭代产出节点对象。"""

    def __iter__(self):
        return iter(self.values())


def _fake_tree(nodes, with_material_node=True):
    members = list(nodes)
    if with_material_node:
        members.append(_material_node())
    return types.SimpleNamespace(
        nodes=_FakeNodeCollection({getattr(node, "name", ""): node for node in members})
    )


class _FakeLayout:
    """记录 draw_buttons 调用轨迹的最小 UILayout 替身。"""

    def __init__(self):
        self.log = []

    def row(self, align=False):
        return self

    def column(self, align=False):
        return self

    def box(self):
        return self

    def separator(self):
        return self

    def label(self, **kwargs):
        self.log.append(("label", kwargs.get("text", ""), kwargs.get("icon", "")))
        return self

    def prop(self, _owner, name, **kwargs):
        self.log.append(("prop", name, kwargs.get("text", "<未指定>")))
        return self

    def operator(self, idname, **_kwargs):
        self.log.append(("op", idname, ""))
        return _FakeOperator(self.log, idname)

    def template_list(self, list_type, _list_id, _data, propname, _active_data, active_propname, **kwargs):
        self.log.append(
            ("template_list", list_type, f"rows={kwargs.get('rows')} prop={propname} active={active_propname}")
        )
        return self

    def prop_search(self, *_args, **_kwargs):
        return self


class _FakeOperator:
    """记录 `op.xxx = yyy` 这类赋值，便于断言每行按钮携带的下标。"""

    def __init__(self, log, idname):
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "_idname", idname)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._log.append(("op_prop", self._idname, f"{name}={value}"))


def _write_ini(path, content):
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(content)


def _read_ini(path):
    with open(path, 'r', encoding='utf-8') as handle:
        return handle.read()


MATERIAL_SECTION = (
    f"[{GLOW_SECTION}]\n"
    f"[mesh:{MESH_NAME}]\n"
    "hash = fd054d1d\n"
    "match_first_index = 0\n"
    "ps-t0 = Resource_DiffuseMap_Body\n"
    "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_5_Body\n"
    "$\\RabbitFX\\brightness = 5\n"
    "run = CommandList\\RabbitFX\\Run\n"
    "drawindexed = 52688, 0, 3\n"
    "Resource\\RabbitFX\\Glowmap = ref null\n"
    "$\\RabbitFX\\brightness = 0\n"
    "run = CommandList\\RabbitFX\\Run\n"
)

RECORD_MARKER = '; MaterialRabbitFX record'

# 材质转资源登记过的文件：标记行是它在段内的定位点，参数块要插在标记之前。
MATERIAL_RECORD_SECTION = (
    f"[{GLOW_SECTION}]\n"
    f"[mesh:{MESH_NAME}]\n"
    "hash = fd054d1d\n"
    "match_first_index = 0\n"
    "ps-t0 = Resource_DiffuseMap_Body\n"
    "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_5_Body\n"
    f"{RECORD_MARKER}\n"
    "run = CommandList\\RabbitFX\\Run\n"
    "drawindexed = 52688, 0, 3\n"
)

FXMAP_SECTION = (
    f"[{SECOND_SECTION}]\n"
    f"[mesh:{SECOND_OBJECT}]\n"
    "hash = aa11bb22\n"
    "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_9_Body\n"
    "Resource\\RabbitFX\\FXMap = ref Resource_FXMap_Body\n"
    "run = CommandList\\RabbitFX\\Run\n"
    "drawindexed = 100, 0, 0\n"
)


class RabbitFXProTests(unittest.TestCase):
    def test_preview_uses_static_and_breath_parameters(self):
        node = _make_node(glow_h=30, glow_s=20, glow_v=-10,
                          glow_brightness=4, glow_interpolate=0.5)
        self.assertEqual(pro._preview_parameters(node, 0), (30, 20, -10, 4, 0.5))
        node.enable_breath = True
        node.breath_mode = 'SINGLE'
        node.breath_fps = 60
        node.breath_step = 1
        node.breath_brightness_min = 2
        node.breath_brightness_max = 6
        self.assertEqual(pro._preview_parameters(node, 1)[3], 6)
        self.assertEqual(pro._preview_parameters(node, 2)[3], 2)
        node.breath_mode = 'RAINBOW'
        self.assertEqual(pro._preview_parameters(node, 0.5)[0], 180)

    def test_preview_uses_wide_high_quality_bloom(self):
        """预览 Bloom 应模拟游戏的大范围高亮溢出。"""
        inputs = {
            'Type': types.SimpleNamespace(default_value=None),
            'Quality': types.SimpleNamespace(default_value=None),
            'Threshold': types.SimpleNamespace(default_value=None),
            'Strength': types.SimpleNamespace(default_value=None),
            'Size': types.SimpleNamespace(default_value=None),
            'Iterations': types.SimpleNamespace(default_value=None),
        }
        bloom = types.SimpleNamespace(inputs=inputs)

        pro._configure_preview_bloom(bloom)

        self.assertEqual(inputs['Type'].default_value, 'Bloom')
        self.assertEqual(inputs['Quality'].default_value, 'High')
        self.assertEqual(inputs['Threshold'].default_value, 0.5)
        self.assertEqual(inputs['Strength'].default_value, 1.0)
        self.assertEqual(inputs['Size'].default_value, 0.9)
        self.assertEqual(inputs['Iterations'].default_value, 4)
        self.assertEqual(pro._PREVIEW_BLOOM_CUTOFF, 0.05)

    def test_preview_uses_emission_surface_without_disconnecting_textures(self):
        """预览在现有 Surface 上叠加发光着色器，不改内部贴图连接。"""
        class FakeNodes:
            def __init__(self):
                self.created = []

            def new(self, bl_idname):
                sockets = {
                    'ShaderNodeEmission': (
                        {'Color': object(), 'Strength': object(), 'Weight': object()},
                        {'Emission': object()},
                    ),
                    'ShaderNodeAddShader': (
                        [object(), object()],
                        {'Shader': object()},
                    ),
                }
                inputs, outputs = sockets[bl_idname]
                node = types.SimpleNamespace(
                    bl_idname=bl_idname, inputs=inputs, outputs=outputs,
                )
                self.created.append(node)
                return node

        class FakeLinks:
            def __init__(self):
                self.log = []

            def new(self, output, input_socket):
                self.log.append((output, input_socket))

        nodes = FakeNodes()
        links = FakeLinks()
        original_surface = object()
        surface = types.SimpleNamespace(
            links=[types.SimpleNamespace(from_socket=original_surface)],
        )
        output_node = types.SimpleNamespace(inputs={'Surface': surface})
        color = object()
        strength = object()

        emission = pro._use_preview_emission_surface(
            nodes, links, output_node, color, strength,
        )

        self.assertEqual(emission.bl_idname, 'ShaderNodeEmission')
        self.assertEqual(len(links.log), 5)
        self.assertIs(links.log[0][1], emission.inputs['Color'])
        self.assertIs(links.log[1][1], emission.inputs['Strength'])
        self.assertIs(links.log[2][0], original_surface)
        self.assertIs(links.log[3][0], emission.outputs['Emission'])
        self.assertIs(links.log[4][1], surface)

    def test_preview_mutes_base_emission_without_removing_texture_nodes(self):
        """预览副本保留原发光贴图，但把原 Emission Strength 路径乘 0。"""
        class FakeNodes:
            def __init__(self):
                self.created = []

            def new(self, bl_idname):
                node = types.SimpleNamespace(
                    bl_idname=bl_idname,
                    inputs=[
                        types.SimpleNamespace(default_value=0.0),
                        types.SimpleNamespace(default_value=0.0),
                    ],
                    outputs={'Value': object()},
                )
                self.created.append(node)
                return node

        class FakeLinks:
            def __init__(self):
                self.log = []
                self.removed = []

            def new(self, output, input_socket):
                self.log.append((output, input_socket))

            def remove(self, link):
                self.removed.append(link)

        original_output = object()
        original_link = types.SimpleNamespace(from_socket=original_output)
        linked_strength = types.SimpleNamespace(
            links=[original_link], default_value=3.0,
        )
        unlinked_strength = types.SimpleNamespace(
            links=[], default_value=2.0,
        )
        shader_nodes = [
            types.SimpleNamespace(
                bl_idname='ShaderNodeBsdfPrincipled',
                inputs={'Emission Strength': linked_strength},
            ),
            types.SimpleNamespace(
                bl_idname='ShaderNodeBsdfPrincipled',
                inputs={'Emission Strength': unlinked_strength},
            ),
        ]

        nodes = FakeNodes()
        links = FakeLinks()
        pro._mute_preview_base_emission(shader_nodes, nodes, links)

        self.assertEqual(links.removed, [original_link])
        self.assertEqual(len(nodes.created), 1)
        multiply = nodes.created[0]
        self.assertEqual(multiply.bl_idname, 'ShaderNodeMath')
        self.assertEqual(multiply.operation, 'MULTIPLY')
        self.assertEqual(multiply.inputs[1].default_value, 0.0)
        self.assertEqual(links.log, [
            (original_output, multiply.inputs[0]),
            (multiply.outputs['Value'], linked_strength),
        ])
        self.assertEqual(unlinked_strength.default_value, 0.0)

    def test_preview_restarts_from_data_material_when_slot_holds_stale_preview(self):
        """对象槽里的旧预览副本不能再作为下一次预览的复制源。"""
        data_material = types.SimpleNamespace(
            name='Glowmap_发光', node_tree=types.SimpleNamespace(nodes=[]), get=lambda *_args: None,
        )
        stale_preview = types.SimpleNamespace(
            name='Glowmap_发光.010',
            node_tree=types.SimpleNamespace(nodes=[
                types.SimpleNamespace(bl_idname='ShaderNodeEmission'),
                types.SimpleNamespace(bl_idname='ShaderNodeHueSaturation'),
                types.SimpleNamespace(bl_idname='ShaderNodeCombineColor'),
            ]),
            get=lambda *_args: None,
        )
        obj = types.SimpleNamespace(
            data=types.SimpleNamespace(materials=[data_material]),
            material_slots=[types.SimpleNamespace(
                link='OBJECT', material=stale_preview,
            )],
        )

        source, source_link = pro._preview_material_source(obj, 0)
        self.assertIs(source, data_material)
        self.assertEqual(source_link, 'DATA')

        override = types.SimpleNamespace(
            name='IMGPV_Body', node_tree=types.SimpleNamespace(nodes=[]), get=lambda *_args: None,
        )
        obj.material_slots[0].material = override
        source, source_link = pro._preview_material_source(obj, 0)
        self.assertIs(source, override)
        self.assertEqual(source_link, 'OBJECT')

    def test_preview_uses_diffuse_material_when_active_source_is_glowmap(self):
        """专用 Glowmap 材质只作发光遮罩，不能作为整个网格的可见材质。"""
        diffuse = types.SimpleNamespace(name='DiffuseMap_发光', use_nodes=True)
        glowmap = types.SimpleNamespace(name='Glowmap_发光', use_nodes=True)
        composite = types.SimpleNamespace(name='IMGPV_Body', use_nodes=True)
        obj = types.SimpleNamespace(
            data=types.SimpleNamespace(materials=[diffuse, glowmap]),
            material_slots=[
                types.SimpleNamespace(link='DATA', material=diffuse),
                types.SimpleNamespace(link='DATA', material=glowmap),
            ],
        )

        self.assertIs(pro._preview_base_material(obj, glowmap, [glowmap]), diffuse)
        self.assertIs(pro._preview_base_material(obj, composite, [glowmap]), composite)

    def test_preview_ownership_follows_target_nodes_from_either_button(self):
        """从任一节点预览时，目标物体仍使用各自列表所属节点的参数。"""
        first_object = _fake_object()
        second_object = _fake_object(SECOND_OBJECT)
        first_node = _make_node(
            name='RabbitFXPro.First',
            enable_breath=True,
            breath_mode='RAINBOW',
            preview_use_color=True,
            preview_color=(1.0, 0.0, 0.0, 1.0),
            target_items=[_item(first_object)],
        )
        second_node = _make_node(
            name='RabbitFXPro.Second',
            enable_breath=False,
            preview_use_color=True,
            preview_color=(0.0, 0.0, 1.0, 1.0),
            target_items=[_item(second_object)],
        )
        tree = _fake_tree([first_node, second_node])
        first_node.id_data = tree
        second_node.id_data = tree

        self.assertEqual(pro._preview_tree_key(first_node), pro._preview_tree_key(second_node))
        for trigger_node in (first_node, second_node):
            owners = pro._preview_owner_nodes(trigger_node)
            self.assertIs(owners[first_object], first_node)
            self.assertIs(owners[second_object], second_node)

    def test_material_record_controls_parameter_position(self):
        obj = _fake_object()
        node = _make_node(target_items=[_item(obj)])
        marker = '; MaterialRabbitFX test'
        lines = [
            f'; [mesh:{obj.name}]',
            r'run = CommandList\ZZMI\SetTextures',
            r'$\RabbitFX\brightness = 5',
            r'Resource\RabbitFX\Glowmap = ref Resource_Glow',
            marker,
            r'run = CommandList\RabbitFX\Run',
            'drawindexed = 10, 0, 0',
        ]
        sections = {'[TextureOverride_Test]': lines}
        records = [{'object': obj.name, 'type': 'Glowmap', 'marker': marker,
                    'section': '[TextureOverride_Test]',
                    'lines': [r'Resource\RabbitFX\Glowmap = ref Resource_Glow']}]
        self.assertTrue(node._apply_material_records(sections, records, [obj], 'buffer'))
        text = '\n'.join(lines)
        self.assertLess(text.index(r'$\RabbitFX\brightness = 5'), text.index(r'$\RabbitFX\brightness = 8.0'))
        self.assertLess(text.index(r'$\RabbitFX\brightness = 8.0'), text.index(r'run = CommandList\RabbitFX\Run'))
        untouched = list(lines)
        self.assertFalse(node._apply_material_records(sections, records, [_fake_object('another')], 'buffer'))
        self.assertEqual(lines, untouched)

    def test_material_record_keeps_fxmap_switch_branches(self):
        obj = _fake_object()
        node = _make_node(target_items=[_item(obj)], enable_glow=False, enable_colorshift=True)
        marker = '; MaterialRabbitFX fx'
        bindings = ['if $swapkey1 == 0', r'    Resource\RabbitFX\FXMap = ref Resource_A',
                    'endif', 'if $swapkey1 == 1', r'    Resource\RabbitFX\FXMap = ref Resource_B', 'endif']
        lines = [f'; [mesh:{obj.name}]', *bindings, marker,
                 r'run = CommandList\RabbitFX\Run', 'drawindexed = 10, 0, 0']
        records = [{'object': obj.name, 'type': 'FXMap', 'marker': marker,
                    'section': '[TextureOverride_Test]', 'lines': bindings}]
        self.assertTrue(node._apply_material_records({'[TextureOverride_Test]': lines}, records, [obj], 'buffer'))
        after_run = '\n'.join(lines).split(r'run = CommandList\RabbitFX\Run', 1)[1]
        self.assertIn('if $swapkey1 == 0\n    ; === RabbitFXPro ColorShift ===', after_run)
        self.assertIn(r'Resource\RabbitFX\FXMap = ref Resource_A', after_run)
        self.assertIn('if $swapkey1 == 1\n    ; === RabbitFXPro ColorShift ===', after_run)
        self.assertIn(r'Resource\RabbitFX\FXMap = ref Resource_B', after_run)

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.export_dir = self._temp.name
        self.ini_path = os.path.join(self.export_dir, "mod.ini")
        self._probe_backup = pro._PROBE
        pro._PROBE = _StubProbe()
        # 材质转资源的登记表是模块级状态：每个用例从空表开始，避免互相污染。
        self._outputs_backup = dict(material._rabbitfx_outputs)
        material._rabbitfx_outputs.clear()
        # 命名空间按当前逻辑解析，测试之间必须复位，否则会互相污染。
        self._logic_backup = sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "ZZMI"

    def tearDown(self):
        pro._PROBE = self._probe_backup
        material._rabbitfx_outputs.clear()
        material._rabbitfx_outputs.update(self._outputs_backup)
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = self._logic_backup
        self._temp.cleanup()

    # ──────────────── 目标物体列表（一个大列表） ────────────────

    def test_target_list_uses_scrollable_uilist(self):
        """列表是 UIList（自带滚动条），可见行数有上限，不会把所有物体铺开。"""
        node = _make_node(
            target_items=[_item(_fake_object()), _item(_fake_object(SECOND_OBJECT))]
        )
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        log = layout.log
        template_calls = [e for e in log if e[0] == "template_list"]
        self.assertEqual(len(template_calls), 1)
        self.assertEqual(template_calls[0][1], "SSMT_UL_RABBITFX_PRO_TARGETS")
        self.assertIn("prop=target_items", template_calls[0][2])
        self.assertIn("active=active_target_index", template_calls[0][2])
        # 节点本身不再逐个画物体行
        self.assertFalse([e for e in log if e[0] == "prop" and e[1] == "target_object"])

    def test_hundreds_of_objects_keep_the_node_compact(self):
        """两百个物体时仍然只占 6 行高度（其余靠滚动）。"""
        node = _make_node(
            target_items=[_item(_fake_object(f"Body_{i:03d}")) for i in range(200)]
        )
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        template_calls = [e for e in layout.log if e[0] == "template_list"]
        self.assertEqual(len(template_calls), 1)
        self.assertIn("rows=6", template_calls[0][2])
        self.assertFalse([e for e in layout.log if e[0] == "prop" and e[1] == "target_object"])

    def test_uilist_row_draws_picker_icons_and_remove(self):
        """列表行：物体选择器 + 发光/FX 图标 + 移除按钮（带该行下标）。"""
        node = _make_node()
        item = _item(_fake_object())
        item.has_glow = True
        item.has_fx = True
        uilist = pro.SSMT_UL_RabbitFXProTargets()
        uilist.layout_type = 'DEFAULT'
        layout = _FakeLayout()

        uilist.draw_item(None, layout, node, item, None, None, None, 7)

        props = [e for e in layout.log if e[0] == "prop"]
        self.assertEqual([e[1] for e in props], ["target_object"])
        icons = [e[2] for e in layout.log if e[0] == "label"]
        self.assertIn("LIGHT", icons)
        self.assertIn("TEXTURE", icons)
        remove_props = [
            e[2] for e in layout.log
            if e[0] == "op_prop" and e[1] == "ssmt.rabbitfx_pro_remove_at"
        ]
        self.assertIn("index=7", remove_props)
        self.assertIn(f"node_name={node.name}", remove_props)

    def test_side_remove_button_removes_active_row(self):
        """右侧 − 不指定 index 时移除列表里高亮的那一行。"""
        first, second = _fake_object(), _fake_object(SECOND_OBJECT)
        node = _make_node(target_items=[_item(first), _item(second)], active_target_index=1)
        context = types.SimpleNamespace(active_node=node)
        operator = pro.SSMT_OT_RabbitFXProRemoveAt()
        operator.index = -1  # 真实 Blender 里由 IntProperty(default=-1) 提供

        operator.execute(context)

        self.assertEqual(
            [item.target_object.name for item in node.target_items], [OBJECT_NAME]
        )
        self.assertEqual(node.active_target_index, 0)

    def test_add_selected_operator_appends_into_one_list(self):
        """「添加选中物体」把选中的物体各加一行到同一个列表里，并去重。"""
        node = _make_node()
        first = _fake_object()
        second = _fake_object(SECOND_OBJECT)
        context = types.SimpleNamespace(
            active_node=node, selected_objects=[first, second, first]
        )
        operator = pro.SSMT_OT_RabbitFXProAddSelected()
        operator.report = lambda *_args, **_kwargs: None

        operator.execute(context)

        self.assertEqual(
            [item.target_object.name for item in node.target_items],
            [OBJECT_NAME, SECOND_OBJECT],
        )

    def test_add_selected_resolves_node_by_name_without_active_node(self):
        """点节点面板里的按钮时 active_node 可能不是本节点 —— 必须按名字取到。

        这是实机反馈的 bug：按钮只认 context.active_node，活动节点是别的节点时
        直接静默 CANCELLED，点了没反应。
        """
        node = _make_node()
        first = _fake_object()
        second = _fake_object(SECOND_OBJECT)
        context = types.SimpleNamespace(
            space_data=types.SimpleNamespace(edit_tree=node.id_data),
            active_node=None,
            selected_objects=[first, second],
        )
        operator = pro.SSMT_OT_RabbitFXProAddSelected()
        operator.node_name = node.name
        operator.report = lambda *_args, **_kwargs: None

        operator.execute(context)

        self.assertEqual(len(node.target_items), 2)

    def test_add_selected_reports_when_nothing_selected(self):
        node = _make_node()
        context = types.SimpleNamespace(active_node=node, selected_objects=[])
        reports = []
        operator = pro.SSMT_OT_RabbitFXProAddSelected()
        operator.report = lambda kind, message: reports.append((kind, message))

        result = operator.execute(context)

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(len(node.target_items), 0)
        self.assertTrue(any("没有选中" in message for _kind, message in reports))

    def test_node_buttons_carry_node_name(self):
        """节点内部按钮都要带 node_name，否则点下去可能落到别的活动节点上。"""
        node = _make_node(target_items=[_item(_fake_object())])
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        buttons = {
            entry[1] for entry in layout.log if entry[0] == "op"
        }
        self.assertIn("ssmt.rabbitfx_pro_scan", buttons)
        self.assertIn("ssmt.rabbitfx_pro_add", buttons)
        self.assertIn("ssmt.rabbitfx_pro_add_selected", buttons)
        self.assertIn("ssmt.rabbitfx_pro_remove_at", buttons)
        named = {
            entry[1] for entry in layout.log
            if entry[0] == "op_prop" and entry[2] == f"node_name={node.name}"
        }
        for idname in buttons:
            self.assertIn(idname, named, f"{idname} 没有带 node_name")

    def test_remove_at_operator_removes_that_row(self):
        node = _make_node(
            target_items=[_item(_fake_object()), _item(_fake_object(SECOND_OBJECT))]
        )
        context = types.SimpleNamespace(active_node=node)
        operator = pro.SSMT_OT_RabbitFXProRemoveAt()
        operator.index = 0

        operator.execute(context)

        self.assertEqual(
            [item.target_object.name for item in node.target_items], [SECOND_OBJECT]
        )

    def test_static_glow_injected_before_run_with_reset(self):
        _write_ini(self.ini_path, MATERIAL_SECTION)
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn(pro._PARAM_BEGIN, content)
        self.assertIn("$\\RabbitFX\\H = 0.0", content)
        self.assertIn("$\\RabbitFX\\S = 0.0", content)
        self.assertIn("$\\RabbitFX\\V = 0.0", content)
        self.assertIn("$\\RabbitFX\\brightness = 8.0", content)
        self.assertIn("$\\RabbitFX\\interpolate = 1.0", content)
        # 参数必须在 run 之前，否则 Run 会先按材质转资源的旧值执行。
        self.assertLess(
            content.index(pro._PARAM_BEGIN),
            content.index("run = CommandList\\RabbitFX\\Run"),
        )
        # 绘制后复位；材质转资源已经写过的 null/0 行不重复。
        self.assertIn(pro._RESET_BEGIN, content)
        self.assertLess(
            content.index("drawindexed = 52688, 0, 3"),
            content.index(pro._RESET_BEGIN),
        )
        self.assertEqual(content.count("Resource\\RabbitFX\\Glowmap = ref null"), 1)
        self.assertEqual(content.count("$\\RabbitFX\\brightness = 0"), 1)
        self.assertIn("$\\RabbitFX\\interpolate = 0", content)

    def test_registered_material_record_drives_injection(self):
        """有材质登记：只处理登记文件，参数插在记录标记之前。"""
        _write_ini(self.ini_path, MATERIAL_RECORD_SECTION)
        decoy_path = os.path.join(self.export_dir, "unregistered.ini")
        _write_ini(decoy_path, MATERIAL_SECTION)
        material._rabbitfx_outputs[os.path.normcase(os.path.abspath(self.ini_path))] = [
            {
                'object': OBJECT_NAME,
                'type': 'Glowmap',
                'marker': RECORD_MARKER,
                'section': f'[{GLOW_SECTION}]',
                'lines': [r'Resource\RabbitFX\Glowmap = ref Resource_Glowmap_5_Body'],
            }
        ]
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)

        content = _read_ini(self.ini_path)
        self.assertIn(pro._PARAM_BEGIN, content)
        self.assertLess(content.index(pro._PARAM_BEGIN), content.index(RECORD_MARKER))
        self.assertLess(
            content.index(pro._PARAM_BEGIN),
            content.index("run = CommandList\\RabbitFX\\Run"),
        )
        # 同目录里没被登记的文件不走旧 glob 路径，必须保持原样。
        self.assertEqual(_read_ini(decoy_path), MATERIAL_SECTION)

    def test_no_record_for_directory_falls_back_to_glob(self):
        """该目录一条登记都没有：回退旧口径，扫描目录下 *.ini 并注入。"""
        _write_ini(self.ini_path, MATERIAL_SECTION)
        # 别的目录的登记不能顶替本目录的回退判断。
        material._rabbitfx_outputs[
            os.path.normcase(
                os.path.abspath(os.path.join(self.export_dir, "other", "other.ini"))
            )
        ] = []
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)

        self.assertIn(pro._PARAM_BEGIN, _read_ini(self.ini_path))

    def test_registered_empty_records_keep_ini_untouched(self):
        """文件已登记但记录为空：新语义下不回退扫目录，字节不变。"""
        _write_ini(self.ini_path, MATERIAL_SECTION)
        material._rabbitfx_outputs[os.path.normcase(os.path.abspath(self.ini_path))] = []
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)

        self.assertEqual(_read_ini(self.ini_path), MATERIAL_SECTION)

    def test_material_module_imports_without_package_path(self):
        """fake-package 下也能取到 material 记录来源；重复调用不重装载。"""
        imported = pro._load_material_module()
        self.assertIs(imported, material)
        self.assertIs(imported, sys.modules[f"{PKG}.blueprint.node_postprocess_material"])
        self.assertIs(sys.modules[f"{PKG}.blueprint"].node_postprocess_material, imported)
        self.assertIsInstance(imported._rabbitfx_outputs, dict)
        self.assertTrue(hasattr(imported, "SSMTNode_PostProcess_MaterialBase"))
        self.assertIs(
            importlib.import_module(".node_postprocess_material", f"{PKG}.blueprint"),
            imported,
        )

    def test_rerun_is_idempotent(self):
        _write_ini(self.ini_path, MATERIAL_SECTION)
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)
        first = _read_ini(self.ini_path)
        node.execute_postprocess(self.export_dir)
        second = _read_ini(self.ini_path)

        self.assertEqual(first, second)
        self.assertEqual(second.count(pro._PARAM_BEGIN), 1)

    def test_colorshift_rebinds_fxmap_after_run(self):
        _write_ini(self.ini_path, FXMAP_SECTION)
        node = _make_node(
            enable_glow=False,
            enable_colorshift=True,
            cs_h=-105.0,
            cs_s=30.0,
            cs_v=50.0,
            target_items=[_item(_fake_object(SECOND_OBJECT))],
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn(pro._CS_BEGIN, content)
        self.assertIn("Resource\\RabbitFX\\FXMap = ref Resource_FXMap_Body", content)
        self.assertIn("run = CommandList\\RabbitFX\\ColorShift", content)
        # Run 命令列表结尾会清空 FXMap 别名，ColorShift 必须排在它后面。
        run_index = content.index("run = CommandList\\RabbitFX\\Run")
        self.assertLess(run_index, content.index("run = CommandList\\RabbitFX\\ColorShift"))
        self.assertLess(
            content.index("run = CommandList\\RabbitFX\\Run"),
            content.index("$\\RabbitFX\\H = -105.0"),
        )

    def test_draw_buttons_nests_breath_under_glow(self):
        """层级：发光贴图参数（启用勾）→ 呼吸灯（启用勾）；收起父级时子级不画。"""
        node = _make_node(
            target_items=[_item(_fake_object())],
            enable_breath=True,
            enable_sync=True,
            enable_colorshift=True,
        )
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        labels = [entry[1] for entry in layout.log if entry[0] == "label"]
        self.assertIn("发光贴图参数", labels)
        self.assertIn("呼吸灯", labels)
        self.assertIn("FX 贴图参数", labels)
        self.assertIn("W-Engine 同步", labels)
        self.assertIn("颜色偏移 ColorShift", labels)
        # 呼吸灯在发光贴图参数之后（= 挂在它下面）。
        self.assertLess(labels.index("发光贴图参数"), labels.index("呼吸灯"))

        # 一级是折叠标题，二级是组内「标题 + 启用勾」行。
        self.assertEqual(labels.count("发光贴图参数"), 2)
        log = layout.log
        title_rows = [
            index for index, entry in enumerate(log)
            if entry[0] == "label" and entry[1] == "发光贴图参数"
        ]
        enable_row = next(
            index for index, entry in enumerate(log)
            if entry[0] == "prop" and entry[1] == "enable_glow"
        )
        self.assertGreater(enable_row, title_rows[1])

        # 收起「发光贴图参数」时，子项呼吸灯不再绘制。
        collapsed = _FakeLayout()
        node.show_glow = False
        node.draw_buttons(None, collapsed)
        collapsed_labels = [entry[1] for entry in collapsed.log if entry[0] == "label"]
        self.assertNotIn("呼吸灯", collapsed_labels)

    def test_draw_buttons_has_no_english_label_leak(self):
        """折叠标题/启用勾都不能漏出属性英文名（text 必须显式给空串）。"""
        node = _make_node(target_items=[_item(_fake_object())], enable_breath=True)
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        bare_props = [
            entry for entry in layout.log
            if entry[0] == "prop"
            and (
                entry[1].startswith("show_")
                or entry[1] in {"enable_glow", "enable_breath", "enable_sync", "enable_colorshift"}
            )
        ]
        self.assertTrue(bare_props)
        for _kind, name, text in bare_props:
            self.assertEqual(text, "", f"{name} 会漏出英文属性名")

    def test_colorshift_reset_only_covers_written_params(self):
        """FX 参数块不依赖任何"镂空"开关；复位也只覆盖真正写过的参数。"""
        _write_ini(self.ini_path, FXMAP_SECTION)
        node = _make_node(
            enable_glow=False,
            enable_colorshift=True,
            target_items=[_item(_fake_object(SECOND_OBJECT))],
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertNotIn(pro._PARAM_BEGIN, content)
        self.assertIn(pro._CS_BEGIN, content)
        reset = content.split(pro._RESET_BEGIN, 1)[1]
        self.assertIn("Resource\\RabbitFX\\FXMap = ref null", reset)
        self.assertNotIn("Resource\\RabbitFX\\Glowmap = ref null", reset)
        self.assertNotIn("$\\RabbitFX\\brightness = 0", reset)

    def test_breathing_writes_wave_and_constants(self):
        _write_ini(self.ini_path, MATERIAL_SECTION)
        node = _make_node(
            breath_mode='SINGLE',
            enable_breath=True,
            breath_brightness_min=1.0,
            breath_brightness_max=8.0,
            target_items=[_item(_fake_object())],
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn(pro._BREATH_BEGIN, content)
        self.assertIn("$rfxpro_abcd1234_frame = $rfxpro_abcd1234_frame + 0.3", content)
        self.assertIn("if $rfxpro_abcd1234_frame >= 200", content)
        self.assertIn(
            "$\\RabbitFX\\brightness = 1 + (8 - 1) * $rfxpro_abcd1234_glow / 200", content
        )
        self.assertIn("global persist $rfxpro_abcd1234_frame = 0", content)
        self.assertIn("global $rfxpro_abcd1234_valve = 0", content)
        self.assertIn("global $rfxpro_abcd1234_glow = 0", content)
        # 呼吸灯块必须整体在 Run 之前。
        self.assertLess(
            content.index(pro._BREATH_BEGIN),
            content.index("run = CommandList\\RabbitFX\\Run"),
        )

    def test_breathing_splits_step_across_target_draw_units(self):
        """同一个节点的多个目标绘制单元合计每帧只推进一次配置速度。"""
        first = _fake_object()
        second = _fake_object(SECOND_OBJECT)
        first_marker = '; MaterialRabbitFX first'
        second_marker = '; MaterialRabbitFX second'
        lines = [
            f'; [mesh:{first.name}]',
            'hash = fd054d1d',
            r'Resource\RabbitFX\Glowmap = ref Resource_Glowmap_1',
            first_marker,
            r'run = CommandList\RabbitFX\Run',
            'drawindexed = 10, 0, 0',
            f'; [mesh:{second.name}]',
            'hash = aa11bb22',
            r'Resource\RabbitFX\Glowmap = ref Resource_Glowmap_2',
            second_marker,
            r'run = CommandList\RabbitFX\Run',
            'drawindexed = 20, 0, 0',
        ]
        records = [
            {'object': first.name, 'type': 'Glowmap', 'marker': first_marker,
             'section': '[TextureOverride_Test]',
             'lines': [r'Resource\RabbitFX\Glowmap = ref Resource_Glowmap_1']},
            {'object': second.name, 'type': 'Glowmap', 'marker': second_marker,
             'section': '[TextureOverride_Test]',
             'lines': [r'Resource\RabbitFX\Glowmap = ref Resource_Glowmap_2']},
        ]
        node = _make_node(
            enable_breath=True,
            breath_step=0.3,
            target_items=[_item(first), _item(second)],
        )

        self.assertTrue(node._apply_material_records(
            {'[TextureOverride_Test]': lines}, records, [first, second], 'buffer'
        ))
        content = '\n'.join(lines)

        self.assertEqual(
            content.count("$rfxpro_abcd1234_frame = $rfxpro_abcd1234_frame + 0.15"),
            2,
        )
        self.assertNotIn(
            "$rfxpro_abcd1234_frame = $rfxpro_abcd1234_frame + 0.3",
            content,
        )

    def test_breathing_without_glow_switch_emits_nothing(self):
        """呼吸灯勾了但发光贴图参数关掉：不写任何东西（没有 Run 可执行）。"""
        _write_ini(self.ini_path, MATERIAL_SECTION)
        node = _make_node(
            enable_glow=False,
            enable_breath=True,
            breath_mode='RAINBOW',
            target_items=[_item(_fake_object())],
        )

        node.execute_postprocess(self.export_dir)

        self.assertEqual(_read_ini(self.ini_path), MATERIAL_SECTION)

    def test_breathing_off_keeps_static_values(self):
        """呼吸灯没勾时写静态值，不写三角波与 [Constants] 声明。"""
        _write_ini(self.ini_path, MATERIAL_SECTION)
        node = _make_node(
            breath_mode='SINGLE',
            enable_breath=False,
            target_items=[_item(_fake_object())],
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn("$\\RabbitFX\\brightness = 8.0", content)
        self.assertNotIn(pro._BREATH_BEGIN, content)
        self.assertNotIn("global persist $rfxpro_", content)

    def test_sync_copy_mode_writes_buffer_and_source(self):
        source_section = (
            "[TextureOverride_hair]\n"
            "[mesh:LOD0.cc33dd44-30030-0.Hair]\n"
            "hash = cc33dd44\n"
            "ib = ResourceHairIB\n"
            "drawindexed = 10, 0, 0\n"
        )
        _write_ini(self.ini_path, MATERIAL_SECTION + "\n" + source_section)
        source = _fake_object("LOD0.cc33dd44-30030-0.Hair")
        node = _make_node(
            enable_sync=True,
            sync_blendmode=1,
            sync_source_object=source,
            target_items=[_item(_fake_object())],
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn("[ResourceRabbitFXPro_abcd1234_FXBuffer]", content)
        self.assertIn(
            "ResourceRabbitFXPro_abcd1234_FXBuffer = copy Resource\\RabbitFX\\FXBuffer",
            content,
        )
        self.assertIn("$\\rabbitfx\\blendmode = 1", content)
        self.assertIn(
            "Resource\\RabbitFX\\SetFXBuffer = ref ResourceRabbitFXPro_abcd1234_FXBuffer",
            content,
        )
        self.assertIn("pre run = CommandList\\RabbitFX\\UpdateFXBuffer", content)
        self.assertIn("run = CommandList\\RabbitFX\\ERun", content)
        # 拷贝语句必须在提取源的绘制段里、且排在 ERun 之前执行。
        self.assertLess(
            content.index("ResourceRabbitFXPro_abcd1234_FXBuffer = copy"),
            content.index("run = CommandList\\RabbitFX\\ERun"),
        )
        self.assertLess(
            content.index("run = CommandList\\RabbitFX\\ERun"),
            content.index("drawindexed = 10, 0, 0"),
        )

    def test_sync_rwbuffer_mode_declares_rwbuffer(self):
        _write_ini(self.ini_path, MATERIAL_SECTION)
        node = _make_node(
            enable_sync=True,
            buffer_mode='RWBUFFER',
            target_items=[_item(_fake_object())],
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn("[ResourceRabbitFXPro_abcd1234_FXBuffer]", content)
        self.assertIn("type = RWBuffer", content)
        self.assertIn("ps-u4 = ResourceRabbitFXPro_abcd1234_FXBuffer", content)
        self.assertNotIn("= copy Resource\\RabbitFX\\FXBuffer", content)

    def test_object_without_fx_binding_keeps_file_untouched(self):
        untouched = (
            "[TextureOverride_other]\n"
            "[mesh:LOD0.fd054d1d-30030-0.Body]\n"
            "hash = fd054d1d\n"
            "ps-t0 = Resource_DiffuseMap_Body\n"
            "drawindexed = 10, 0, 0\n"
        )
        _write_ini(self.ini_path, untouched)
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)

        self.assertEqual(_read_ini(self.ini_path), untouched)

    def test_chained_nodes_only_touch_their_own_objects(self):
        _write_ini(self.ini_path, MATERIAL_SECTION + "\n" + FXMAP_SECTION)
        first = _make_node(
            buffer_tag='node0001',
            glow_brightness=20.0,
            target_items=[_item(_fake_object())],
        )
        second = _make_node(
            buffer_tag='node0002',
            glow_brightness=3.0,
            target_items=[_item(_fake_object(SECOND_OBJECT))],
        )

        first.execute_postprocess(self.export_dir)
        second.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        glow_section, fxmap_section = content.split(f"[{SECOND_SECTION}]", 1)
        self.assertIn("$\\RabbitFX\\brightness = 20.0", glow_section)
        self.assertNotIn("$\\RabbitFX\\brightness = 3.0", glow_section)
        self.assertIn("$\\RabbitFX\\brightness = 3.0", fxmap_section)
        self.assertNotIn("$\\RabbitFX\\brightness = 20.0", fxmap_section)

    # ──────────────── 与其它后处理节点的兼容性 ────────────────

    def test_generated_markers_are_ascii(self):
        """标记必须是纯 ASCII：配置文件清理节点会改写非 ASCII 文本。"""
        _write_ini(self.ini_path, MATERIAL_SECTION)
        node = _make_node(enable_breath=True, target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        for marker in (
            pro._PARAM_BEGIN, pro._PARAM_END, pro._BREATH_BEGIN, pro._BREATH_END,
            pro._RESET_BEGIN, pro._RESET_END,
        ):
            self.assertTrue(marker.isascii(), f"标记含非 ASCII 字符: {marker}")
        # 注入的参数/呼吸灯/复位块本身也不能含非 ASCII，否则会被清理节点改写。
        block = content[content.index(pro._PARAM_BEGIN):content.index(pro._RESET_END)]
        self.assertTrue(block.isascii(), "注入块里出现非 ASCII 文本")

    def test_legacy_chinese_markers_are_still_cleaned(self):
        """早期中文标记写的块也要能被清掉，不能因为改标记而累积重复。"""
        legacy_block = (
            "; === RabbitFXPro 参数 ===\n"
            "$\\RabbitFX\\H = 99.0\n"
            "; === 结束 RabbitFXPro 参数 ===\n"
        )
        _write_ini(self.ini_path, MATERIAL_SECTION + legacy_block)
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertNotIn("99.0", content)
        self.assertEqual(content.count(pro._PARAM_BEGIN), 1)

    def test_shader_replace_section_without_draw_is_patched(self):
        """着色器替换：段内没有 drawindexed，用 run = 行当锚点。"""
        section = (
            f"[{GLOW_SECTION}]\n"
            f"[mesh:{MESH_NAME}]\n"
            "hash = fd054d1d\n"
            "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_5_Body\n"
            "run = CommandList\\RabbitFX\\Run\n"
            "run = CustomShader_Body\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn(pro._PARAM_BEGIN, content)
        self.assertLess(content.index(pro._PARAM_BEGIN), content.index("run = CustomShader_Body"))
        self.assertLess(content.index("run = CustomShader_Body"), content.index(pro._RESET_BEGIN))

    def test_callback_commandlist_section_is_patched(self):
        """EFMI 合并骨骼把绘制放在 CommandList 回调段，同样要注入。"""
        section = (
            "[CommandList_Draw_Body]\n"
            f"[mesh:{MESH_NAME}]\n"
            "hash = fd054d1d\n"
            "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_5_Body\n"
            "run = CommandList\\RabbitFX\\Run\n"
            "drawindexed = 10, 0, 0\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn(pro._PARAM_BEGIN, content)
        self.assertIn("$\\RabbitFX\\brightness = 8.0", content)

    def test_ntemifx_namespace_is_left_untouched(self):
        """NTEMI 逻辑用的是 NTEMIFX 命名空间，本节点不该动它。"""
        section = (
            f"[{GLOW_SECTION}]\n"
            f"[mesh:{MESH_NAME}]\n"
            "hash = fd054d1d\n"
            "Resource\\NTEMIFX\\Glowmap = ref Resource_Glowmap_5_Body\n"
            "run = CommandList\\NTEMIFX\\Run\n"
            "drawindexed = 10, 0, 0\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)

        self.assertEqual(_read_ini(self.ini_path), section)

    def test_ui_hints_when_material_node_missing(self):
        node = _make_node(target_items=[_item(_fake_object())])
        node.id_data = _fake_tree([node], with_material_node=False)
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        labels = [entry[1] for entry in layout.log if entry[0] == "label"]
        self.assertTrue(any("没有「材质转资源」节点" in text for text in labels))

    # ──────────────── 串联（透传）与接线 ────────────────

    def test_chain_passthrough_sockets(self):
        """一进一出的后处理 socket：链条能穿过本节点继续往后接。"""
        node = pro.SSMTNode_PostProcess_RabbitFXPro()
        node.buffer_tag = ""  # 真实 Blender 里由属性默认值提供

        node.init(None)

        self.assertEqual([s.bl_idname for s in node.inputs], ['SSMTSocketPostProcess'])
        self.assertEqual([s.bl_idname for s in node.outputs], ['SSMTSocketPostProcess'])
        self.assertTrue(node.buffer_tag, "init 应生成唯一的缓冲标识")

    def test_wiring_registration_menu_and_multi_instance(self):
        """注册表 / 菜单 / 嵌套显示名都挂上，且不在单实例限制表里。"""
        init_src = (ROOT / "blueprint" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('"node_postprocess_rabbitfx_pro"', init_src)

        menu_src = (ROOT / "blueprint" / "node_menu.py").read_text(encoding="utf-8")
        self.assertIn("SSMTNode_PostProcess_RabbitFXPro", menu_src)

        nest_src = (ROOT / "blueprint" / "node_nest.py").read_text(encoding="utf-8")
        self.assertIn("SSMTNode_PostProcess_RabbitFXPro", nest_src)

        model_src = (ROOT / "blueprint" / "model.py").read_text(encoding="utf-8")
        single_instance_block = model_src.split(
            "_SINGLE_INSTANCE_POSTPROCESS_LABELS = {", 1
        )[1].split("}", 1)[0]
        self.assertNotIn(
            "SSMTNode_PostProcess_RabbitFXPro",
            single_instance_block,
            "本节点必须允许多个串联，不能进单实例限制表",
        )

    def test_ntmi_modimp_export_runs_the_node(self):
        """NTMI/ModImp 导出走白名单，本节点要在名单里（否则静默不执行）。"""
        src = (ROOT / "blueprint" / "ntmi_export_modimp.py").read_text(encoding="utf-8")
        compatible_block = src.split("COMPATIBLE_POSTPROCESS_NODE_TYPES = {", 1)[1].split("}", 1)[0]
        self.assertIn("SSMTNode_PostProcess_RabbitFXPro", compatible_block)

    def test_validate_export_configuration_flags_shared_object(self):
        shared = _fake_object()
        first = _make_node(target_items=[_item(shared)])
        second = _make_node(target_items=[_item(shared)])
        tree = types.SimpleNamespace(nodes=[first, second])
        first.id_data = tree
        second.id_data = tree
        first.name = "pro.001"
        second.name = "pro.002"

        with self.assertRaises(ValueError) as ctx:
            second.validate_export_configuration()
        self.assertIn("重复接管", str(ctx.exception))

    def test_probe_fx_textures_uses_marked_slots_fallback(self):
        class _NoPrefixProbe:
            def find_matching_materials(self, _obj, _texture_type):
                return []

        original = pro._PROBE
        pro._PROBE = _NoPrefixProbe()
        try:
            marked = types.SimpleNamespace(
                name="SomeBody",
                type="MESH",
                get=lambda _key, _default=None: json.dumps(
                    {"ps-t5": {"mark_name": "FXMap"}}
                ),
            )
            self.assertEqual(pro._probe_fx_textures(marked), (False, True))
            plain = types.SimpleNamespace(
                name="Plain", type="MESH", get=lambda _key, _default=None: ""
            )
            self.assertEqual(pro._probe_fx_textures(plain), (False, False))
        finally:
            pro._PROBE = original

    # ──────────────── HIMI（崩坏 3）→ HI3FX ────────────────

    HIMI_SECTION = (
        f"[{GLOW_SECTION}]\n"
        f"[mesh:{MESH_NAME}]\n"
        "hash = fd054d1d\n"
        "match_first_index = 0\n"
        "Resource\\HI3FX\\GlowMap = ref Resource_Glowmap_5_Body\n"
        "$\\HI3FX\\brightness = 5\n"
        "run = CommandList\\HI3FX\\Run\n"
        "drawindexed = 52688, 0, 3\n"
        "Resource\\HI3FX\\GlowMap = ref null\n"
        "run = CommandList\\HI3FX\\Reset\n"
    )

    def _use_himi(self):
        sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "HIMI"

    def test_himi_static_glow_uses_lowercase_hi3fx_params(self):
        """HIMI 注入的是 $\\HI3FX\\h/s/v（小写），不是 RabbitFX 的 H/S/V。"""
        self._use_himi()
        _write_ini(self.ini_path, self.HIMI_SECTION)
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn(pro._PARAM_BEGIN, content)
        self.assertIn("$\\HI3FX\\h = 0.0", content)
        self.assertIn("$\\HI3FX\\s = 0.0", content)
        self.assertIn("$\\HI3FX\\v = 0.0", content)
        self.assertIn("$\\HI3FX\\brightness = 8.0", content)
        self.assertIn("$\\HI3FX\\interpolate = 1.0", content)
        self.assertNotIn("$\\RabbitFX\\H", content)
        self.assertNotIn("$\\RabbitFX\\brightness", content)
        # 参数必须排在 HI3FX 的 Run 之前。
        self.assertLess(
            content.index(pro._PARAM_BEGIN),
            content.index("run = CommandList\\HI3FX\\Run"),
        )

    def test_himi_reset_uses_hi3fx_reset_command_list(self):
        """HI3FX 的 Run 不复位变量，所以复位必须走 CommandList\\HI3FX\\Reset。

        照搬 RabbitFX 的「写 0 再 Run 一次」会让 ``$brightness`` 一直停在 0，
        后续绘制全黑——这里断言 HIMI 下**不**生成任何 ``= 0`` 的复位参数行。
        """
        self._use_himi()
        bind_only = (
            f"[{GLOW_SECTION}]\n"
            f"[mesh:{MESH_NAME}]\n"
            "hash = fd054d1d\n"
            "Resource\\HI3FX\\GlowMap = ref Resource_Glowmap_5_Body\n"
            "$\\HI3FX\\brightness = 5\n"
            "run = CommandList\\HI3FX\\Run\n"
            "drawindexed = 52688, 0, 3\n"
        )
        _write_ini(self.ini_path, bind_only)
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        reset = content.split(pro._RESET_BEGIN, 1)[1].split(pro._RESET_END, 1)[0]
        self.assertIn("run = CommandList\\HI3FX\\Reset", reset)
        self.assertNotIn("$\\HI3FX\\brightness = 0", reset)
        self.assertNotIn("$\\HI3FX\\h = 0", reset)
        self.assertNotIn("$\\HI3FX\\interpolate = 0", reset)
        # 复位排在绘制之后。
        self.assertLess(
            content.index("drawindexed = 52688, 0, 3"), content.index(pro._RESET_BEGIN)
        )

    def test_himi_reset_is_not_duplicated_when_material_node_already_wrote_it(self):
        """材质转资源已经写过 Reset 时，本节点的复位块被去重掉，不重复写。"""
        self._use_himi()
        _write_ini(self.ini_path, self.HIMI_SECTION)
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertNotIn(pro._RESET_BEGIN, content)
        self.assertEqual(content.count("run = CommandList\\HI3FX\\Reset"), 1)
        self.assertEqual(content.count("Resource\\HI3FX\\GlowMap = ref null"), 1)

    def test_himi_skips_sync_and_colorshift(self):
        """HI3FX 没有 SetFXBuffer / ERun / ColorShift，两项都不生成任何行。"""
        self._use_himi()
        section = (
            f"[{SECOND_SECTION}]\n"
            f"[mesh:{SECOND_OBJECT}]\n"
            "hash = aa11bb22\n"
            "Resource\\HI3FX\\GlowMap = ref Resource_Glowmap_9_Body\n"
            "Resource\\HI3FX\\FXMap = ref Resource_FXMap_Body\n"
            "run = CommandList\\HI3FX\\Run\n"
            "drawindexed = 100, 0, 0\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(
            enable_sync=True,
            enable_colorshift=True,
            cs_h=-105.0,
            target_items=[_item(_fake_object(SECOND_OBJECT))],
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn(pro._PARAM_BEGIN, content)
        self.assertNotIn(pro._CS_BEGIN, content)
        self.assertNotIn("ColorShift", content)
        self.assertNotIn("SetFXBuffer", content)
        self.assertNotIn("FXBuffer", content)
        self.assertNotIn("blendmode", content)

    def test_himi_breathing_writes_hi3fx_params(self):
        """呼吸灯同样按命名空间写变量（HIMI 下是 $\\HI3FX\\h）。"""
        self._use_himi()
        _write_ini(self.ini_path, self.HIMI_SECTION)
        node = _make_node(
            enable_breath=True,
            breath_mode='RAINBOW',
            target_items=[_item(_fake_object())],
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn(pro._BREATH_BEGIN, content)
        self.assertIn("$\\HI3FX\\h = ", content)
        self.assertIn("$\\HI3FX\\s = 0", content)
        self.assertIn("$\\HI3FX\\brightness = 8", content)
        self.assertNotIn("$\\RabbitFX\\H", content)

    def test_himi_breathing_combo_mode_has_no_rabbitfx_leak(self):
        """COMBO 模式的三条变量行同样不能漏出 RabbitFX 命名空间。"""
        self._use_himi()
        _write_ini(self.ini_path, self.HIMI_SECTION)
        node = _make_node(
            enable_breath=True,
            breath_mode='COMBO',
            target_items=[_item(_fake_object())],
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        breath = content.split(pro._BREATH_BEGIN, 1)[1].split(pro._BREATH_END, 1)[0]
        self.assertNotIn("RabbitFX", breath)
        self.assertIn("$\\HI3FX\\h = ", breath)
        self.assertIn("$\\HI3FX\\s = ", breath)
        self.assertIn("$\\HI3FX\\v = ", breath)
        self.assertIn("$\\HI3FX\\brightness = ", breath)
        self.assertIn("$\\HI3FX\\interpolate = ", breath)

    def test_hi3fx_namespace_is_left_untouched_outside_himi(self):
        """非 HIMI 逻辑（这里是 ZZMI/RabbitFX）不该动 HI3FX 的绑定。"""
        _write_ini(self.ini_path, self.HIMI_SECTION)
        node = _make_node(target_items=[_item(_fake_object())])

        node.execute_postprocess(self.export_dir)

        self.assertEqual(_read_ini(self.ini_path), self.HIMI_SECTION)

    def test_profile_label_reports_missing_capabilities(self):
        """面板上要能看出当前逻辑用的是哪套命名空间、少了哪些能力。"""
        self._use_himi()
        node = _make_node()
        layout = _FakeLayout()

        node.draw_buttons(None, layout)

        labels = [entry[1] for entry in layout.log if entry[0] == "label"]
        self.assertTrue(any("HI3FX" in text for text in labels), labels)
        self.assertTrue(any("W-Engine 同步" in text for text in labels))

    # ──────────────── ini_scope 结构化圈定（实机 bug 回归） ────────────────

    def test_part_name_matching_only_accepts_same_part(self):
        """部件名口径：剥掉 LOD / IB 编号 / ``_copy`` 后必须相等，序号也要一致。"""
        self.assertEqual(
            pro._part_name("LOD0.c209c22b-45087-0.右手.001_copy"), "右手.001"
        )
        self.assertEqual(pro._part_name("LOD0.c209c22b-45087-0.双乳钩"), "双乳钩")
        # 序号分隔符写法可以不同，序号本身必须一致。
        self.assertTrue(pro._part_names_equal("右手.001", "右手_001"))
        self.assertFalse(pro._part_names_equal("右手.001", "右手.002"))
        self.assertFalse(pro._part_names_equal("右手", "右手.001"))
        self.assertFalse(pro._part_names_equal("右手.001", "右乳球.001"))

    def test_near_part_under_shared_ib_is_not_matched(self):
        """同 IB 下近似名不误匹配：``右手.001`` 不是 ``右乳球.001`` 的目标。

        实机 bug：``_extract_resource_suffix`` 只取到 IB 编号，``_mesh_name_matches``
        后缀互相包含 → 同一 IB 的所有 mesh 都被当成目标，参数写进了别的部件。
        """
        section = (
            f"[{SHARED_IB_SECTION}]\n"
            "hash = c209c22b\n"
            f"[mesh:{SHARED_IB_TARGET}]\n"
            "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_1\n"
            "run = CommandList\\RabbitFX\\Run\n"
            "drawindexed = 100, 0, 0\n"
            f"[mesh:{SHARED_IB_NEAR}]\n"
            "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_2\n"
            "run = CommandList\\RabbitFX\\Run\n"
            "drawindexed = 200, 0, 0\n"
        )
        # 段里根本没有的部件（同 IB 的另一个部件名）：不能借着「同 IB」乱注，
        # 也不能走整段回退（段里明明有 mesh 注释）。
        _write_ini(self.ini_path, section)
        node = _make_node(
            target_items=[_item(_fake_object("LOD0.c209c22b-30030-0.双乳钩"))]
        )
        node.execute_postprocess(self.export_dir)
        self.assertEqual(_read_ini(self.ini_path), section)

        # 把「右手.001」当目标：只改它自己的单元，右乳球.001 的单元不受影响。
        node = _make_node(target_items=[_item(_fake_object(SHARED_IB_NEAR))])
        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)
        near_index = content.index(f"[mesh:{SHARED_IB_NEAR}]")
        self.assertEqual(content.count(pro._PARAM_BEGIN), 1)
        self.assertGreater(content.index(pro._PARAM_BEGIN), near_index)
        self.assertNotIn(pro._PARAM_BEGIN, content[:near_index])

        # 换成把「右乳球.001」当目标：同样只改自己的单元，右手.001 不受影响。
        _write_ini(self.ini_path, section)
        node = _make_node(target_items=[_item(_fake_object(SHARED_IB_TARGET))])
        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)
        near_index = content.index(f"[mesh:{SHARED_IB_NEAR}]")
        self.assertEqual(content.count(pro._PARAM_BEGIN), 1)
        self.assertLess(content.index(pro._PARAM_BEGIN), near_index)
        self.assertNotIn(pro._PARAM_BEGIN, content[near_index:])

    def test_params_before_draw_inside_condition_and_reset_before_chain_endif(self):
        """mesh 在 ``if $swapkey4 == 0`` 块内、块里还有嵌套材质切换 if。

        参数块插在该 if 块内、目标 draw 之前；复位块统一写在 draw 之后、chain 的
        ``endif`` **之前**（留在条件块内，实机要求不要写到 endif 之外）。
        """
        section = (
            f"[{SHARED_IB_SECTION}]\n"
            "hash = c209c22b\n"
            "if $swapkey4 == 0\n"
            f"    [mesh:{SHARED_IB_TARGET}]\n"
            "    Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_1\n"
            "    if $material == 1\n"
            "        run = CommandList\\RabbitFX\\Run\n"
            "        drawindexed = 100, 0, 0\n"
            "    endif\n"
            "    if $material == 2\n"
            "        drawindexed = 200, 0, 0\n"
            "    endif\n"
            "endif\n"
            f"[mesh:{SHARED_IB_NEAR}]\n"
            "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_2\n"
            "run = CommandList\\RabbitFX\\Run\n"
            "drawindexed = 300, 0, 0\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(target_items=[_item(_fake_object(SHARED_IB_TARGET))])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        params_index = content.index(pro._PARAM_BEGIN)
        # 参数块在 if 块内、目标 draw 之前（缩进跟随锚点行）。
        self.assertLess(content.index("if $material == 1"), params_index)
        self.assertLess(params_index, content.index("drawindexed = 100, 0, 0"))
        self.assertIn("        ; === RabbitFXPro Params ===", content)
        # 复位块在两个嵌套材质 if 之后、chain 的 endif 之前（条件块内）。
        reset_index = content.index(pro._RESET_BEGIN)
        reset_end = content.index(pro._RESET_END)
        self.assertEqual(content[:reset_index].count("endif"), 2)
        self.assertLess(content.index("drawindexed = 200, 0, 0"), reset_index)
        tail = content[reset_end + len(pro._RESET_END):].strip().splitlines()[0]
        self.assertEqual(tail.strip(), "endif")
        self.assertLess(reset_end, content.index(f"[mesh:{SHARED_IB_NEAR}]"))
        self.assertNotIn(pro._PARAM_BEGIN, content[content.index(f"[mesh:{SHARED_IB_NEAR}]"):])

        # 复位块在单元区间内，重跑幂等。
        node.execute_postprocess(self.export_dir)
        self.assertEqual(_read_ini(self.ini_path), content)

    def test_run_after_draw_keeps_params_before_draw(self):
        """块内 ``run`` 排在 draw 之后：参数仍插在 draw 前，并补一条 Run 生效。"""
        section = (
            f"[{SHARED_IB_SECTION}]\n"
            "hash = c209c22b\n"
            f"[mesh:{SHARED_IB_TARGET}]\n"
            "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_1\n"
            "drawindexed = 100, 0, 0\n"
            "run = CommandList\\RabbitFX\\Run\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(target_items=[_item(_fake_object(SHARED_IB_TARGET))])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        draw_index = content.index("drawindexed = 100, 0, 0")
        self.assertLess(content.index(pro._PARAM_BEGIN), draw_index)
        first_run = content.index("run = CommandList\\RabbitFX\\Run")
        self.assertLess(content.index(pro._PARAM_BEGIN), first_run)
        self.assertLess(first_run, draw_index)
        # 原有的那条 run 还在，另外补了一条让参数在本次绘制前生效。
        self.assertEqual(content.count("run = CommandList\\RabbitFX\\Run"), 2)
        self.assertLess(draw_index, content.index(pro._RESET_BEGIN))

    def test_two_targets_sharing_ib_each_keep_their_params(self):
        """两个目标共享同一 IB：各自单元都注入参数块，本轮互不剥离。"""
        section = (
            f"[{SHARED_IB_SECTION}]\n"
            "hash = c209c22b\n"
            f"[mesh:{SHARED_IB_TARGET}]\n"
            "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_1\n"
            "run = CommandList\\RabbitFX\\Run\n"
            "drawindexed = 100, 0, 0\n"
            f"[mesh:{SHARED_IB_NEAR}]\n"
            "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_2\n"
            "run = CommandList\\RabbitFX\\Run\n"
            "drawindexed = 200, 0, 0\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(
            target_items=[
                _item(_fake_object(SHARED_IB_TARGET)),
                _item(_fake_object(SHARED_IB_NEAR)),
            ]
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        near_index = content.index(f"[mesh:{SHARED_IB_NEAR}]")
        self.assertEqual(content.count(pro._PARAM_BEGIN), 2)
        self.assertEqual(content.count("$\\RabbitFX\\brightness = 8.0"), 2)
        self.assertEqual(content[:near_index].count(pro._PARAM_BEGIN), 1)
        self.assertEqual(content[near_index:].count(pro._PARAM_BEGIN), 1)

        # 重跑幂等：新一轮把上一轮的两块换成本轮的两块，不累积也不互相剥掉。
        node.execute_postprocess(self.export_dir)
        self.assertEqual(_read_ini(self.ini_path), content)

    def test_fallback_whole_section_without_mesh_comment(self):
        """段里没有 mesh 注释时的回退：整段按第一个 draw/run 定位，仍能注入。"""
        section = (
            f"[{SHARED_IB_SECTION}]\n"
            "hash = c209c22b\n"
            "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_1\n"
            "run = CommandList\\RabbitFX\\Run\n"
            "drawindexed = 100, 0, 0\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(target_items=[_item(_fake_object(SHARED_IB_TARGET))])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        draw_index = content.index("drawindexed = 100, 0, 0")
        self.assertLess(content.index(pro._PARAM_BEGIN), draw_index)
        self.assertLess(
            content.index("run = CommandList\\RabbitFX\\Run"), draw_index
        )
        self.assertLess(draw_index, content.index(pro._RESET_BEGIN))

    def test_adjacent_sibling_units_in_same_block_each_get_params(self):
        """同一 if 块内相邻的 3 个目标单元：每个都有自己的 Params（不吞并、不剥离）。

        实机 bug（4:15:54 导出）：``if $swapkey4 == 0`` 下乳钩.001 之后的
        音擎.002 / 珠子.002 / 肛塞.002 都没有参数块 —— 前一个单元的可写范围被扩到
        共享的 ``endif``，把兄弟单元整个吞进来，注入时把兄弟刚写好的块当成上一轮
        残留剥掉了。
        """
        unit_lines = []
        for index, name in enumerate(CHAIN_UNIT_NAMES, start=1):
            unit_lines.extend(
                [
                    f"    ; [mesh:{name}]",
                    f"    Resource\\ZZMI\\GlowMap = ref ResourceTexture_Glowmap_{index}.Glowmap",
                    f"    Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_{index}",
                    "    run = CommandList\\ZZMI\\SetTextures",
                    "    run = CommandList\\RabbitFX\\Run",
                    f"    drawindexed = {index * 100}, 0, 0",
                ]
            )
        section = (
            f"[{SHARED_IB_SECTION}]\n"
            "hash = c209c22b\n"
            "if $swapkey4 == 0\n"
            + "\n".join(unit_lines)
            + "\nendif\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(
            target_items=[_item(_fake_object(name)) for name in CHAIN_UNIT_NAMES]
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertEqual(content.count(pro._PARAM_BEGIN), 3)
        self.assertEqual(content.count(pro._RESET_BEGIN), 3)
        marks = [f"; [mesh:{name}]" for name in CHAIN_UNIT_NAMES]
        positions = [content.index(mark) for mark in marks]
        endif_index = content.index("endif")
        self.assertLess(positions[-1], endif_index)
        for index, start in enumerate(positions):
            stop = positions[index + 1] if index + 1 < len(positions) else endif_index
            segment = content[start:stop]
            draw_line = f"drawindexed = {(index + 1) * 100}, 0, 0"
            self.assertEqual(segment.count(pro._PARAM_BEGIN), 1, segment)
            # 参数在各自单元的 Run / draw 之前（单元之间互不吞并）。
            self.assertLess(
                segment.index(pro._PARAM_BEGIN),
                segment.index("run = CommandList\\RabbitFX\\Run"),
            )
            self.assertLess(
                segment.index("run = CommandList\\RabbitFX\\Run"),
                segment.index(draw_line),
            )
            # 每个兄弟单元的复位块都在自己的单元区间内、chain 的 endif 之前。
            self.assertEqual(segment.count(pro._RESET_BEGIN), 1, segment)

        # 重跑幂等：3 块各自替换成新的 3 块，不累积、不互相剥掉。
        node.execute_postprocess(self.export_dir)
        self.assertEqual(_read_ini(self.ini_path), content)

    def test_last_sibling_reset_stays_inside_endif_without_duplicates(self):
        """chain 最后一个兄弟（双乳钩形状）：复位块留在 endif 之前，不重复块外复位行。

        实机 bug：块内把 Reset 写全（``Glowmap = ref null`` / ``brightness = 0``），
        而 endif 之外还有材质转资源写的同样三行 → 块内块外两套等价复位。
        """
        section = (
            f"[{SHARED_IB_SECTION}]\n"
            "hash = c209c22b\n"
            "if $swapkey4 == 1\n"
            f"    ; [mesh:{SHARED_IB_TARGET}]\n"
            "    Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_1\n"
            "    run = CommandList\\RabbitFX\\Run\n"
            "    drawindexed = 100, 0, 0\n"
            "endif\n"
            "Resource\\RabbitFX\\Glowmap = ref null\n"
            "$\\RabbitFX\\brightness = 0\n"
            "run = CommandList\\RabbitFX\\Run\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(target_items=[_item(_fake_object(SHARED_IB_TARGET))])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertEqual(content.count(pro._PARAM_BEGIN), 1)
        reset_index = content.index(pro._RESET_BEGIN)
        reset_end = content.index(pro._RESET_END)
        draw_index = content.index("drawindexed = 100, 0, 0")
        endif_index = content.index("endif")
        # 复位块在 draw 之后、chain 的 endif 之前（留在条件块内，不写到 endif 之外）。
        self.assertLess(draw_index, reset_index)
        self.assertLess(reset_end, endif_index)
        # 块外材质转资源的三行不重复写进 Reset 块。
        reset_block = content[reset_index:reset_end]
        self.assertNotIn("Glowmap", reset_block)
        self.assertNotIn("brightness", reset_block)
        self.assertEqual(content.count("Resource\\RabbitFX\\Glowmap = ref null"), 1)
        self.assertEqual(content.count("$\\RabbitFX\\brightness = 0"), 1)
        # 块内 1 条 Run + 块外材质转资源的 1 条 Run，Reset 块里不再重复 Run。
        self.assertEqual(content.count("run = CommandList\\RabbitFX\\Run"), 2)
        # 该单元 draw 后没有块内复位行，Reset 仍要补上状态复位行。
        self.assertIn("$\\RabbitFX\\H = 0", reset_block)
        self.assertIn("$\\RabbitFX\\interpolate = 0", reset_block)

        node.execute_postprocess(self.export_dir)
        self.assertEqual(_read_ini(self.ini_path), content)

    def test_mid_sibling_reset_only_writes_missing_lines(self):
        """中间兄弟（乳钩形状）：块内 draw 后已有材质转资源复位行 → Reset 只写缺的行。"""
        section = (
            f"[{SHARED_IB_SECTION}]\n"
            "hash = c209c22b\n"
            "if $swapkey4 == 0\n"
            f"    ; [mesh:{SHARED_IB_TARGET}]\n"
            "    Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_1\n"
            "    run = CommandList\\RabbitFX\\Run\n"
            "    drawindexed = 100, 0, 0\n"
            "    Resource\\RabbitFX\\Glowmap = ref null\n"
            "    $\\RabbitFX\\brightness = 0\n"
            "    run = CommandList\\RabbitFX\\Run\n"
            f"    ; [mesh:{SHARED_IB_NEAR}]\n"
            "    Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_2\n"
            "    run = CommandList\\RabbitFX\\Run\n"
            "    drawindexed = 200, 0, 0\n"
            "endif\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(target_items=[_item(_fake_object(SHARED_IB_TARGET))])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        near_index = content.index(f"; [mesh:{SHARED_IB_NEAR}]")
        reset_index = content.index(pro._RESET_BEGIN)
        reset_end = content.index(pro._RESET_END)
        reset_block = content[reset_index:reset_end]
        # 只写块内还没写过的变量行；材质转资源已有的两行不重复。
        self.assertIn("$\\RabbitFX\\H = 0", reset_block)
        self.assertNotIn("Glowmap", reset_block)
        self.assertNotIn("brightness", reset_block)
        self.assertEqual(content.count("Resource\\RabbitFX\\Glowmap = ref null"), 1)
        self.assertEqual(content.count("$\\RabbitFX\\brightness = 0"), 1)
        # 复位块在 draw 之后、下一个兄弟 mesh 之前（条件块内）。
        self.assertLess(content.index("drawindexed = 100, 0, 0"), reset_index)
        self.assertLess(reset_end, near_index)
        # 兄弟单元不是目标，不受影响。
        self.assertNotIn(pro._PARAM_BEGIN, content[near_index:])

        node.execute_postprocess(self.export_dir)
        self.assertEqual(_read_ini(self.ini_path), content)

    def test_unconditional_unit_null_reset_lines_are_deduped(self):
        """无 chain 的单元（肛门触手形状）：材质转资源写 ``= null``，Reset 不重复。

        ``= null`` 与 ``= ref null`` 语义相同，去重时必须归一化，否则会漏判。
        """
        section = (
            f"[{SHARED_IB_SECTION}]\n"
            "hash = c209c22b\n"
            f"[mesh:{SHARED_IB_TARGET}]\n"
            "Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_1\n"
            "run = CommandList\\RabbitFX\\Run\n"
            "drawindexed = 100, 0, 0\n"
            "Resource\\RabbitFX\\Glowmap = null\n"
            "$\\RabbitFX\\brightness = 0\n"
            "run = CommandList\\RabbitFX\\Run\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(target_items=[_item(_fake_object(SHARED_IB_TARGET))])

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        reset_index = content.index(pro._RESET_BEGIN)
        reset_block = content[reset_index:content.index(pro._RESET_END)]
        self.assertNotIn("Glowmap", reset_block)
        self.assertNotIn("brightness", reset_block)
        self.assertEqual(content.count("Glowmap = null"), 1)
        self.assertNotIn("Glowmap = ref null", content)
        self.assertEqual(content.count("$\\RabbitFX\\brightness = 0"), 1)
        self.assertLess(content.index("drawindexed = 100, 0, 0"), reset_index)

        node.execute_postprocess(self.export_dir)
        self.assertEqual(_read_ini(self.ini_path), content)

    def test_reset_disabled_writes_only_params(self):
        """``enable_reset`` 关闭时不写任何复位块。"""
        _write_ini(self.ini_path, MATERIAL_SECTION)
        node = _make_node(
            enable_reset=False, target_items=[_item(_fake_object())]
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertIn(pro._PARAM_BEGIN, content)
        self.assertNotIn(pro._RESET_BEGIN, content)

    def test_if_else_branches_each_keep_their_own_params_and_reset(self):
        """同一个 ``if/else`` 两支各有目标：各自的参数与复位都留在自己那一支里。

        被 ``else`` 截断的单元（then 分支的最后一个兄弟）一旦把可写范围扩到所属
        ``if`` 的 ``endif``，else 分支就被整个包进来：复位块落进另一支的参数块中间，
        排在该支 ``run``/``drawindexed`` **之前** —— 那一支物体的 RabbitFX 贴图静默
        全灭（无任何告警），末兄弟的复位块还会跑到 ``endif`` 之外。
        """
        section = (
            f"[{SHARED_IB_SECTION}]\n"
            "hash = c209c22b\n"
            "if $swapkey4 == 0\n"
            f"    ; [mesh:{SHARED_IB_TARGET}]\n"
            "    Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_1\n"
            "    run = CommandList\\RabbitFX\\Run\n"
            "    drawindexed = 100, 0, 0\n"
            "else\n"
            f"    ; [mesh:{SHARED_IB_NEAR}]\n"
            "    Resource\\RabbitFX\\Glowmap = ref Resource_Glowmap_2\n"
            "    run = CommandList\\RabbitFX\\Run\n"
            "    drawindexed = 200, 0, 0\n"
            "endif\n"
        )
        _write_ini(self.ini_path, section)
        node = _make_node(
            target_items=[
                _item(_fake_object(SHARED_IB_TARGET)),
                _item(_fake_object(SHARED_IB_NEAR)),
            ]
        )

        node.execute_postprocess(self.export_dir)
        content = _read_ini(self.ini_path)

        self.assertEqual(content.count(pro._PARAM_BEGIN), 2, content)
        self.assertEqual(content.count(pro._RESET_BEGIN), 2, content)
        else_index = content.index("else")
        endif_index = content.index("endif")

        # then 分支：参数在 run 之前，复位块在 draw 之后、``else`` 之前。
        then_segment = content[:else_index]
        self.assertEqual(then_segment.count(pro._PARAM_BEGIN), 1, then_segment)
        self.assertEqual(then_segment.count(pro._RESET_BEGIN), 1, then_segment)
        self.assertLess(
            then_segment.index(pro._PARAM_BEGIN),
            then_segment.index("run = CommandList\\RabbitFX\\Run"),
        )
        self.assertLess(
            then_segment.index("drawindexed = 100, 0, 0"),
            then_segment.index(pro._RESET_BEGIN),
        )

        # else 分支：同样自带一套，且不越出 ``endif``。
        else_segment = content[else_index:endif_index]
        self.assertEqual(else_segment.count(pro._PARAM_BEGIN), 1, else_segment)
        self.assertEqual(else_segment.count(pro._RESET_BEGIN), 1, else_segment)
        self.assertLess(
            else_segment.index(pro._PARAM_BEGIN),
            else_segment.index("run = CommandList\\RabbitFX\\Run"),
        )
        self.assertLess(
            else_segment.index("drawindexed = 200, 0, 0"),
            else_segment.index(pro._RESET_BEGIN),
        )

        # 重跑幂等：不累积、不漂移。
        node.execute_postprocess(self.export_dir)
        self.assertEqual(_read_ini(self.ini_path), content)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_mesh_create_helper_material_graph_test_pkg"
for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils", f"{PKG}.ui", f"{PKG}.ui.wwmi"):
    package = _install_module(package_name)
    package.__path__ = []

from tests import _real_modules as _real_modules
_real_modules.register_real_common_modules(f"{PKG}.common")


class _FakeImage:
    def __init__(self, filepath):
        self.filepath = filepath
        self.name = os.path.basename(filepath)
        self.alpha_mode = ""
        self.colorspace_settings = types.SimpleNamespace(is_data=False, name="")


class _FakeImageCollection:
    def load(self, filepath):
        return _FakeImage(filepath)


class _FakeSocket:
    def __init__(self, name=""):
        self.name = name
        self.default_value = None


class _FakeSocketMap(dict):
    def __missing__(self, key):
        socket = _FakeSocket(str(key))
        self[key] = socket
        return socket


class _FakeNode:
    def __init__(self, bl_idname):
        self.bl_idname = bl_idname
        self.type = {
            "ShaderNodeBsdfPrincipled": "BSDF_PRINCIPLED",
            "ShaderNodeOutputMaterial": "OUTPUT_MATERIAL",
            "ShaderNodeTexImage": "TEX_IMAGE",
            "ShaderNodeBsdfDiffuse": "BSDF_DIFFUSE",
            "ShaderNodeBsdfTransparent": "BSDF_TRANSPARENT",
            "ShaderNodeMixShader": "MIX_SHADER",
            "ShaderNodeNormalMap": "NORMAL_MAP",
            "ShaderNodeSeparateColor": "SEPARATE_COLOR",
            "ShaderNodeRGBCurve": "CURVE_RGB",
            "ShaderNodeCombineColor": "COMBINE_COLOR",
            "ShaderNodeMath": "MATH",
        }.get(bl_idname, bl_idname)
        self.inputs = _FakeSocketMap()
        self.outputs = _FakeSocketMap()
        self.location = types.SimpleNamespace(x=0, y=0)
        self.image = None
        self.name = bl_idname
        self.label = bl_idname
        self.width = 140
        self.uv_map = ""
        self.space = ""
        self.operation = ""
        self.use_clamp = False
        self.is_active_output = self.bl_idname == "ShaderNodeOutputMaterial"
        self.mapping = types.SimpleNamespace(
            initialize=lambda: None,
            update=lambda: None,
            curves=[
                types.SimpleNamespace(points=[types.SimpleNamespace(location=(0.0, 0.0)), types.SimpleNamespace(location=(1.0, 1.0))]),
                types.SimpleNamespace(points=[types.SimpleNamespace(location=(0.0, 0.0)), types.SimpleNamespace(location=(1.0, 1.0))]),
                types.SimpleNamespace(points=[types.SimpleNamespace(location=(0.0, 0.0)), types.SimpleNamespace(location=(1.0, 1.0))]),
            ],
        )


class _FakeNodeList(list):
    def new(self, type):
        node = _FakeNode(type)
        self.append(node)
        return node

    def get(self, name):
        for node in self:
            if getattr(node, "name", None) == name:
                return node
        return None

    def remove(self, node):
        list.remove(self, node)


class _FakeLinks:
    def __init__(self):
        self.created = []

    def new(self, from_socket, to_socket):
        self.created.append((from_socket, to_socket))


class _FakeNodeTree:
    def __init__(self):
        self.nodes = _FakeNodeList()
        self.links = _FakeLinks()


class _FakeMaterial:
    def __init__(self, name):
        self.name = name
        self.use_nodes = False
        self.node_tree = _FakeNodeTree()
        self.blend_method = ""
        self.use_transparency_overlap = True


class _FakeMaterialCollection(dict):
    def new(self, name):
        material = _FakeMaterial(name)
        self[name] = material
        return material


class _FakeObjectDataMaterials(list):
    pass


class _FakeObjectData:
    def __init__(self):
        self.materials = _FakeObjectDataMaterials()


class _FakeObject:
    def __init__(self, name):
        self.name = name
        self.data = _FakeObjectData()


_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(
        images=_FakeImageCollection(),
        materials=_FakeMaterialCollection(),
    ),
    types=types.SimpleNamespace(Collection=object),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module("bpy_extras", io_utils=types.SimpleNamespace(unpack_list=lambda seq: seq, axis_conversion=lambda **_kwargs: None))
_install_module("bpy_extras.io_utils", unpack_list=lambda seq: seq, axis_conversion=lambda **_kwargs: None)
_install_module(f"{PKG}.utils.format_utils", Fatal=RuntimeError, FormatUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.mesh_utils", MeshUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.obj_utils", ObjUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.color_attribute_utils", write_color_attribute_data=lambda *_args, **_kwargs: None)
_install_module(
    f"{PKG}.utils.texture_utils",
    TextureUtils=types.SimpleNamespace(find_texture=lambda prefix, suffix, directory: os.path.join(directory, f"{prefix}{suffix}")),
)
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace(Start=lambda *_args, **_kwargs: None))
_install_module(f"{PKG}.utils.vertexgroup_utils", VertexGroupUtils=types.SimpleNamespace())
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="GIMI"))
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(
        GIMI="GIMI",
        ZZMI="ZZMI",
        IdentityV="IdentityV",
        NTEMI="NTEMI",
        SnowBreak="SnowBreak",
    ),
)
_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=types.SimpleNamespace(
    extract_prefix_info=lambda name: (name.rsplit(".", 1)[0], ".") if "." in str(name) else None,
    parse_prefix_parts=lambda prefix: {
        "draw_ib": prefix.split("-")[0].split(".")[-1] if prefix else "",
        "component": prefix.split("-")[1] if "-" in str(prefix) else "",
    },
))
_install_module(f"{PKG}.common.d3d11_element", D3D11Element=object)
_install_module(f"{PKG}.ui.wwmi.extracted_object", ExtractedObjectHelper=types.SimpleNamespace())


module_path = Path(__file__).resolve().parents[1] / "common" / "mesh_create_helper.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.common.mesh_create_helper", module_path)
mesh_create_helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mesh_create_helper
spec.loader.exec_module(mesh_create_helper)

# 规范材质（common/marked_texture_material.py）的图构建复用同一套 MeshCreateHelper，
# 这里也把真实模块加载进来，好断言它走的是哪条路线。
_install_module(f"{PKG}.common.submesh_metadata", SubmeshMetadataResolver=object)
_install_module(f"{PKG}.common.texture_metadata_helper", TextureMetadataResolver=object)

marked_path = Path(__file__).resolve().parents[1] / "common" / "marked_texture_material.py"
marked_spec = importlib.util.spec_from_file_location(
    f"{PKG}.common.marked_texture_material", marked_path
)
marked_material = importlib.util.module_from_spec(marked_spec)
sys.modules[marked_spec.name] = marked_material
marked_spec.loader.exec_module(marked_material)


class MeshCreateHelperMaterialGraphTests(unittest.TestCase):
    """测试网格创建助手的材质图构建：不同逻辑名称下的节点结构差异"""

    def setUp(self):
        """每个测试前清空材质集合"""
        _fake_bpy.data.materials.clear()

    def test_multiple_logic_names_do_not_create_principled_bsdf(self):
        """测试多种逻辑名称下都不会创建 Principled BSDF 节点"""
        logic_names = ("GIMI", "ZZMI", "IdentityV", "NTEMI", "SnowBreak")

        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "abc12345-12-DiffuseMap.dds")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")

            for logic_name in logic_names:
                with self.subTest(logic_name=logic_name):
                    obj = _FakeObject(f"{logic_name}_Mesh")
                    mesh_create_helper.MeshCreateHelper.create_bsdf_with_diffuse_linked(
                        obj=obj,
                        mesh_name="LOD0.abc12345-12-0.Body",
                        directory=temp_dir,
                        logic_name=logic_name,
                    )

                    material = obj.data.materials[0]
                    node_types = [node.bl_idname for node in material.node_tree.nodes]
                    self.assertNotIn("ShaderNodeBsdfPrincipled", node_types)
                    self.assertIn("ShaderNodeBsdfDiffuse", node_types)
                    self.assertIn("ShaderNodeOutputMaterial", node_types)
                    image_nodes = [node for node in material.node_tree.nodes if node.bl_idname == "ShaderNodeTexImage"]
                    self.assertEqual(len(image_nodes), 1)
                    self.assertEqual(image_nodes[0].image.colorspace_settings.name, "sRGB")
                    expected_alpha_mode = "NONE" if logic_name == "IdentityV" else "CHANNEL_PACKED"
                    self.assertEqual(image_nodes[0].image.alpha_mode, expected_alpha_mode)
                    if logic_name == "IdentityV":
                        self.assertEqual(material.blend_method, "OPAQUE")
                        self.assertNotIn("ShaderNodeMixShader", node_types)
                        self.assertNotIn("ShaderNodeBsdfTransparent", node_types)
                    else:
                        self.assertEqual(material.blend_method, "BLEND")
                        self.assertFalse(material.use_transparency_overlap)
                        self.assertIn("ShaderNodeMixShader", node_types)
                        self.assertIn("ShaderNodeBsdfTransparent", node_types)

    def test_diffuse_material_name_always_carries_the_color_prefix(self):
        """「导入贴图材质去掉颜色贴图前缀」已移除，材质名固定为 DiffuseMap_{mesh}。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            diffuse_path = os.path.join(temp_dir, "abc12345-12-DiffuseMap.dds")
            with open(diffuse_path, "wb") as file_obj:
                file_obj.write(b"diffuse")

            obj = _FakeObject("LOD0.Body")
            mesh_create_helper.MeshCreateHelper.create_bsdf_with_diffuse_linked(
                obj=obj,
                mesh_name="d892c658-2256-0",
                directory=temp_dir,
                logic_name="GIMI",
            )

            self.assertEqual(obj.data.materials[0].name, "DiffuseMap_d892c658-2256-0")

    def test_removed_import_switches_stay_removed(self):
        """两个已移除的开关不得复活（属性与访问器都不能再出现在 GlobalProterties 上）。"""
        props = sys.modules[f"{PKG}.common.global_properties"].GlobalProterties
        for name in (
            "use_normal_map",
            "set_use_normal_map",
            "toggle_use_normal_map",
            "import_texture_material_strip_color_prefix",
            "toggle_import_texture_material_strip_color_prefix",
        ):
            self.assertFalse(hasattr(props, name), name)

    def test_blend_index_sentinels_follow_dxgi_width(self):
        helper = mesh_create_helper.MeshCreateHelper
        cases = (
            ("R8G8B8A8_UINT", np.array([[0, 0xFF]], dtype=np.uint8)),
            ("R16G16_UINT", np.array([[0, 0xFFFF]], dtype=np.uint16)),
            ("R32G32_UINT", np.array([[0, 0xFFFFFFFF]], dtype=np.uint32)),
        )
        for fmt, values in cases:
            with self.subTest(fmt=fmt):
                normalized = helper._normalize_blend_index_array(values, fmt)
                self.assertEqual(normalized.tolist(), [[0, -1]])

    def test_zero_weight_huge_index_is_excluded_from_group_scan(self):
        helper = mesh_create_helper.MeshCreateHelper
        indices = np.array([[0, 0xFFFFFFFF]], dtype=np.uint32)
        normalized = helper._normalize_blend_index_array(indices, "R32G32_UINT")
        valid = helper._valid_blend_channel_mask(
            normalized, np.array([[1.0, 0.0]], dtype=np.float32)
        )
        self.assertEqual(normalized[valid].tolist(), [0])

    def test_incomplete_merged_vgmap_is_rejected(self):
        helper = mesh_create_helper.MeshCreateHelper
        with self.assertRaises(RuntimeError):
            helper._normalize_and_validate_vg_map({"0": 3}, vg_count=2)


class SpecMaterialRouteTests(unittest.TestCase):
    """规范材质 ``<类型名>_<网格名>`` 必须走颜色贴图图，不得经过原理化 BSDF。"""

    def setUp(self):
        _fake_bpy.data.materials.clear()
        self.temp_dir = tempfile.mkdtemp(prefix="spec_route_")
        self.texture_path = os.path.join(self.temp_dir, "abc12345-12-DiffuseMap.dds")
        with open(self.texture_path, "wb") as handle:
            handle.write(b"diffuse")

    def _make_material(self):
        return _fake_bpy.data.materials.new("DiffuseMap_abc12345-12-0")

    def _node_types(self, material):
        return [node.bl_idname for node in material.node_tree.nodes]

    def _images(self, material):
        return [
            node.image for node in material.node_tree.nodes
            if node.bl_idname == "ShaderNodeTexImage"
        ]

    def test_spec_material_uses_the_color_texture_graph(self):
        for logic_name in ("GIMI", "ZZMI", "NTEMI"):
            with self.subTest(logic_name=logic_name):
                material = self._make_material()
                marked_material._apply_spec_material(
                    material, "NORMAL", self.texture_path, logic_name
                )

                node_types = self._node_types(material)
                self.assertNotIn("ShaderNodeBsdfPrincipled", node_types)
                self.assertIn("ShaderNodeBsdfDiffuse", node_types)
                self.assertIn("ShaderNodeBsdfTransparent", node_types)
                self.assertIn("ShaderNodeMixShader", node_types)
                self.assertIn("ShaderNodeOutputMaterial", node_types)
                self.assertEqual(material.blend_method, "BLEND")
                self.assertFalse(material.use_transparency_overlap)
                self.assertEqual(self._images(material)[0].alpha_mode, "CHANNEL_PACKED")

    def test_spec_material_ignores_the_socket_type(self):
        """各种标记类型建出来的图完全一样——规范材质不按类型接通道。"""
        shapes = []
        for socket_type in ("DIFFUSE", "NORMAL", "LIGHTMAP", "SURFACE", "BODYMASK", ""):
            material = self._make_material()
            marked_material._apply_spec_material(
                material, socket_type, self.texture_path, "ZZMI"
            )
            shapes.append(self._node_types(material))

        self.assertEqual(shapes, [shapes[0]] * len(shapes))

    def test_spec_material_for_identity_v_is_opaque(self):
        material = self._make_material()
        marked_material._apply_spec_material(
            material, "DIFFUSE", self.texture_path, "IdentityV"
        )

        node_types = self._node_types(material)
        self.assertNotIn("ShaderNodeBsdfPrincipled", node_types)
        self.assertNotIn("ShaderNodeMixShader", node_types)
        self.assertNotIn("ShaderNodeBsdfTransparent", node_types)
        self.assertEqual(material.blend_method, "OPAQUE")
        self.assertEqual(self._images(material)[0].alpha_mode, "NONE")

    def test_spec_material_alpha_follows_the_ignore_alpha_switch(self):
        """默认通道打包；开启「导入贴图时忽略透明度通道」后贴图 alpha 变「无」。"""
        props = sys.modules[f"{PKG}.common.global_properties"].GlobalProterties
        props.ignore_texture_alpha = staticmethod(lambda: True)
        try:
            material = self._make_material()
            marked_material._apply_spec_material(
                material, "DIFFUSE", self.texture_path, "ZZMI"
            )
        finally:
            del props.ignore_texture_alpha

        self.assertEqual(self._images(material)[0].alpha_mode, "NONE")
        self.assertIn("ShaderNodeMixShader", self._node_types(material))


if __name__ == "__main__":
    unittest.main()

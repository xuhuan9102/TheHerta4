import importlib.util
import sys
import types
import unittest
from unittest import mock
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _FakeOpsObject:
    def shape_key_remove(self, *args, **kwargs):
        raise AssertionError("bpy.ops.object.shape_key_remove should not be called")

    def select_all(self, *args, **kwargs):
        return None


PKG = "_preprocess_shape_key_apply_no_operator_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeVertex:
    def __init__(self, co):
        self.co = list(co)


class _FakeVertexCollection(list):
    def foreach_set(self, attr, values):
        if attr != "co":
            raise AssertionError(attr)
        flat = list(values)
        for index, vertex in enumerate(self):
            start = index * 3
            vertex.co = flat[start:start + 3]

    def foreach_get(self, attr, target):
        if attr != "co":
            raise AssertionError(attr)
        flat = []
        for vertex in self:
            flat.extend(vertex.co)
        for index, value in enumerate(flat):
            target[index] = value


class _FakeKeyPoint:
    def __init__(self, co):
        self.co = tuple(co)


class _FakeKeyPointCollection(list):
    def foreach_get(self, attr, target):
        if attr != "co":
            raise AssertionError(attr)
        flat = []
        for item in self:
            flat.extend(item.co)
        for index, value in enumerate(flat):
            target[index] = value


class _FakeShapeKey:
    def __init__(self, name, coords, value=0.0, mute=False):
        self.name = name
        self.value = value
        self.mute = mute
        self.data = _FakeKeyPointCollection(_FakeKeyPoint(co) for co in coords)


class _FakeShapeKeys:
    def __init__(self, key_blocks):
        self.key_blocks = key_blocks


class _FakeMeshData:
    def __init__(self, vertices, key_blocks):
        self.vertices = _FakeVertexCollection(_FakeVertex(vertex) for vertex in vertices)
        self.shape_keys = _FakeShapeKeys(key_blocks)
        self.loops = []

    def update(self):
        return None


class _FakeEvaluatedMesh:
    def __init__(self, vertices):
        self.vertices = _FakeVertexCollection(_FakeVertex(vertex) for vertex in vertices)


class _FakeEvaluatedObject:
    def __init__(self, vertices):
        self._mesh = _FakeEvaluatedMesh(vertices)
        self.cleared = False

    def to_mesh(self):
        return self._mesh

    def to_mesh_clear(self):
        self.cleared = True


class _FakeObject:
    def __init__(self, name, vertices, key_blocks, evaluated_vertices=None):
        self.name = name
        self.type = "MESH"
        self.data = _FakeMeshData(vertices, key_blocks)
        self.selected = False
        self._evaluated_object = _FakeEvaluatedObject(evaluated_vertices if evaluated_vertices is not None else vertices)

    def select_set(self, value):
        self.selected = bool(value)

    def shape_key_remove(self, shape_key):
        self.data.shape_keys.key_blocks.remove(shape_key)
        remaining = getattr(self.data.shape_keys, "key_blocks", None)
        if not remaining or len(remaining) <= 1:
            self.data.shape_keys = None

    def evaluated_get(self, _depsgraph):
        return self._evaluated_object


class _FakeObjectWithoutShapeKeyRemove(_FakeObject):
    shape_key_remove = None


_fake_view_layer = types.SimpleNamespace(objects=types.SimpleNamespace(active=None), update=lambda: None)
_fake_scene = types.SimpleNamespace(render=types.SimpleNamespace(use_simplify=True))
_fake_bpy_data_objects = {}
_install_module(
    "bpy",
    context=types.SimpleNamespace(
        view_layer=_fake_view_layer,
        scene=_fake_scene,
        evaluated_depsgraph_get=lambda: object(),
    ),
    data=types.SimpleNamespace(objects=_fake_bpy_data_objects),
    ops=types.SimpleNamespace(object=_FakeOpsObject()),
    path=types.SimpleNamespace(abspath=lambda value: value),
    types=types.SimpleNamespace(Object=object),
)
_install_module("bmesh")
_install_module("mathutils", Matrix=object, Vector=tuple)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="GIMI"))
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        copy_object_with_all_data=lambda: False,
        only_use_marked_texture=lambda: False,
        import_flip_scale_x=lambda: False,
        import_use_groups_as_ignore_pattern=lambda: False,
        import_merged_vgmap=lambda: False,
        remove_all_vgs=lambda: False,
        enable_non_mirror_workflow=lambda: False,
        ignore_muted_shape_keys=lambda: False,
        apply_all_modifiers=lambda: False,
        import_skip_empty_vertex_groups=lambda: False,
    ),
)
_install_module(f"{PKG}.common.logic_name", LogicName=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.non_mirror_workflow",
    NonMirrorWorkflowHelper=types.SimpleNamespace(),
)
_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=types.SimpleNamespace())
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(
        should_disable_copy_modifiers_on_export=lambda: False,
    ),
)
_install_module(f"{PKG}.blueprint.preprocess_cache", PreProcessCache=types.SimpleNamespace())
_install_module(f"{PKG}.utils.log_utils", LOG=types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None))
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace(start_stage=lambda *a, **k: None, end_stage=lambda *a, **k: None))
_install_module(f"{PKG}.utils.obj_utils", ObjUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.texture_utils", TextureUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.vertexgroup_utils", VertexGroupUtils=types.SimpleNamespace())
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(
        is_basis_shape_key_name=lambda name: str(name or "").strip().lower() == "basis",
        iter_exportable_shape_keys=lambda obj: (
            shape_key
            for index, shape_key in enumerate(
                getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", []) or []
            )
            if index != 0 and getattr(shape_key, "name", "") != "Basis"
        ),
        count_exportable_shape_keys=lambda obj: sum(
            1
            for index, shape_key in enumerate(getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", []))
            if index != 0 and getattr(shape_key, "name", "") != "Basis"
        ),
        bake_current_shape_key_mix_to_mesh=lambda obj, _stage_label="": (
            obj.data.vertices.foreach_set(
                "co",
                [component for vertex in obj._evaluated_object._mesh.vertices for component in vertex.co],
            ),
            obj.data.update(),
            obj._evaluated_object.to_mesh_clear(),
            True,
        )[-1],
        remove_non_basis_shape_keys=lambda obj, _stage_label="": (
            (_ for _ in ()).throw(RuntimeError("当前对象不支持 shape_key_remove，无法移除非 Basis 形态键。"))
            if not callable(getattr(obj, "shape_key_remove", None))
            else [
                obj.shape_key_remove(shape_key)
                for shape_key in reversed(list(getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", []))[1:])
            ]
        ),
    ),
)
_install_module(f"{PKG}.utils.collection_utils", CollectionUtils=types.SimpleNamespace())


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "preprocess.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.preprocess", module_path)
preprocess = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = preprocess
spec.loader.exec_module(preprocess)


class PreprocessShapeKeyApplyNoOperatorTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy_data_objects.clear()
        _fake_scene.render.use_simplify = True

    def test_modifier_validation_uses_type_specific_blender_properties(self):
        self.assertFalse(preprocess.PreProcessHelper._is_invalid_modifier(
            types.SimpleNamespace(type="UV_WARP", uv_layer=""),
        ))
        self.assertFalse(preprocess.PreProcessHelper._is_invalid_modifier(
            types.SimpleNamespace(type="VERTEX_WEIGHT_EDIT"),
        ))
        self.assertFalse(preprocess.PreProcessHelper._is_invalid_modifier(
            types.SimpleNamespace(type="VERTEX_WEIGHT_MIX"),
        ))
        self.assertTrue(preprocess.PreProcessHelper._is_invalid_modifier(
            types.SimpleNamespace(type="VERTEX_WEIGHT_PROXIMITY", target=None),
        ))
        self.assertFalse(preprocess.PreProcessHelper._is_invalid_modifier(
            types.SimpleNamespace(type="VERTEX_WEIGHT_PROXIMITY", target=object()),
        ))
        self.assertTrue(preprocess.PreProcessHelper._is_invalid_modifier(
            types.SimpleNamespace(type="NODES", node_group=None),
        ))
        self.assertTrue(preprocess.PreProcessHelper._is_invalid_modifier(
            types.SimpleNamespace(type="CURVE", object=None),
        ))
        self.assertTrue(preprocess.PreProcessHelper._is_invalid_modifier(
            types.SimpleNamespace(type="BOOLEAN", operand_type="OBJECT", object=None),
        ))
        self.assertFalse(preprocess.PreProcessHelper._is_invalid_modifier(
            types.SimpleNamespace(type="BOOLEAN", operand_type="COLLECTION", collection=object()),
        ))

    def test_apply_modifiers_restores_simplify_after_unexpected_failure(self):
        with mock.patch.object(
            preprocess.PreProcessHelper,
            "_apply_modifiers_with_simplify_disabled",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                preprocess.PreProcessHelper._apply_modifiers(["Mesh"])

        self.assertTrue(_fake_scene.render.use_simplify)

    def test_disable_copy_modifiers_removes_animated_modifiers(self):
        class _AnimatedModifier:
            def __init__(self, name):
                self.name = name
                self.show_viewport = True

        class _AnimatedModifierCollection(list):
            def __iter__(self):
                for modifier in list(super().__iter__()):
                    # 模拟关键帧求值把 show_viewport 重新写回 True。
                    modifier.show_viewport = True
                    yield modifier

        obj = _FakeObject("Mesh", [], [])
        obj.modifiers = _AnimatedModifierCollection(
            [_AnimatedModifier("碰撞推开"), _AnimatedModifier("矫正平滑")]
        )
        _fake_bpy_data_objects[obj.name] = obj

        preprocess.PreProcessHelper._disable_copy_modifiers([obj.name])

        self.assertEqual(list(obj.modifiers), [])

    def test_apply_modifiers_strict_mode_reports_missing_objects(self):
        with self.assertRaisesRegex(RuntimeError, "MissingMesh: object not found"):
            preprocess.PreProcessHelper._apply_modifiers_with_simplify_disabled(
                ["MissingMesh"],
                fail_on_error=True,
            )

    def test_apply_shape_keys_uses_object_api_without_bpy_operator(self):
        basis_coords = [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)]
        mixed_coords = [(2.0, 2.0, 2.0), (3.0, 3.0, 3.0)]
        key_blocks = [
            _FakeShapeKey("Basis", basis_coords, value=0.0),
            _FakeShapeKey("Smile", mixed_coords, value=1.0),
        ]
        obj = _FakeObject("Mesh", basis_coords, key_blocks, evaluated_vertices=mixed_coords)
        _fake_bpy_data_objects[obj.name] = obj

        preprocess.PreProcessHelper._apply_shape_keys([obj.name])

        self.assertEqual([tuple(vertex.co) for vertex in obj.data.vertices], mixed_coords)
        self.assertTrue(obj._evaluated_object.cleared)
        remaining_shape_keys = getattr(obj.data, "shape_keys", None)
        if remaining_shape_keys is None:
            return
        self.assertEqual(
            [shape_key.name for shape_key in remaining_shape_keys.key_blocks],
            ["Basis"],
        )

    def test_apply_shape_keys_reports_detailed_chinese_error_when_remove_method_missing(self):
        basis_coords = [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)]
        mixed_coords = [(2.0, 2.0, 2.0), (3.0, 3.0, 3.0)]
        key_blocks = [
            _FakeShapeKey("Basis", basis_coords, value=0.0),
            _FakeShapeKey("Smile", mixed_coords, value=1.0),
        ]
        obj = _FakeObjectWithoutShapeKeyRemove("BrokenMesh", mixed_coords, key_blocks)
        _fake_bpy_data_objects[obj.name] = obj

        with self.assertRaises(RuntimeError) as context:
            preprocess.PreProcessHelper._apply_shape_keys([obj.name])

        message = str(context.exception)
        self.assertIn("前处理中的形态键应用失败", message)
        self.assertIn("BrokenMesh", message)
        self.assertIn("shape_key_remove", message)


if __name__ == "__main__":
    unittest.main()

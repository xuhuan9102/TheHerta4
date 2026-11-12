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


PKG = "_preprocess_parallel_runtime_context_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_object = types.SimpleNamespace(
    name="Mesh",
    type="MESH",
    constraints=[],
    modifiers=[],
    vertex_groups=[],
    data=types.SimpleNamespace(
        vertices=[object()],
        loops=[object()] * 4,
        shape_keys=None,
    ),
)
_fake_copy_object = types.SimpleNamespace(
    name="Mesh_copy",
    type="MESH",
    constraints=[],
    modifiers=[],
    vertex_groups=[],
    data=types.SimpleNamespace(vertices=[], loops=[], shape_keys=None),
)


class _FakeObjectStore(dict):
    def __iter__(self):
        return iter(self.values())

    def remove(self, obj, do_unlink=True):
        self.pop(obj.name, None)


_fake_objects = _FakeObjectStore({"Mesh": _fake_object})
_fake_node_groups = {}
_install_module(
    "bpy",
    context=types.SimpleNamespace(),
    app=types.SimpleNamespace(binary_path=sys.executable),
    data=types.SimpleNamespace(
        filepath=str(Path(sys.executable)),
        objects=_fake_objects,
        node_groups=_fake_node_groups,
        libraries=types.SimpleNamespace(
            write=lambda path, _datablocks, compress=False: Path(path).write_bytes(b"blend")
        ),
    ),
)

_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        parallel_preprocess_instances=lambda: 1,
        parallel_preprocess_timeout_seconds=lambda: 1800,
        parallel_preprocess_keep_temp_files=lambda: False,
        parallel_blender_executable=lambda: "",
        enable_export_cache=lambda: False,
        enable_non_mirror_workflow=lambda: False,
        apply_all_modifiers=lambda: False,
    ),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        start_collecting=lambda: None,
        stop_collecting=lambda: None,
        get_log_content=lambda **_kwargs: "",
        _write_stdout_text=lambda *_args, **_kwargs: None,
    ),
)
_install_module(
    f"{PKG}.utils.process_log",
    configure_worker_output=lambda: None,
    REDIRECTED_LOG_ENV="HERTA_TEST_LOG_REDIRECTED",
)
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(
        cleanup_invalid_shapekeys=lambda _names: 0,
        save_as_mainfile_with_shape_key_recovery=lambda filepath, **_kwargs: Path(
            filepath
        ).write_bytes(b"blend"),
    ),
)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(
        runtime_blueprint_tree_name="",
        clear_direct_shapekey_position_records=lambda: None,
        set_capture_direct_shapekey_positions=lambda _value: None,
        get_direct_shapekey_position_records=lambda: {},
        set_runtime_blueprint_tree=lambda tree: setattr(
            blueprint_export_helper.BlueprintExportHelper,
            "runtime_blueprint_tree_name",
            tree.name,
        ),
    ),
)
blueprint_export_helper = sys.modules[f"{PKG}.blueprint.export_helper"]
_install_module(
    f"{PKG}.blueprint.preprocess",
    PreProcessHelper=types.SimpleNamespace(
        original_to_copy_map={},
        reset_runtime_state=lambda: None,
    ),
)
_install_module(f"{PKG}.blueprint.preprocess_cache", PreProcessCache=types.SimpleNamespace())


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "preprocess_parallel.py"
spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.preprocess_parallel",
    module_path,
)
preprocess_parallel = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = preprocess_parallel
spec.loader.exec_module(preprocess_parallel)


class PreprocessParallelRuntimeContextTests(unittest.TestCase):
    def setUp(self):
        _fake_objects.clear()
        _fake_objects.update({"Mesh": _fake_object})
        _fake_node_groups.clear()
        blueprint_export_helper.BlueprintExportHelper.runtime_blueprint_tree_name = ""
        os.environ.pop("HERTA_PREPROCESS_RESULT_LIBRARY", None)

    def test_build_jobs_persists_runtime_blueprint_tree_name(self):
        with tempfile.TemporaryDirectory(prefix="herta_parallel_context_") as temp:
            jobs = preprocess_parallel.ParallelPreprocessCoordinator._build_jobs(
                session_dir=temp,
                snapshot_path=str(Path(temp) / "snapshot.blend"),
                object_names=["Mesh"],
                config={
                    "instance_count": 1,
                    "runtime_blueprint_tree_name": "SSMT BluePrint",
                },
                preserve_shape_keys=False,
                capture_shape_keys=True,
            )

            with open(jobs[0]["manifest_path"], "r", encoding="utf-8") as handle:
                manifest = json.load(handle)

        self.assertEqual(
            manifest["runtime_blueprint_tree_name"],
            "SSMT BluePrint",
        )

    def test_runtime_config_captures_runtime_blueprint_tree_name(self):
        blueprint_export_helper.BlueprintExportHelper.runtime_blueprint_tree_name = (
            "SSMT BluePrint"
        )

        config = preprocess_parallel.ParallelPreprocessCoordinator._build_runtime_config(
            ["Mesh"],
            preserve_shape_keys=False,
            capture_shape_keys=True,
        )

        self.assertEqual(
            config["runtime_blueprint_tree_name"],
            "SSMT BluePrint",
        )

    def test_worker_restores_runtime_blueprint_tree_before_preprocess(self):
        expected_tree_name = "SSMT BluePrint"
        _fake_node_groups[expected_tree_name] = types.SimpleNamespace(
            name=expected_tree_name
        )
        preprocess_helper = sys.modules[f"{PKG}.blueprint.preprocess"].PreProcessHelper

        def _execute_objects_without_cache(_object_names):
            if (
                blueprint_export_helper.BlueprintExportHelper.runtime_blueprint_tree_name
                != expected_tree_name
            ):
                raise RuntimeError("runtime blueprint context missing")
            preprocess_helper.original_to_copy_map = {"Mesh": "Mesh_copy"}
            _fake_objects["Mesh_copy"] = _fake_copy_object

        preprocess_helper.execute_objects_without_cache = _execute_objects_without_cache

        with tempfile.TemporaryDirectory(prefix="herta_parallel_worker_") as temp:
            result_path = Path(temp) / "result.json"
            manifest_path = Path(temp) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "runtime_blueprint_tree_name": expected_tree_name,
                        "object_names": ["Mesh"],
                        "capture_shape_keys": True,
                        "preserve_shape_keys": False,
                        "result_path": str(result_path),
                        "result_blend_path": str(Path(temp) / "result.blend"),
                        "log_path": str(Path(temp) / "worker.log"),
                        "shapekey_records_path": str(
                            Path(temp) / "direct_shapekey_records.pkl"
                        ),
                        "job_index": 1,
                    }
                ),
                encoding="utf-8",
            )

            preprocess_parallel._run_worker(str(manifest_path))
            result = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["original_to_copy_map"], {"Mesh": "Mesh_copy"})


if __name__ == "__main__":
    unittest.main()

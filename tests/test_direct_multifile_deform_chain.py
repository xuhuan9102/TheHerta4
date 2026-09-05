import importlib.util
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_direct_multifile_deform_chain_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module("bpy")
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="ZZMI"))


def _ensure_alias(sections, resource_name, suffix, source_candidates=()):
    target = f"[{resource_name}{suffix}]"
    if target not in sections:
        source = next(
            (f"[{name}]" for name in source_candidates if name and f"[{name}]" in sections),
            None,
        )
        if source is None:
            raise AssertionError(f"missing alias source for {target}")
        sections[target] = list(sections[source])
    return target


_install_module(
    f"{PKG}.common.mod_path_compat",
    collect_stale_texture_override_position_alias_names=lambda *_args, **_kwargs: [],
    ensure_resource_alias_section=_ensure_alias,
    find_base_position_resource_name=lambda *_args, **kwargs: kwargs.get("fallback_name"),
    is_stale_texture_override_position_copy_desc_line=lambda *_args, **_kwargs: False,
    resolve_position_buffer_candidate=lambda *_args, **_kwargs: (None, None),
)
_install_module(f"{PKG}.utils.export_utils", ExportUtils=types.SimpleNamespace())
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None),
)
_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
    apply_position_override_in_place=lambda *_args, **_kwargs: None,
    get_model_vertex_count=lambda *_args, **_kwargs: 0,
    iter_drawib_models=lambda *_args, **_kwargs: [],
    normalize_runtime_name=lambda value: value,
)


def _load(module_name, relative_path):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


deform_chain = _load("blueprint.deform_chain", "blueprint/deform_chain.py")
direct_multifile = _load("blueprint.direct_export_multifile", "blueprint/direct_export_multifile.py")


class DirectMultiFileDeformChainTests(unittest.TestCase):
    def test_direct_multifile_and_shapekey_share_v3_chain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shader_source = root / "merge.hlsl"
            shader_source.write_text("shader", encoding="utf-8")

            config = types.SimpleNamespace(
                animation_swapkey="$swapkey100",
                active_swapkey="$active0",
                active_value=1,
                comment="",
                _get_shader_source_path=lambda: str(shader_source),
                _update_shader_file=lambda _path: True,
                _hash_to_resource_prefix=lambda value: value.replace("-", "_"),
                _compute_dispatch_group_count=lambda *_args, **_kwargs: 2,
                _write_ordered_dict_to_ini=lambda *_args, **_kwargs: None,
            )
            generator = direct_multifile.DirectMultiFileGenerator(
                config_node=config,
                multi_file_nodes=[],
                mod_export_path=str(root),
                exporter=types.SimpleNamespace(),
            )

            base_resource = "Resource_abc_Position"
            sections = OrderedDict([
                ("[Constants]", []),
                (f"[{base_resource}]", ["type = Buffer", "stride = 24", "filename = Meshes0000/base.buf"]),
                ("[Present]", [
                    "run = CustomShader_shape_Anim",
                ]),
                ("[CustomShader_shape_Anim]", [
                    f"    cs-u5 = copy {base_resource}_0",
                    f"    {base_resource} = ref cs-u5",
                ]),
            ])
            runtime_infos = {
                "abc": {
                    "actual_hash": "abc",
                    "hash_filter": "abc",
                    "base_resource_name": base_resource,
                    "vertex_count": 16,
                },
            }
            generated_states = {
                "abc": {
                    1: {"data_filename": "delta.buf", "map_filename": "map.buf"},
                },
            }

            generator._update_ini_sections(
                sections,
                preserved_tail_content="",
                target_ini_file=str(root / "mod.ini"),
                runtime_infos=runtime_infos,
                generated_states=generated_states,
            )

        present = "\n".join(sections["[Present]"])
        self.assertLess(
            present.index("run = CustomShader_abc_1Anim"),
            present.index("run = CustomShader_shape_Anim"),
        )
        shape_shader = "\n".join(sections["[CustomShader_shape_Anim]"])
        self.assertIn(f"cs-u5 = copy {base_resource}_mf", shape_shader)
        self.assertEqual(
            sections[f"[{base_resource}_mf]"],
            ["type = Buffer", "stride = 24"],
        )
        constants = "\n".join(sections["[Constants]"])
        self.assertNotIn("post run =", constants)
        self.assertIn(f"global persist $ssmt_mf_ran_{base_resource} = 0", constants)


if __name__ == "__main__":
    unittest.main()

"""Exercise the INI and file publication shared by all ZZMI LOD batches."""

from pathlib import Path
import sys
from types import ModuleType

import pytest


package = ModuleType("_zzmi_lod_publish_test")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package

from _zzmi_lod_publish_test.common.zzmi_lod_export import publish_lod_exports


def write_asset(folder, name, data):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def lod_inputs(tmp_path):
    exports = []
    for lod, hash_ in (("LOD0", "00112233"), ("LOD1", "44556677")):
        folder = tmp_path / "staging" / lod
        write_asset(folder, "Textures/trim$mask.dds", lod.encode("ascii"))
        write_asset(folder, "res/ResourceBody/palette.hlsl", b"shared shader")
        write_asset(folder, "Meshes/body.buf", lod.encode("ascii"))
        (folder / "character.ini").write_text(f"""
[Constants]
global $active = 0
[Present]
post $active = 0
[TextureOverrideBody]
hash = {hash_}
$active = 1
vb0 = ResourceBody
run = CustomShaderPaint
[ResourceBody]
filename = Meshes/body.buf
[ResourceMask]
filename = Textures/trim$mask.dds
[CustomShaderPaint]
ps = res/ResourceBody/palette.hlsl
[ShaderRegexNative]
shader_model = ps_5_0
[ShaderRegexNative.Pattern]
sample
[ShaderRegexNative.Pattern.Replace]
sample
""".lstrip(), encoding="utf-8")
        exports.append((lod, None, None, folder))
    return exports


def test_publish_merges_draws_and_keeps_both_lods_bound_to_their_own_assets(tmp_path):
    output = tmp_path / "mod" / "character.ini"
    publish_lod_exports(lod_inputs(tmp_path), output)
    ini = output.read_text(encoding="utf-8")

    assert ini.count("[Constants]") == ini.count("[Present]") == 1
    assert "[ShaderRegexNative_LOD0.Pattern.Replace]" in ini
    assert "[ShaderRegexNative_LOD1" not in ini
    for lod, hash_, prefix in (("LOD0", "00112233", ""), ("LOD1", "44556677", "LOD1_")):
        assert f"global ${lod.lower()}_active = 0" in ini
        assert f"post ${lod.lower()}_active = 0" in ini
        assert f"[TextureOverrideBody_{lod}]\nhash = {hash_}" in ini
        assert f"vb0 = ResourceBody_{lod}" in ini
        assert f"run = CustomShaderPaint_{lod}" in ini
        for kind, name in (("Meshes", "body.buf"), ("Textures", "trim$mask.dds")):
            target = f"{kind}/{prefix}{name}"
            assert f"filename = {target}" in ini
            assert (output.parent / target).read_bytes() == lod.encode("ascii")
        assert f"[CustomShaderPaint_{lod}]\nps = res/ResourceBody/palette.hlsl" in ini
    assert list((output.parent / "res").rglob("*.hlsl")) == [
        output.parent / "res/ResourceBody/palette.hlsl",
    ]


def test_buffers_only_keeps_the_existing_main_ini(tmp_path):
    output = tmp_path / "mod" / "character.ini"
    write_asset(output.parent, output.name, b"older ini")
    publish_lod_exports(lod_inputs(tmp_path), output, buffers_only=True)

    assert output.read_bytes() == b"older ini"
    assert (output.parent / "Meshes/body.buf").read_bytes() == b"LOD0"
    assert (output.parent / "Meshes/LOD1_body.buf").read_bytes() == b"LOD1"


def test_asset_name_collision_is_reported_before_writing_output(tmp_path):
    output = tmp_path / "mod" / "character.ini"
    write_asset(output.parent, output.name, b"older ini")
    write_asset(output.parent, "Meshes/body.buf", b"older buffer")
    exports = lod_inputs(tmp_path)
    write_asset(exports[0][3], "Meshes/LOD1_body.buf", b"reserved filename")

    with pytest.raises(ValueError, match="LOD1_body.buf"):
        publish_lod_exports(exports, output)
    assert output.read_bytes() == b"older ini"
    assert (output.parent / "Meshes/body.buf").read_bytes() == b"older buffer"

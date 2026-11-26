import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "common" / "apmi_capture.py"
SPEC = importlib.util.spec_from_file_location("_apmi_render_capture_test", MODULE_PATH)
CAPTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAPTURE)


def texcoords(uv0_format, uv1_offset):
    return {
        ("COLOR", 0): (0, "R8G8B8A8_UNORM"),
        ("TEXCOORD", 0): (4, uv0_format),
        ("TEXCOORD", 1): (uv1_offset, "R32G32_FLOAT"),
        ("TEXCOORD", 2): (uv1_offset, "R32G32_FLOAT"),
    }


def test_world_draw_reads_the_exported_texcoords():
    source = (20, texcoords("R32G32_FLOAT", 12))
    world = (16, texcoords("R16G16_FLOAT", 8))

    overrides = CAPTURE.plan_texcoord_overrides(
        {"first_vs": source, "second_vs": source},
        [("first_vs", world), ("second_vs", world)],
    )

    assert set(overrides) == {16}
    assert "vb1->ElementFormat(TEXCOORD, 0) = DXGI_FORMAT_R32G32_FLOAT" in overrides[16]
    assert "vb1->ElementOffset(TEXCOORD, 1) = 12" in overrides[16]
    assert "vb1->ElementOffset(TEXCOORD, 2) = 12" in overrides[16]


def test_unobserved_shader_is_not_allowed_to_use_alternate_stride():
    source = (20, texcoords("R32G32_FLOAT", 12))
    world = (16, texcoords("R16G16_FLOAT", 8))

    assert CAPTURE.plan_texcoord_overrides(
        {"first_vs": source, "second_vs": source},
        [("first_vs", world)],
    ) == {}

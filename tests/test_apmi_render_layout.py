import importlib.util
import sys
import types
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


def test_capture_prefers_workspace_files_and_skips_unavailable_skin_dumps(tmp_path):
    workspace = tmp_path / "APMICapture"
    deduped = workspace / "deduped"
    deduped.mkdir(parents=True)
    external = tmp_path / "FrameAnalysis"
    external.mkdir()
    shader = "1234567890abcdef"
    local_name = f"000001-cs-t0=12345678-cs={shader}.buf"
    deduped_name = f"000001-cs-t1=87654321-cs={shader}.buf"
    absent_name = f"000002-cs-t0=12345678-cs={shader}.buf"
    (workspace / local_name).write_bytes(b"local")
    (external / "older.buf").write_bytes(b"older")
    (deduped / "transfer.buf").write_bytes(b"copied")

    (workspace / "log.txt").write_text(
        f"000001 3DMigoto Dumping Buffer {external / local_name} -> {external / 'older.buf'}\n"
        f"000001 3DMigoto Dumping Buffer {external / deduped_name} -> {external / 'transfer.buf'}\n"
        f"000002 3DMigoto Dumping Buffer {external / absent_name} -> {external / 'absent.buf'}\n",
        encoding="utf-8",
    )
    capture = CAPTURE.APMICapture(workspace)
    assert capture.skin["000001", shader]["cs-t0"][1].read_bytes() == b"local"
    assert capture.skin["000001", shader]["cs-t1"][1].read_bytes() == b"copied"
    assert ("000002", shader) not in capture.skin


def test_capture_folder_uses_local_deduplicated_buffers(tmp_path, monkeypatch):
    package = "_apmi_local_capture_test"
    package_module = types.ModuleType(package)
    package_module.__path__ = []
    monkeypatch.setitem(sys.modules, package, package_module)
    monkeypatch.setitem(sys.modules, f"{package}.efmi_skeleton", types.SimpleNamespace(
        EFMISkeletonMergeHelper=types.SimpleNamespace(resolve_frame_analysis_dir=lambda _: None),
    ))
    spec = importlib.util.spec_from_file_location(
        f"{package}.apmi_skeleton", MODULE_PATH.with_name("apmi_skeleton.py"),
    )
    skeleton = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, skeleton)
    spec.loader.exec_module(skeleton)

    local = tmp_path / "APMICapture"
    deduped = local / "deduped"
    deduped.mkdir(parents=True)
    (local / "log.txt").touch()
    (deduped / "buffer.buf").write_bytes(b"buffer")
    assert skeleton.capture_folder(tmp_path) == local


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

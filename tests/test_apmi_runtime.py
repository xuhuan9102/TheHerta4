from common.apmi_runtime import ensure_runtime_core_files


def test_apmi_runtime_installs_missing_core_files(tmp_path):
    loader = tmp_path / "APMI"
    loader.mkdir()
    runtime_ini = loader / "d3dx.ini"
    runtime_ini.write_bytes(b"")
    resources = tmp_path / "resources"
    resources.mkdir()
    payloads = {
        "APMI_Skinning.ini": b"skin",
        "APMI_MergedSkeleton.ini": b"skeleton",
    }
    for name, payload in payloads.items():
        (resources / name).write_bytes(payload)

    installed = ensure_runtime_core_files(
        runtime_ini,
        payloads,
        resource_dir=resources,
    )

    assert installed == list(payloads)
    for name, payload in payloads.items():
        assert (loader / "Core" / name).read_bytes() == payload


def test_apmi_runtime_does_not_overwrite_existing_core_file(tmp_path):
    loader = tmp_path / "APMI"
    core = loader / "Core"
    core.mkdir(parents=True)
    runtime_ini = loader / "d3dx.ini"
    runtime_ini.write_bytes(b"")
    destination = core / "APMI_Skinning.ini"
    destination.write_bytes(b"loader-owned")
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / destination.name).write_bytes(b"plugin-bundled")

    installed = ensure_runtime_core_files(
        runtime_ini,
        [destination.name],
        resource_dir=resources,
    )

    assert installed == []
    assert destination.read_bytes() == b"loader-owned"

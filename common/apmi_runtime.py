from pathlib import Path

from .safe_write import write_bytes_if_changed_atomic


def ensure_runtime_core_files(runtime_ini, file_names, *, resource_dir=None):
    """Install bundled APMI core files that are missing from the loader."""
    core_dir = Path(runtime_ini).resolve().parent / "Core"
    resources = (
        Path(resource_dir)
        if resource_dir is not None
        else Path(__file__).resolve().parents[1] / "resources"
    )
    installed = []
    for name in file_names:
        destination = core_dir / name
        if destination.is_file():
            continue
        source = resources / name
        if not source.is_file():
            raise FileNotFoundError(source)
        write_bytes_if_changed_atomic(str(destination), source.read_bytes())
        installed.append(name)
    return installed

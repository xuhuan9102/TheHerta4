"""Run ZZMI fusion in Blender's bundled Python without loading bpy."""

import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types

_PROGRESS = "HERTA_IMPORT_PROGRESS "
_RESULT = "HERTA_IMPORT_RESULT "


def bundled_python():
    name = "python.exe" if sys.platform == "win32" else f"python{sys.version_info.major}.{sys.version_info.minor}"
    return Path(sys.prefix) / "bin" / name


class ZZMIImportWorker:
    def __init__(self, workspace_root, identities, settings):
        self._manifest = {
            "workspace_root": workspace_root,
            "unique_str_list": identities,
            "settings": settings,
        }

    def __enter__(self):
        self._directory = tempfile.TemporaryDirectory(prefix="herta-zzmi-import-")
        directory = Path(self._directory.name)
        request = directory / "request.json"
        self._log = directory / "worker.log"
        try:
            request.write_text(json.dumps(self._manifest), encoding="utf-8")
            with self._log.open("wb") as log:
                self._process = subprocess.Popen(
                    [str(bundled_python()), "-B", "-u", "-X", "utf8", str(Path(__file__).resolve()), str(request)],
                    stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except OSError:
            self._directory.cleanup()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        # A worker may still be publishing workspace files when the operator is closed.
        self._process.wait()
        self._directory.cleanup()

    def poll(self):
        """Return the latest progress and the result once the process exits."""
        status = self._process.poll()
        lines = self._log.read_text(encoding="utf-8", errors="replace").splitlines()
        progress = next(
            (json.loads(line[len(_PROGRESS):]) for line in reversed(lines)
             if line.startswith(_PROGRESS) and line.endswith("]")),
            (0, "启动骨骼融合进程"),
        )
        if status is None:
            return progress, None
        result = None
        for line in lines:
            if line.startswith(_RESULT):
                result = json.loads(line[len(_RESULT):])
            elif not line.startswith(_PROGRESS):
                print(line)
        if status or result is None:
            raise RuntimeError(f"ZZMI 骨骼融合进程异常退出（退出码 {status}）")
        return progress, result


def _run_worker(request):
    # Resolve only the data modules. Importing the add-on root would register Blender UI.
    package = types.ModuleType("_herta_zzmi_worker")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[package.__name__] = package
    module = importlib.import_module(package.__name__ + ".common.zzmi_skeleton")

    def progress(fraction, message):
        print(_PROGRESS + json.dumps([fraction, message]), flush=True)

    arguments = json.loads(Path(request).read_text(encoding="utf-8"))
    settings = arguments["settings"]
    result = module.ZZMISkeletonMergeHelper.ensure_import_skeleton_data(
        arguments["workspace_root"], arguments["unique_str_list"],
        per_lod=settings["per_lod"], settings=settings, progress=progress,
    )
    print(_RESULT + json.dumps(result), flush=True)


if __name__ == "__main__":
    _run_worker(sys.argv[1])

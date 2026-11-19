"""Sample Blender poses in separate processes using the same mesh and UV layer."""

from contextlib import ExitStack, closing, contextmanager
import ctypes
from ctypes import wintypes
from multiprocessing.shared_memory import SharedMemory
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from time import perf_counter

import bpy
import numpy as np


POSE_BLOCK_SIZE = 16


def _pose_ranges(frame_count):
    return [(start, min(start + POSE_BLOCK_SIZE, frame_count))
            for start in range(0, frame_count, POSE_BLOCK_SIZE)]


@contextmanager
def _pose_events(prefix, blocks):
    import _winapi

    create_event = ctypes.WinDLL("kernel32", use_last_error=True).CreateEventW
    create_event.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    create_event.restype = wintypes.HANDLE
    with ExitStack() as stack:
        events = {}
        for block in blocks:
            pair = []
            for state in ("ready", "done"):
                handle = create_event(None, False, False, f"{prefix}-pose-{block}-{state}")
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                stack.callback(_winapi.CloseHandle, handle)
                pair.append(handle)
            events[block] = pair
        yield events


def _worker_error(process, log_path):
    log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    return RuntimeError(f"形态键采样进程未完成（{process.returncode}）\n{log_tail}")


def pose_worker_count(mesh, frame_count, row_count):
    cpu_count = os.cpu_count() or 1
    if sys.platform != "win32" or cpu_count < 4 or frame_count < 120:
        return 0
    if not bpy.app.binary_path or len(mesh.polygons) < 10_000 or not row_count:
        return 0
    # Bound the two shared mappings. Large jobs can still use the serial sampler.
    transfer_size = frame_count * (12 * len(mesh.vertices) + 40 * row_count)
    if transfer_size > 3 * 1024**3 or "TEXCOORD.xy" not in mesh.uv_layers:
        return 0

    counts = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", counts)
    if np.any((counts != 3) & (counts != 4)):
        return 0
    return min(4, cpu_count - 2)


@contextmanager
def sample_parallel(mesh, indices, basis, sampled_coords, flags, worker_count):
    import _winapi

    frame_count, vertex_count, row_count = len(flags), len(basis), len(indices)
    ranges = _pose_ranges(frame_count)
    with tempfile.TemporaryDirectory(prefix="herta_pose_") as tmp:
        root = Path(tmp)
        bpy.data.libraries.write(str(root / "sample.blend"), {mesh}, fake_user=True)
        np.save(root / "rows.npy", indices)
        with (
            closing(SharedMemory(create=True, size=12 * frame_count * vertex_count)) as input_data,
            closing(SharedMemory(create=True, size=40 * frame_count * row_count)) as output_data,
            _pose_events(input_data.name, range(len(ranges))) as events,
        ):
            coordinates = np.ndarray((frame_count, vertex_count, 3), dtype=np.float32, buffer=input_data.buf)
            output = np.ndarray((frame_count, row_count, 10), dtype=np.float32, buffer=output_data.buf)

            loop_vertices = np.empty(len(mesh.loops), dtype=np.int32)
            mesh.loops.foreach_get("vertex_index", loop_vertices)
            rows_to_vertices = loop_vertices[indices]
            tasks = []
            try:
                for worker in range(worker_count):
                    log_path = root / f"worker-{worker}.log"
                    command = [
                        bpy.app.binary_path, "--background", "--disable-autoexec", "--threads", "2",
                        "--python-exit-code", "1", "--python", __file__, "--", str(os.getpid()), str(root),
                        str(worker), str(worker_count), input_data.name, output_data.name,
                        str(frame_count), str(vertex_count), str(row_count),
                    ]
                    with log_path.open("wb") as log:
                        process = subprocess.Popen(
                            command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    tasks.append((process, log_path))

                def completed_blocks():
                    prepared = 0
                    prep_time = wait_time = 0.0
                    for block, (start, stop) in enumerate(ranges):
                        timer = perf_counter()
                        while prepared < min(len(ranges), block + 2 * worker_count):
                            begin, end = ranges[prepared]
                            for index in range(begin, end):
                                _, key = next(sampled_coords)
                                output[index, :, :3] = key[rows_to_vertices]
                                first, sequential = flags[index]
                                if sequential:
                                    if first:
                                        posed = basis.copy()
                                    # Keep the serial float32 prefix across every block.
                                    posed += key - basis
                                    coordinates[index] = posed
                                else:
                                    coordinates[index] = key
                            _winapi.SetEvent(events[prepared][0])
                            prepared += 1
                        prep_time += perf_counter() - timer
                        timer = perf_counter()
                        process, log_path = tasks[block % worker_count]
                        while _winapi.WaitForSingleObject(events[block][1], 100) == _winapi.WAIT_TIMEOUT:
                            if process.poll() is not None:
                                raise _worker_error(process, log_path)
                        wait_time += perf_counter() - timer
                        yield output[start:stop]
                    print(
                        f"形态键采样流水线：{len(ranges)} 块，坐标准备 {prep_time:.2f}s，等待采样 {wait_time:.2f}s",
                        flush=True,
                    )

                completed = completed_blocks()
                yield output, completed
                # Resume the last yield when the consumer stopped after its final frame.
                for _ in completed:
                    raise RuntimeError("形态键采样结果未全部收集")
                for process, log_path in tasks:
                    if process.wait() != 0:
                        raise _worker_error(process, log_path)
            finally:
                for process, _ in tasks:
                    if process.poll() is None:
                        process.kill()
                        process.wait()


def _watch_parent(parent_pid):
    # The background exporter can be terminated by Esc while this worker is running.
    import _winapi

    handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, parent_pid)
    try:
        _winapi.WaitForSingleObject(handle, _winapi.INFINITE)
        os._exit(1)
    finally:
        _winapi.CloseHandle(handle)


def _sample_range(root, worker, worker_count, input_name, output_name, frame_count, vertex_count, row_count):
    import _winapi

    root = Path(root)
    with bpy.data.libraries.load(str(root / "sample.blend")) as (source, target):
        target.meshes = source.meshes
    mesh, = target.meshes
    indices = np.load(root / "rows.npy")
    ranges = _pose_ranges(frame_count)
    blocks = range(worker, len(ranges), worker_count)
    with (
        closing(SharedMemory(input_name)) as input_data,
        closing(SharedMemory(output_name)) as output_data,
        _pose_events(input_name, blocks) as events,
    ):
        coordinates = np.ndarray((frame_count, vertex_count, 3), dtype=np.float32, buffer=input_data.buf)
        samples = np.ndarray((frame_count, row_count, 10), dtype=np.float32, buffer=output_data.buf)
        normals = np.empty((len(mesh.loops), 3), dtype=np.float32)
        tangents = np.empty_like(normals)
        signs = np.empty(len(mesh.loops), dtype=np.float32)
        for block in blocks:
            ready, done = events[block]
            _winapi.WaitForSingleObject(ready, _winapi.INFINITE)
            start, stop = ranges[block]
            for index in range(start, stop):
                mesh.vertices.foreach_set("co", coordinates[index].ravel())
                mesh.update()
                mesh.calc_tangents(uvmap="TEXCOORD.xy")
                mesh.corner_normals.foreach_get("vector", normals.ravel())
                mesh.loops.foreach_get("tangent", tangents.ravel())
                mesh.loops.foreach_get("bitangent_sign", signs)
                samples[index, :, 3:6] = normals[indices]
                samples[index, :, 6:9] = tangents[indices]
                samples[index, :, 9] = -signs[indices]
            _winapi.SetEvent(done)


if __name__ == "__main__":
    parent, root, worker, worker_count, coords, output, frames, verts, rows = sys.argv[sys.argv.index("--") + 1:]
    threading.Thread(target=_watch_parent, args=(int(parent),), daemon=True).start()
    _sample_range(root, int(worker), int(worker_count), coords, output, int(frames), int(verts), int(rows))
    sys.stdout.flush()
    os._exit(0)

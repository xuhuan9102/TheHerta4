"""Run the exported bone physics shader in Blender on Windows' D3D11 WARP."""

import ctypes as ct
import sys
from concurrent.futures import Future
from functools import lru_cache
from pathlib import Path
from threading import Thread

import numpy as np

from .zzmi_bone_physics import AIR_PARAMS_TAG, JOINT_PARAMS_TAG, SHADER, solver_shader_source


P = ct.c_void_p
U = ct.c_uint32
HR = ct.c_long
_preview_source = None
_preview_shader = None


class InitialData(ct.Structure):
    _fields_ = [("pointer", P), ("pitch", U), ("slice_pitch", U)]


class Mapping(ct.Structure):
    _fields_ = [("pointer", P), ("row_pitch", U), ("depth_pitch", U)]


def _call(obj, index, returns, types, *args):
    table = ct.cast(obj, ct.POINTER(ct.POINTER(P))).contents
    return ct.WINFUNCTYPE(returns, P, *types)(table[index])(obj, *args)


def _check(result):
    if result < 0:
        raise RuntimeError(f"DX11 物理预览失败（0x{result & 0xffffffff:08X}）")


def _release(obj):
    if obj:
        _call(obj, 2, U, ())


def _blob(blob):
    address = _call(blob, 3, P, ())
    size = _call(blob, 4, ct.c_size_t, ())
    return ct.string_at(address, size)


@lru_cache(maxsize=1)
def _compile(source):
    compiler = ct.WinDLL("d3dcompiler_47").D3DCompile
    compiler.argtypes = [P, ct.c_size_t, ct.c_char_p, P, P, ct.c_char_p, ct.c_char_p, U, U,
                         ct.POINTER(P), ct.POINTER(P)]
    compiler.restype = HR
    output, errors = P(), P()
    try:
        result = compiler(source, len(source), SHADER.encode(), None, None, b"main", b"cs_5_0", 0, 0,
                          ct.byref(output), ct.byref(errors))
        if result < 0 and errors:
            raise RuntimeError(_blob(errors).decode(errors="replace").rstrip("\0"))
        _check(result)
        return _blob(output)
    finally:
        _release(output)
        _release(errors)


def prepare_preview_shader(configuration):
    """Compile once in the background without accessing any Blender objects."""
    global _preview_source, _preview_shader
    if sys.platform != "win32":
        raise RuntimeError("实时骨骼物理预览需要 Windows DX11")
    source = solver_shader_source((Path(__file__).resolve().parents[1] / "Toolset" / SHADER).read_bytes(),
                                  configuration.assets())
    if _preview_source == source and _preview_shader is not None:
        if not _preview_shader.done() or _preview_shader.exception() is None:
            return _preview_shader

    work = Future()

    def compile_shader():
        try:
            work.set_result(_compile(source))
        except Exception as error:
            work.set_exception(error)

    _preview_source, _preview_shader = source, work
    Thread(target=compile_shader, name="SSMT physics shader", daemon=True).start()
    return work


class PreviewSolver:
    def __init__(self, configuration, *, bytecode=None):
        if sys.platform != "win32":
            raise RuntimeError("实时骨骼物理预览需要 Windows DX11")
        self._owned = []
        self.device, self.context = P(), P()
        dll = ct.WinDLL("d3d11").D3D11CreateDevice
        dll.argtypes = [P, U, P, U, P, U, U, ct.POINTER(P), ct.POINTER(U), ct.POINTER(P)]
        dll.restype = HR
        try:
            _check(dll(None, 5, None, 0, None, 0, 7, ct.byref(self.device), None, ct.byref(self.context)))
            assets = tuple(configuration.assets())
            if bytecode is None:
                bytecode = _compile(solver_shader_source(
                    (Path(__file__).resolve().parents[1] / "Toolset" / SHADER).read_bytes(), assets,
                ))
            self.shader = self._create(18, (P, ct.c_size_t, P), bytecode, len(bytecode), None)
            bodies, followers, state, air_drag, *joint_options = (payload for _, payload in assets)
            self.capacity = len(configuration.followers)
            self.size = self.capacity * 48
            self.palette = self._buffer(bytes(self.size), flags=8 | 128)
            self.staging = self._buffer(None, size=self.size, usage=3, access=0x20000)
            self.state = self._buffer(state, flags=8 | 128)
            # Z-up Blender world to the Y-up frame the in-game solver uses.
            space = np.array(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)), "<f4")
            self.inputs = (P * 4)(
                self._view(self._buffer(space.tobytes(), flags=8), 4, 2),
                self._view(self._buffer(bodies, flags=8), len(bodies) // 16, 2),
                self._view(self._buffer(followers, flags=8), self.capacity, 43),
                self._view(self._buffer(air_drag, flags=8), len(air_drag) // 4, 41),
            )
            self.uavs = (P * 2)(self._view(self.state, len(state) // 16, 2, True),
                                self._view(self.palette, self.size // 4, 41, True))
            self.controls = np.zeros((64, 4), "<f4")
            self.controls[47, 1] = 1
            self.controls[47, 3] = AIR_PARAMS_TAG
            self.joint_input = None
            if joint_options:
                data = joint_options[0]
                self.joint_input = (P * 1)(self._view(self._buffer(data, flags=8), len(data) // 16, 2))
                self.controls[47, 3] = JOINT_PARAMS_TAG
            self.controls[48] = configuration.solver_params
            descriptor = (U * 8)(64, 1, 1, 2, 0, 8, 0, 0)
            data = InitialData(self.controls.ctypes.data, self.controls.nbytes, self.controls.nbytes)
            self.texture = self._create(4, (P, P), descriptor, ct.byref(data))
            self.control_input = (P * 1)(self._create(7, (P, P), self.texture, None))
        except BaseException:
            self.close()
            raise

    def _create(self, index, types, *args):
        obj = P()
        _check(_call(self.device, index, HR, (*types, ct.POINTER(P)), *args, ct.byref(obj)))
        self._owned.append(obj)
        return obj

    def _buffer(self, payload, *, size=None, flags=0, usage=0, access=0):
        desc = (U * 6)(len(payload) if size is None else size, usage, flags, access, 0, 0)
        blob = ct.create_string_buffer(payload) if payload is not None else None
        data = InitialData(ct.addressof(blob), 0, 0) if blob is not None else None
        return self._create(3, (P, P), desc, ct.byref(data) if data else None)

    def _view(self, buffer, count, fmt, write=False):
        desc = (U * (5 if write else 6))(fmt, 1, 0, count, 0)
        return self._create(8 if write else 7, (P, P), buffer, desc)

    def step(self, matrices, time, frame, reset, gravity, force):
        matrices = np.ascontiguousarray(matrices, dtype="<f4")
        if matrices.shape != (self.capacity, 3, 4):
            raise ValueError("预览矩阵与胶囊配置的骨骼范围不一致")
        self.controls[46] = (time, frame, reset, gravity)
        self.controls[47, 0] = force
        for target, data in ((self.palette, matrices), (self.texture, self.controls)):
            _call(self.context, 48, None, (P, U, P, P, U, U), target, 0, None, data.ctypes.data, 0, 0)
        _call(self.context, 67, None, (U, U, P), 96, len(self.inputs), self.inputs)
        if self.joint_input is not None:
            _call(self.context, 67, None, (U, U, P), 108, 1, self.joint_input)
        _call(self.context, 67, None, (U, U, P), 120, 1, self.control_input)
        _call(self.context, 68, None, (U, U, P, P), 6, 2, self.uavs, None)
        _call(self.context, 69, None, (P, P, U), self.shader, None, 0)
        _call(self.context, 41, None, (U, U, U), 1, 1, 1)
        _call(self.context, 68, None, (U, U, P, P), 6, 2, (P * 2)(), None)
        _call(self.context, 47, None, (P, P), self.staging, self.palette)
        mapped = Mapping()
        _check(_call(self.context, 14, HR, (P, U, U, U, P), self.staging, 0, 1, 0, ct.byref(mapped)))
        try:
            return np.frombuffer(ct.string_at(mapped.pointer, self.size), "<f4").reshape(-1, 3, 4)
        finally:
            _call(self.context, 15, None, (P, U), self.staging, 0)

    def close(self):
        for obj in reversed(self._owned):
            _release(obj)
        self._owned.clear()
        _release(self.context)
        _release(self.device)
        self.context, self.device = P(), P()


def prepare_preview_solver(configuration):
    """Prepare and warm the native driver before handing it to Blender's UI thread."""
    shader = prepare_preview_shader(configuration)
    work = Future()

    def warm_driver():
        if not work.set_running_or_notify_cancel():
            return
        driver = None
        try:
            driver = PreviewSolver(configuration, bytecode=shader.result())
            bind = np.repeat(np.eye(4, dtype="<f4")[None, :3], driver.capacity, axis=0)
            driver.step(bind, 0., 0, -1, 0., 0.)
            driver.step(bind, 1 / 30., 1, -1, 0., 0.)
            work.set_result(driver)
        except Exception as error:
            if driver is not None:
                driver.close()
            work.set_exception(error)

    Thread(target=warm_driver, name="SSMT physics preview warmup", daemon=True).start()
    return work


def discard_preview_solver(work):
    """Close a canceled preview's driver when its worker is finished."""
    def discard(done):
        if not done.cancelled() and done.exception() is None:
            done.result().close()

    if not work.cancel():
        work.add_done_callback(discard)

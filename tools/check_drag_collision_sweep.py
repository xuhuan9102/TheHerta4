"""Execute the shipped collision shaders on D3D11 (WARP by default).

    python tools/check_drag_collision_sweep.py
    python tools/check_drag_collision_sweep.py --mod <mod-directory>

The brute-force triangle oracle is independent of the GPU BVH. Tests include
whole-ring drags, moving/rotated colliders, open surfaces, and dense geometry.
"""

from __future__ import annotations

import argparse
import ctypes as ct
import importlib.util
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


baker = load_file("sweep_baker", ROOT / "blueprint/drag_collision.py")


class BufferDesc(ct.Structure):
    _fields_ = [(k, ct.c_uint) for k in ("size", "usage", "bind", "cpu", "misc", "stride")]


class Data(ct.Structure):
    _fields_ = [("data", ct.c_void_p), ("pitch", ct.c_uint), ("slice", ct.c_uint)]


class Mapped(ct.Structure):
    _fields_ = [("data", ct.c_void_p), ("row", ct.c_uint), ("depth", ct.c_uint)]


class D3D:
    """Small, synchronous D3D11 compute harness; no game process is modified."""

    def __init__(self, hardware=False):
        self.objects = []
        self.measure = hardware
        self.last_gpu_ms = None
        device, context = ct.c_void_p(), ct.c_void_p()
        library = ct.WinDLL("d3d11.dll")
        create = library.D3D11CreateDevice
        create.argtypes = [ct.c_void_p, ct.c_uint, ct.c_void_p, ct.c_uint, ct.c_void_p,
                           ct.c_uint, ct.c_uint, ct.POINTER(ct.c_void_p), ct.c_void_p, ct.POINTER(ct.c_void_p)]
        create.restype = ct.c_int32
        self.check(create(None, 1 if hardware else 5, None, 0, None, 0, 7,
                          ct.byref(device), None, ct.byref(context)))
        self.device, self.context = device.value, context.value
        self.objects.extend([self.device, self.context])
        self.compiler = load_file("sweep_compiler", ROOT / "tools/compile-hlsl.py")
        self.compile = self.compiler.load_compiler()
        self.compile.restype = ct.c_int32
        self.bounds_shader = self.shader(ROOT / "Toolset/drag_interaction/rzm_drag_collision_bounds.hlsl")
        self.sweep_shader = self.shader(ROOT / "Toolset/drag_interaction/rzm_drag_collision.hlsl")
        self.surface_shader = self.shader(ROOT / "Toolset/drag_interaction/rzm_drag_collision_surface.hlsl")

    @staticmethod
    def check(hr):
        if hr < 0:
            raise RuntimeError(f"D3D11 HRESULT 0x{hr & 0xffffffff:08x}")

    @staticmethod
    def call(obj, index, args, result=ct.c_int32):
        table = ct.cast(obj, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
        return ct.WINFUNCTYPE(result, ct.c_void_p, *args)(table[index])

    def method(self, obj, index, types, *args, result=ct.c_int32):
        return self.call(obj, index, types, result)(obj, *args)

    def keep(self, obj):
        self.objects.append(obj.value)
        return obj.value

    def shader(self, path):
        source = path.read_bytes()
        code, error = ct.c_void_p(), ct.c_void_p()
        hr = self.compile(source, ct.c_size_t(len(source)), str(path).encode("utf-8"), None, None,
                          b"main", b"cs_5_0", 0x8000, 0, ct.byref(code), ct.byref(error))
        message = self.compiler.read_blob(error.value).decode("utf-8", "replace") if error.value else ""
        if error.value:
            self.objects.append(error.value)
        if code.value:
            self.objects.append(code.value)
        if hr < 0:
            raise RuntimeError(message)
        blob = self.compiler.read_blob(code.value)
        shader = ct.c_void_p()
        self.check(self.method(self.device, 18, [ct.c_void_p, ct.c_size_t, ct.c_void_p, ct.c_void_p],
                               ct.c_char_p(blob), len(blob), None, ct.byref(shader)))
        return self.keep(shader)

    def buffer(self, array, *, structured=0, writable=False, staging=False):
        array = np.ascontiguousarray(array)
        desc = BufferDesc(array.nbytes, 3 if staging else 0,
                          0 if staging else 8 | (128 if writable else 0),
                          0x20000 if staging else 0, 0x40 if structured and not staging else 0,
                          structured if not staging else 0)
        initial = Data(array.ctypes.data, 0, 0)
        resource = ct.c_void_p()
        self.check(self.method(self.device, 3, [ct.c_void_p, ct.c_void_p, ct.c_void_p],
                               ct.byref(desc), None if staging else ct.byref(initial), ct.byref(resource)))
        return self.keep(resource)

    def view(self, resource, elements, fmt, writable=False):
        desc = (ct.c_uint * 6)(fmt, 1, 0, elements, 0, 0)
        view = ct.c_void_p()
        self.check(self.method(self.device, 8 if writable else 7,
                               [ct.c_void_p, ct.c_void_p, ct.c_void_p], resource, ct.byref(desc), ct.byref(view)))
        return self.keep(view)

    def ini(self, params):
        desc = (ct.c_uint * 8)(len(params), 1, 1, 2, 0, 8, 0, 0)
        data = Data(params.ctypes.data, 0, 0)
        texture = ct.c_void_p()
        self.check(self.method(self.device, 4, [ct.c_void_p, ct.c_void_p, ct.c_void_p],
                               ct.byref(desc), ct.byref(data), ct.byref(texture)))
        resource = self.keep(texture)
        view = ct.c_void_p()
        self.check(self.method(self.device, 7, [ct.c_void_p, ct.c_void_p, ct.c_void_p],
                               resource, None, ct.byref(view)))
        return resource, self.keep(view)

    def bind(self, slot, view, writable=False):
        views = (ct.c_void_p * 1)(view)
        if writable:
            self.method(self.context, 68, [ct.c_uint, ct.c_uint, ct.c_void_p, ct.c_void_p],
                        slot, 1, views, None, result=None)
        else:
            self.method(self.context, 67, [ct.c_uint, ct.c_uint, ct.c_void_p],
                        slot, 1, views, result=None)

    def dispatch(self, shader, groups):
        self.method(self.context, 69, [ct.c_void_p, ct.c_void_p, ct.c_uint], shader, None, 0, result=None)
        self.method(self.context, 41, [ct.c_uint, ct.c_uint, ct.c_uint], groups, 1, 1, result=None)

    def query(self, kind):
        desc = (ct.c_uint * 2)(kind, 0)
        result = ct.c_void_p()
        self.check(self.method(self.device, 24, [ct.c_void_p, ct.c_void_p], ct.byref(desc), ct.byref(result)))
        return self.keep(result)

    def solve(self, positions, triangles, targets, participation, gap=0.00039, mode=0,
              cloth_triangles=None, normals=None):
        marker = len(self.objects)
        vertices = np.zeros((len(positions), 10), dtype=np.float32)
        vertices[:, :3] = positions
        if normals is not None:
            vertices[:, 3:6] = normals
        output = vertices.copy()
        output[:, :3] = targets
        triangles = np.asarray(triangles, dtype=np.uint32)
        order, leaves = baker.bake_sweep_order(np.asarray(positions)[triangles])
        source = self.buffer(vertices, structured=40)
        dest = self.buffer(output, structured=40, writable=True)
        contact = np.zeros((len(positions), 2, 4), dtype=np.float32)
        contact[:, 0, 3] = gap
        contact[:, 1, 3] = participation
        l0 = self.buffer(contact)
        index = self.buffer(triangles)
        ordered = self.buffer(order)
        bounds = self.buffer(np.zeros((leaves * 4, 4), dtype=np.float32), writable=True)
        for slot, resource, count, fmt in ((24, source, len(vertices), 0), (79, l0, len(vertices) * 2, 2),
                                          (87, index, triangles.size, 42), (89, ordered, len(order), 42)):
            self.bind(slot, self.view(resource, count, fmt))
        params = np.zeros((128, 4), dtype=np.float32)
        params[101] = (1, .002, mode, .9)
        params[115] = (1, leaves, 0, 0)
        texture, texture_view = self.ini(params)
        self.bind(120, texture_view)
        self.bind(4, self.view(bounds, leaves * 4, 2, writable=True), True)
        if self.measure:
            disjoint, stamp_begin, stamp_end = self.query(3), self.query(2), self.query(2)
            self.method(self.context, 27, [ct.c_void_p], disjoint, result=None)
            self.method(self.context, 28, [ct.c_void_p], stamp_begin, result=None)
        self.dispatch(self.bounds_shader, leaves // 32)
        params[115, 2] = 1
        self.method(self.context, 48, [ct.c_void_p, ct.c_uint, ct.c_void_p, ct.c_void_p, ct.c_uint, ct.c_uint],
                    texture, 0, None, params.ctypes.data, 0, 0, result=None)
        self.dispatch(self.bounds_shader, max(1, (leaves // 32 + 30) // 32))
        self.bind(4, None, True)
        self.bind(91, self.view(bounds, leaves * 4, 2))
        self.bind(5, self.view(dest, len(vertices), 0, writable=True), True)
        if cloth_triangles is None:
            self.dispatch(self.sweep_shader, (len(vertices) + 255) // 256)
        else:
            cloth, csr = baker.bake_cloth_adjacency(cloth_triangles, participation)
            cloth_buffer = self.buffer(cloth)
            adjacent = self.buffer(csr)
            limits = self.buffer(np.ones(len(cloth), dtype=np.float32), writable=True)
            self.bind(92, self.view(cloth_buffer, cloth.size, 42))
            self.bind(93, self.view(adjacent, csr.size, 42))
            self.bind(4, self.view(limits, len(cloth), 41, writable=True), True)
            params[115, 2] = 0
            self.method(self.context, 48, [ct.c_void_p, ct.c_uint, ct.c_void_p, ct.c_void_p, ct.c_uint, ct.c_uint],
                        texture, 0, None, params.ctypes.data, 0, 0, result=None)
            self.dispatch(self.surface_shader, (len(cloth) + 255) // 256)
            params[115, 2] = 2
            self.method(self.context, 48, [ct.c_void_p, ct.c_uint, ct.c_void_p, ct.c_void_p, ct.c_uint, ct.c_uint],
                        texture, 0, None, params.ctypes.data, 0, 0, result=None)
            self.dispatch(self.surface_shader, (len(vertices) + 255) // 256)
            self.bind(4, None, True)
        if self.measure:
            self.method(self.context, 28, [ct.c_void_p], stamp_end, result=None)
            self.method(self.context, 28, [ct.c_void_p], disjoint, result=None)
        self.bind(5, None, True)
        staging = self.buffer(output, staging=True)
        self.method(self.context, 47, [ct.c_void_p, ct.c_void_p], staging, dest, result=None)
        mapped = Mapped()
        self.check(self.method(self.context, 14, [ct.c_void_p, ct.c_uint, ct.c_uint, ct.c_uint, ct.c_void_p],
                               staging, 0, 1, 0, ct.byref(mapped)))
        result = np.frombuffer(ct.string_at(mapped.data, output.nbytes), dtype=np.float32).reshape(-1, 10).copy()
        self.method(self.context, 15, [ct.c_void_p, ct.c_uint], staging, 0, result=None)
        self.last_gpu_ms = None
        if self.measure:
            frequency = (ct.c_uint64 * 2)()
            begin_tick, end_tick = ct.c_uint64(), ct.c_uint64()
            results = [self.method(self.context, 29, [ct.c_void_p, ct.c_void_p, ct.c_uint, ct.c_uint],
                                   query, ct.byref(data), ct.sizeof(data), 0)
                       for query, data in ((disjoint, frequency), (stamp_begin, begin_tick), (stamp_end, end_tick))]
            if results == [0, 0, 0] and frequency[1] == 0 and frequency[0]:
                self.last_gpu_ms = (end_tick.value - begin_tick.value) / frequency[0] * 1000
        for slot in (24, 79, 87, 89, 91, 92, 93, 120):
            self.bind(slot, None)
        self.release(marker)
        np.testing.assert_array_equal(result[:, 3:], vertices[:, 3:])
        return result[:, :3]

    def release(self, marker=0):
        for obj in reversed(self.objects[marker:]):
            self.method(obj, 2, [], result=ct.c_uint)
        del self.objects[marker:]


def first_hit(origin, delta, triangles):
    """Independent brute-force Moller-Trumbore oracle (no BVH, no budgets)."""
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    ab, ac = b - a, c - a
    h = np.cross(np.broadcast_to(delta, ac.shape), ac)
    determinant = np.einsum("ij,ij->i", ab, h)
    usable = np.abs(determinant) > 1e-14
    inv = np.zeros_like(determinant)
    inv[usable] = 1 / determinant[usable]
    rel = origin - a
    u = np.einsum("ij,ij->i", rel, h) * inv
    q = np.cross(rel, ab)
    v = q @ delta * inv
    t = np.einsum("ij,ij->i", ac, q) * inv
    valid = usable & (u >= -1e-7) & (v >= -1e-7) & (u + v <= 1.0000001) & (t >= 0) & (t <= 1)
    return float(np.min(t[valid])) if valid.any() else float("inf")


def surface_intersections(cloth, body):
    """Triangle/triangle oracle with AABB pruning, independent of the GPU BVH."""
    cloth, body = np.asarray(cloth, float), np.asarray(body, float)
    lower, upper = body.min(axis=1), body.max(axis=1)
    hits = set()

    def any_segments(origin, end, a, b, c):
        delta = end - origin
        ab, ac = b - a, c - a
        h = np.cross(delta, ac)
        determinant = np.einsum("ij,ij->i", ab, h)
        usable = np.abs(determinant) > 1e-14
        inv = np.zeros_like(determinant)
        inv[usable] = 1 / determinant[usable]
        rel = origin - a
        u = np.einsum("ij,ij->i", rel, h) * inv
        q = np.cross(rel, ab)
        v = np.einsum("ij,ij->i", delta, q) * inv
        t = np.einsum("ij,ij->i", ac, q) * inv
        return np.any(usable & (u >= -1e-7) & (v >= -1e-7) & (u + v <= 1.0000001) & (t >= 0) & (t <= 1))

    for i, triangle in enumerate(cloth):
        candidates = np.flatnonzero(np.all(lower <= triangle.max(axis=0), axis=1)
                                    & np.all(upper >= triangle.min(axis=0), axis=1))
        if not len(candidates):
            continue
        other = body[candidates]
        for edge in range(3):
            count = len(other)
            p = np.broadcast_to(triangle[edge], (count, 3))
            q = np.broadcast_to(triangle[(edge + 1) % 3], (count, 3))
            if any_segments(p, q, other[:, 0], other[:, 1], other[:, 2]):
                hits.add(i)
                break
            a, b, c = (np.broadcast_to(v, (count, 3)) for v in triangle)
            if any_segments(other[:, edge], other[:, (edge + 1) % 3], a, b, c):
                hits.add(i)
                break
    return hits


def synthetic_surface(gpu):
    ring = load_file("surface_ring", ROOT / "tools/check_drag_collision_ring_tunnel.py")
    a, b, c, _ = ring.make_cylinder()
    collider = np.stack([a, b, c], axis=1)
    body = collider.reshape(-1, 3)
    angle = np.arange(96) * np.pi * 2 / 96
    circle = np.c_[np.cos(angle) * .07539, np.zeros(96), np.sin(angle) * .07539]
    starts = np.r_[circle + [0, .44, 0], circle + [0, .46, 0]]
    faces = []
    for i in range(96):
        j = (i + 1) % 96
        faces.extend(((i, j, i + 96), (j, j + 96, i + 96)))
    faces = np.asarray(faces, np.uint32) + len(body)
    source = np.r_[body, starts].astype(np.float32)
    normals = source.copy(); normals[:, 1] = 0
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    body_ids = np.arange(len(body), dtype=np.uint32).reshape(-1, 3)
    controlled = np.zeros(len(source), np.float32); controlled[len(body):] = 1
    idle = gpu.solve(source, body_ids, source, controlled, cloth_triangles=faces, normals=normals)
    np.testing.assert_array_equal(idle, source)
    target = source.copy(); target[len(body):] += [0.5, 0, 0]
    point_only = gpu.solve(source, body_ids, target, controlled, normals=normals)
    if not surface_intersections(point_only[faces], source[body_ids]):
        raise AssertionError("Vertex-only negative control no longer reproduces face penetration")
    nonuniform = source.copy()
    nonuniform[len(body):] += np.linspace(.01, .5, len(starts))[:, None] * np.array([[1, 0, 0]])
    solved = gpu.solve(source, body_ids, nonuniform, controlled, cloth_triangles=faces, normals=normals)
    if surface_intersections(solved[faces], source[body_ids]):
        raise AssertionError("Nonuniform per-vertex falloff produced surface penetration")
    cases = 0
    for rotated in (False, True):
        rotation = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]]) if rotated else np.eye(3)
        live_source = source @ rotation.T + [.3, -.2, .5]
        live_normals = normals @ rotation.T
        for direction in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (1, .3, 1)):
            offset = np.asarray(direction, float)
            offset = offset / np.linalg.norm(offset) * .5 @ rotation.T
            target = live_source.copy(); target[len(body):] += offset
            solved = gpu.solve(live_source, body_ids, target, controlled,
                               cloth_triangles=faces, normals=live_normals)
            residual = surface_intersections(solved[faces], live_source[body_ids])
            if residual:
                raise AssertionError(f"Full-surface drag {direction}, rotation={rotated}: {len(residual)} cloth faces intersect body")
            np.testing.assert_allclose(solved[:len(body)], live_source[:len(body)], atol=1e-7)
            if np.linalg.norm(solved[len(body):] - live_source[len(body):], axis=1).max() < .1:
                raise AssertionError("Collision froze the entire cloth instead of allowing the near side to move")
            cases += 1
    # A body corner hits the middle of a large cloth face while all three
    # cloth vertex paths miss it. Vertex-only collision cannot catch this.
    wall = np.array([[[0, -.02, -.02], [0, 0, .02], [0, .02, -.02]]])
    cloth = np.array([[-.1, -.2, -.2], [-.1, .2, -.2], [-.1, 0, .2]])
    src = np.r_[wall.reshape(-1, 3), cloth]
    dst = src.copy(); dst[3:, 0] += .5
    norms = np.broadcast_to([-1, 0, 0], src.shape)
    point_only = gpu.solve(src, [[0, 1, 2]], dst, [0, 0, 0, 1, 1, 1], normals=norms)
    np.testing.assert_allclose(point_only[3:], dst[3:], atol=1e-7)
    solved = gpu.solve(src, [[0, 1, 2]], dst, [0, 0, 0, 1, 1, 1],
                       cloth_triangles=[[3, 4, 5]], normals=norms)
    assert np.max(solved[3:, 0]) < 0
    print(f"GPU surface: {cases + 2} cases, zero cloth/body face intersections; outward dragging remains active")


def check_case(gpu, collider, starts, offset, *, mode=0, participation=None):
    collider = np.asarray(collider, dtype=np.float32)
    # Use a separate source row for every corner to exercise arbitrary indexing.
    body = collider.reshape(-1, 3)
    starts = np.asarray(starts, dtype=np.float32)
    source = np.concatenate([body, starts])
    triangles = np.arange(len(body), dtype=np.uint32).reshape(-1, 3)
    targets = source.copy()
    targets[len(body):] += offset
    controlled = np.zeros(len(source), dtype=np.float32)
    controlled[len(body):] = 1 if participation is None else participation
    solved = gpu.solve(source, triangles, targets, controlled, mode=mode)
    np.testing.assert_array_equal(solved[:len(body)], source[:len(body)])
    blocked = 0
    for i, (origin, target) in enumerate(zip(starts, solved[len(body):])):
        if controlled[len(body) + i] < .5:
            np.testing.assert_allclose(target, origin + offset, atol=1e-7)
            continue
        hit = first_hit(origin.astype(float), target.astype(float) - origin, collider.astype(float))
        if hit < .9999:
            raise AssertionError(f"vertex {i}: solved segment still intersects body at t={hit}")
        original_hit = first_hit(origin.astype(float), np.asarray(offset, dtype=float), collider.astype(float))
        if np.isfinite(original_hit):
            blocked += 1
        else:
            np.testing.assert_allclose(target, origin + offset, atol=2e-6)
    return blocked


def synthetic(gpu):
    ring = load_file("old_ring", ROOT / "tools/check_drag_collision_ring_tunnel.py")
    a, b, c, _ = ring.make_cylinder()
    collider = np.stack([a, b, c], axis=1)
    angle = np.arange(96) * np.pi * 2 / 96
    starts = np.c_[np.cos(angle) * .07539, np.full(96, .45), np.sin(angle) * .07539]
    cases, blocked = 0, 0
    for rotation in (np.eye(3), np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])):
        for translation in (np.zeros(3), np.array([.8, -.6, .2])):
            for direction in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (1, .3, 1)):
                offset = np.asarray(direction, float)
                offset = offset / np.linalg.norm(offset) * .5 @ rotation.T
                for mode in (0, 1):
                    blocked += check_case(gpu, collider @ rotation.T + translation,
                                          starts @ rotation.T + translation, offset, mode=mode)
                    cases += 1
    # Front/back endpoints are both outside, a sub-millimeter wall sits between.
    wall = np.array([[[0, -1, -1], [0, 1, -1], [0, 0, 1]]])
    check_case(gpu, wall, [[-.1, 0, 0], [.1, 0, 0]], np.array([.5, 0, 0]))
    # More triangles than either old query budget, mixed winding and padding.
    dense = np.repeat(wall, 4097, axis=0)
    dense[::2] = dense[::2, ::-1]
    check_case(gpu, dense, [[-.1, 0, 0]], np.array([.5, 0, 0]))
    check_case(gpu, wall, [[-.1, 0, 0]], np.array([.5, 0, 0]), participation=[0])
    print(f"GPU synthetic: {cases + 3} cases, {blocked} crossing paths constrained; no residual intersections")


def mod_check(gpu, path):
    mesh = path / "Meshes0000"
    stem = "2825da1e"
    contact = np.fromfile(mesh / f"{stem}ColliderVertexL0.buf", dtype="<f4").reshape(-1, 2, 4)
    vertices = np.fromfile(mesh / f"{stem}-Position.buf", dtype="<f4").reshape(len(contact), -1)[:, :3].copy()
    indices = np.fromfile(mesh / f"{stem}ColliderTriGridVerts.buf", dtype="<u4").reshape(-1, 3)
    selected = np.flatnonzero(contact[:, 1, 3] > .5)
    # Both legs, all sock rows receive one common offset, matching a full-radius grab.
    cases, crossings = 0, 0
    collider = vertices[indices].astype(float)
    for direction in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0)):
        for mode in (0, 1):
            target = vertices.copy()
            target[selected] += np.asarray(direction) * .35
            solved = gpu.solve(vertices, indices, target, contact[:, 1, 3], gap=contact[:, 0, 3], mode=mode)
            # Deterministic samples cover both limbs and the complete sock height.
            samples = selected[np.linspace(0, len(selected) - 1, min(400, len(selected)), dtype=int)]
            for row in samples:
                origin = vertices[row].astype(float)
                delta = solved[row].astype(float) - origin
                hit = first_hit(origin, delta, collider)
                if hit < .9999:
                    raise AssertionError(f"mod vertex {row}, direction {direction}: residual crossing at {hit}")
                crossings += np.isfinite(first_hit(origin, target[row] - origin, collider))
            cases += 1
    print(f"GPU mod: {len(selected)} controlled rows, {len(indices)} live collider triangles, "
          f"{cases} whole-zone drags; {crossings} original crossings in 3200 samples, zero after solving")


def mod_surface_check(gpu, path):
    mesh = path / "Meshes0000"
    stem = "2825da1e"
    contact = np.fromfile(mesh / f"{stem}ColliderVertexL0.buf", dtype="<f4").reshape(-1, 2, 4)
    source = np.fromfile(mesh / f"{stem}-Position.buf", dtype="<f4").reshape(len(contact), -1)
    positions, normals = source[:, :3].copy(), source[:, 3:6].copy()
    body = np.fromfile(mesh / f"{stem}ColliderTriGridVerts.buf", dtype="<u4").reshape(-1, 3)
    index_file = next(mesh.glob(f"LOD0.{stem}-*-Index.buf"))
    all_faces = np.fromfile(index_file, dtype="<u4").reshape(-1, 3)
    participation = contact[:, 1, 3] > .5
    cloth, _ = baker.bake_cloth_adjacency(all_faces, participation)
    baseline = surface_intersections(positions[cloth], positions[body])
    print(f"Surface baseline: {len(cloth)} cloth triangles, {len(baseline)} already intersect the collider", flush=True)
    cases = 0
    for direction in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0)):
        target = positions.copy()
        target[participation] += np.asarray(direction) * .35
        solved = gpu.solve(positions, body, target, participation, cloth_triangles=all_faces, normals=normals)
        hits = surface_intersections(solved[cloth], positions[body])
        fresh = hits - baseline
        if fresh:
            raise AssertionError(f"{direction}: {len(fresh)} newly intersecting cloth faces, {sorted(fresh)[:10]}")
        moving = np.linalg.norm(solved - positions, axis=1)[participation]
        print(f"Surface mod {direction}: {len(hits)} existing intersections, zero new; "
              f"{np.count_nonzero(moving > 1e-4)} vertices move, max {moving.max():.3f}, GPU ms={gpu.last_gpu_ms}", flush=True)
        cases += 1
    print(f"GPU surface mod: {cases} full-radius pulls checked over ALL {len(cloth)} cloth triangles")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mod", type=Path)
    parser.add_argument("--hardware", action="store_true")
    parser.add_argument("--surface-only", action="store_true")
    args = parser.parse_args()
    gpu = D3D(hardware=args.hardware)
    try:
        if not args.surface_only:
            synthetic(gpu)
        synthetic_surface(gpu)
        if args.mod:
            if not args.surface_only:
                mod_check(gpu, args.mod)
            mod_surface_check(gpu, args.mod)
    finally:
        gpu.release()


if __name__ == "__main__":
    main()

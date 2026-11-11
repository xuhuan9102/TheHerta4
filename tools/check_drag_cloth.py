"""Run generated cloth command lists and their actual HLSL on D3D11.

This deliberately interprets the emitted bindings/copies, instead of reconstructing
a second solver pipeline. It never attaches to a game or modifies a live mod.
"""

from __future__ import annotations

import ast
import ctypes as ct
import hashlib
import importlib.util
import operator
import re
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "cloth_d3d_base", ROOT / "tools/check_drag_collision_sweep.py"
)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


class GPU(base.D3D):
    def shader(self, path):
        source = path.read_bytes()
        cache = Path(tempfile.gettempdir()) / "ssmt-cloth-shader-tests"
        cache.mkdir(exist_ok=True)
        target = cache / (hashlib.sha256(source + b"cs_5_0:8000").hexdigest() + ".bin")
        if target.exists():
            blob = target.read_bytes()
        else:
            code, error = ct.c_void_p(), ct.c_void_p()
            hr = self.compile(
                source,
                len(source),
                str(path).encode("utf8"),
                None,
                None,
                b"main",
                b"cs_5_0",
                0x8000,
                0,
                ct.byref(code),
                ct.byref(error),
            )
            if code.value:
                self.objects.append(code.value)
            if error.value:
                self.objects.append(error.value)
            if hr < 0:
                raise RuntimeError(
                    self.compiler.read_blob(error.value).decode("utf8", "replace")
                )
            blob = self.compiler.read_blob(code.value)
            target.write_bytes(blob)
        shader = ct.c_void_p()
        self.check(
            self.method(
                self.device,
                18,
                [ct.c_void_p, ct.c_size_t, ct.c_void_p, ct.c_void_p],
                ct.c_char_p(blob),
                len(blob),
                None,
                ct.byref(shader),
            )
        )
        return self.keep(shader)

    def raw_buffer(self, blob, flags, stride=0, structured=False):
        blob = np.ascontiguousarray(blob, dtype=np.uint8)
        desc = base.BufferDesc(
            blob.nbytes, 0, flags, 0, 64 if structured else 0, stride
        )
        initial = base.Data(blob.ctypes.data, 0, 0)
        resource = ct.c_void_p()
        self.check(
            self.method(
                self.device,
                3,
                [ct.c_void_p, ct.c_void_p, ct.c_void_p],
                ct.byref(desc),
                ct.byref(initial),
                ct.byref(resource),
            )
        )
        return self.keep(resource)

    def read_bytes(self, resource, size):
        array = np.zeros(size, np.uint8)
        staging = self.buffer(array, staging=True)
        self.method(
            self.context, 47, [ct.c_void_p, ct.c_void_p], staging, resource, result=None
        )
        mapped = base.Mapped()
        self.check(
            self.method(
                self.context,
                14,
                [ct.c_void_p, ct.c_uint, ct.c_uint, ct.c_uint, ct.c_void_p],
                staging,
                0,
                1,
                0,
                ct.byref(mapped),
            )
        )
        result = np.frombuffer(ct.string_at(mapped.data, size), np.uint8).copy()
        self.method(self.context, 15, [ct.c_void_p, ct.c_uint], staging, 0, result=None)
        return result


FORMATS = {
    "R32_UINT": (42, 4),
    "R32_FLOAT": (41, 4),
    "R32G32B32A32_FLOAT": (2, 16),
    "UNKNOWN": (0, 0),
}


class Resource:
    def __init__(self, pointer, size, stride, fmt=0, structured=False):
        self.pointer = pointer
        self.size = size
        self.stride = stride
        self.fmt = fmt
        self.structured = structured
        self.views = {}


class GeneratedCloth:
    def __init__(self, root, sections, comp, namespace="unit", hardware=False):
        self.root = Path(root)
        self.sections = sections
        self.comp = comp
        self.ns = namespace
        self.gpu = GPU(hardware=hardware)
        self.resources = {}
        self.shaders = {}
        self.bindings = {}
        self.dispatches = 0
        self.params = np.zeros((128, 4), np.float32)
        self.ini, self.ini_view = self.gpu.ini(self.params)
        self.gpu.bind(120, self.ini_view)
        self.variables = {
            "time": 0,
            "$cursorY": 600,
            "$screenH": 1080,
            "$isMouseButtonDown": 0,
        }
        self.current_shader = None
        for line in sections.get("[Constants]", []):
            match = re.match(r"\s*global\s+(?:persist\s+)?(\$\w+)\s*=\s*(.*)", line)
            if match:
                try:
                    self.variables[match.group(1)] = float(match.group(2))
                except ValueError:
                    self.variables[match.group(1)] = 0
        self.prefix = "$ssmtcloth_" + namespace + "_"
        self.variables[self.prefix + "enabled"] = 1
        self.variables["$ssmtdrag_drag_enabled_" + namespace] = 2

    def close(self):
        self.gpu.release()

    def _declaration(self, name):
        return {
            m.group(1).lower(): m.group(2)
            for line in self.sections.get("[" + name + "]", [])
            if (m := re.match(r"\s*([\w_]+)\s*=\s*(.*?)\s*$", line))
        }

    def external(self, name, array, stride, fmt=0, structured=False, flags=8):
        raw = np.ascontiguousarray(array).view(np.uint8).ravel()
        pointer = self.gpu.raw_buffer(raw, flags, stride, structured)
        value = Resource(pointer, raw.nbytes, stride, fmt, structured)
        self.resources[name] = value
        return value

    def resource(self, name, copy_size=None):
        if name in self.resources:
            return self.resources[name]
        decl = self._declaration(name)
        if not decl:
            raise ValueError("Undeclared generated resource: " + name)
        fmt, element = FORMATS.get(
            decl.get("format", "UNKNOWN").replace("DXGI_FORMAT_", ""), (0, 0)
        )
        stride = int(decl.get("stride", element))
        structured = "structured" in decl.get("type", "").lower()
        writable = "rw" in decl.get(
            "type", ""
        ).lower() or "unordered_access" in decl.get("bind_flags", "")
        flags = (128 if writable else 0) | (
            1 if "vertex_buffer" in decl.get("bind_flags", "") else 8
        )
        filename = decl.get("filename")
        if filename:
            raw = np.fromfile(self.root / filename.replace("\\", "/"), np.uint8)
        else:
            size = int(decl.get("array", "0")) * stride or copy_size
            if not size:
                raise ValueError("Uninitialized dynamic resource: " + name)
            raw = np.zeros(size, np.uint8)
        value = Resource(
            self.gpu.raw_buffer(raw, flags, stride, structured),
            len(raw),
            stride,
            fmt,
            structured,
        )
        self.resources[name] = value
        return value

    def view(self, value, writable):
        if writable not in value.views:
            unit = (
                value.stride
                if value.structured
                else (
                    FORMATS.get(
                        next(
                            (k for k, v in FORMATS.items() if v[0] == value.fmt),
                            "UNKNOWN",
                        ),
                        (0, 0),
                    )[1]
                    or value.stride
                )
            )
            value.views[writable] = self.gpu.view(
                value.pointer, value.size // unit, value.fmt, writable
            )
        return value.views[writable]

    def _eval(self, text):
        text = text.strip()
        if "===" in text:
            a, b = map(str.strip, text.split("===", 1))
            return self.resources.get(a) is self.resources.get(b) or (
                a in self.resources
                and b in self.resources
                and self.resources[a].pointer == self.resources[b].pointer
            )
        expression = re.sub(
            r"\$[\w]+",
            lambda m: (
                "V" + str(list(self.variables).index(m.group()))
                if m.group() in self.variables
                else "0"
            ),
            text,
        )
        expression = expression.replace("&&", " and ").replace("||", " or ")
        values = {"V" + str(i): v for i, v in enumerate(self.variables.values())}
        values["time"] = self.variables["time"]
        binary = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
        }
        compare = {
            ast.Eq: operator.eq,
            ast.NotEq: operator.ne,
            ast.Lt: operator.lt,
            ast.LtE: operator.le,
            ast.Gt: operator.gt,
            ast.GtE: operator.ge,
        }

        def evaluate(node):
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                return values[node.id]
            if isinstance(node, ast.BinOp):
                return binary[type(node.op)](evaluate(node.left), evaluate(node.right))
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
                return -evaluate(node.operand)
            if isinstance(node, ast.BoolOp):
                return (
                    all(map(evaluate, node.values))
                    if isinstance(node.op, ast.And)
                    else any(map(evaluate, node.values))
                )
            if isinstance(node, ast.Compare):
                return all(
                    compare[type(op)](evaluate(a), evaluate(b))
                    for a, op, b in zip(
                        [node.left] + node.comparators, node.ops, node.comparators
                    )
                )
            raise ValueError("Unsupported generated expression: " + text)

        return evaluate(ast.parse(expression, mode="eval").body)

    def execute(self, name, depth=0):
        if depth > 12:
            raise ValueError("Generated command recursion")
        lines = self.sections["[" + name + "]"]
        active = True
        stack = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("if "):
                parent = active
                condition = bool(self._eval(line[3:])) if parent else False
                stack.append([parent, condition])
                active = parent and condition
                continue
            if line.startswith("elif "):
                parent, taken = stack[-1]
                condition = (
                    bool(self._eval(line[5:])) if parent and not taken else False
                )
                active = parent and condition
                stack[-1][1] |= condition
                continue
            if line == "else":
                parent, taken = stack[-1]
                active = parent and not taken
                stack[-1][1] = True
                continue
            if line == "endif":
                active = stack.pop()[0]
                continue
            if not active:
                continue
            key, sep, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if not sep:
                raise ValueError("Unsupported generated command: " + line)
            if key.startswith("$"):
                self.variables[key] = self._eval(value)
            elif re.fullmatch("[xyzw][0-9]+", key):
                self.params[int(key[1:]), "xyzw".index(key[0])] = self._eval(value)
            elif key == "run":
                self.execute(value, depth + 1)
            elif key == "cs":
                path = self.root / value
                if path not in self.shaders:
                    self.shaders[path] = self.gpu.shader(path)
                self.current_shader = self.shaders[path]
            elif key == "Dispatch":
                self.gpu.method(
                    self.gpu.context,
                    48,
                    [
                        ct.c_void_p,
                        ct.c_uint,
                        ct.c_void_p,
                        ct.c_void_p,
                        ct.c_uint,
                        ct.c_uint,
                    ],
                    self.ini,
                    0,
                    None,
                    self.params.ctypes.data,
                    0,
                    0,
                    result=None,
                )
                self.gpu.dispatch(self.current_shader, int(value.split(",")[0]))
                self.dispatches += 1
            elif key == "clear":
                resource = self.resource(value)
                view = self.view(resource, True)
                zeros = (ct.c_uint * 4)(0, 0, 0, 0)
                self.gpu.method(
                    self.gpu.context,
                    51,
                    [ct.c_void_p, ct.c_void_p],
                    view,
                    zeros,
                    result=None,
                )
            elif re.fullmatch("cs-[tu][0-9]+", key):
                writable = key[3] == "u"
                slot = int(key[4:])
                view = None
                if value != "null":
                    view = self.view(
                        self.resource(re.sub(r"^ref\s+", "", value)), writable
                    )
                self.gpu.bind(slot, view, writable)
            elif key.startswith("Resource") or key in ("vb0", "vb2"):
                parts = value.split()
                mode = parts[0]
                source = parts[1] if mode in ("copy", "ref") else parts[0]
                if source not in self.resources:
                    try:
                        self.resource(source)
                    except ValueError:
                        if "unless_null" in parts:
                            continue
                        raise
                src = self.resources[source]
                if mode == "copy":
                    target = self.resource(key, src.size)
                    if target.size != src.size:
                        raise ValueError("Generated copy size mismatch: " + line)
                    self.gpu.method(
                        self.gpu.context,
                        47,
                        [ct.c_void_p, ct.c_void_p],
                        target.pointer,
                        src.pointer,
                        result=None,
                    )
                else:
                    decl = self._declaration(key)
                    fmt, _unit = FORMATS.get(
                        decl.get("format", "UNKNOWN"), (src.fmt, 0)
                    )
                    stride = int(decl.get("stride", src.stride))
                    self.resources[key] = Resource(
                        src.pointer, src.size, stride, fmt, src.structured
                    )
            else:
                raise ValueError("Unsupported generated command: " + line)
        if stack:
            raise ValueError("Unbalanced generated conditions: " + name)

    def prepare_identity(self):
        meta = self.comp["cloth"]
        folder = self.root / meta["directory"]
        stride = meta["stride"]
        native = np.fromfile(folder / "bind.buf", np.uint8).reshape(-1, stride)
        self.native = native.copy()
        bones = np.eye(4, dtype=np.float32)[:3].copy()
        for slot in (1, 2):
            self.external(
                f'ResourceZZMergedSkeleton_G{meta["group"]}_s{slot}', bones, 16, 2
            )
            self.external(
                f'{meta["native_family"]}_G{meta["group"]}_s{slot}',
                native,
                stride,
                flags=25,
            )
        self.external("vb2", np.fromfile(folder / "blend.buf", np.uint8), 32, flags=1)
        self.capture_skin_inputs()
        self.resources["vb0"] = self.resources[
            f'{meta["native_family"]}_G{meta["group"]}_s1'
        ]

    def capture_skin_inputs(self):
        """Run the emitted capture commands at a simulated deform draw."""
        meta = self.comp["cloth"]
        prefix = f'ResourceSSMTCloth_{self.comp["comp_name"]}_{self.ns}_'
        command = "[TestSkinCapture]"
        for slot in (1, 2):
            section = next(
                section for section, _, i, _, _ in meta["skin_writes"] if i == slot
            )
            names = tuple(
                prefix + f"{name}_s{slot} = " for name in ("SkinSource", "SkinPalette")
            ) + (self.prefix + self.comp["comp_name"] + f"_ready_s{slot} = ",)
            self.sections[command] = [
                line
                for line in self.sections[section]
                if line.strip().startswith(names)
            ][:3]
            self.resources["so0"] = self.resources[
                f'{meta["native_family"]}_G{meta["group"]}_s{slot}'
            ]
            self.execute(command.strip("[]"))
        del self.sections[command]

    def frame(self, cursor=600, held=False, hit_row=None, slot=1):
        self.variables["time"] += 1 / 60
        self.variables["$cursorY"] = cursor
        self.variables["$isMouseButtonDown"] = int(held)
        self.variables["$zz_ms_occ_" + str(self.comp["cloth"]["occ"])] = slot
        meta = self.comp["cloth"]
        self.resources["vb0"] = self.resources[
            f'{meta["native_family"]}_G{meta["group"]}_s{slot}'
        ]
        if hit_row is not None:
            name = (
                "ResourceDragPinnedComponentInfo_"
                + self.comp["comp_name"]
                + "_"
                + self.ns
            )
            value = self.resource(name)
            data = np.zeros((value.size // 16, 4), np.float32)
            data[0, 0] = 0
            data[3] = [hit_row, hit_row, hit_row, 0]
            self.gpu.method(
                self.gpu.context,
                48,
                [
                    ct.c_void_p,
                    ct.c_uint,
                    ct.c_void_p,
                    ct.c_void_p,
                    ct.c_uint,
                    ct.c_uint,
                ],
                value.pointer,
                0,
                None,
                data.ctypes.data,
                0,
                0,
                result=None,
            )
        self.execute(self.comp["cloth_bind"])
        output = self.resources["vb0"]
        return self.gpu.read_bytes(output.pointer, output.size).reshape(
            -1, meta["stride"]
        )

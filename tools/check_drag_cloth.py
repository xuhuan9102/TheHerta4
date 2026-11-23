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
    def texture2d(self, width, height, fmt, *, array=1, msaa=1, mips=1, staging=False):
        desc = (ct.c_uint * 11)(width, height, mips, array, fmt, msaa, 0,
                               3 if staging else 0, 0 if staging else 8 | 128,
                               0x20000 if staging else 0, 0)
        texture = ct.c_void_p()
        self.check(self.method(self.device, 5, [ct.c_void_p, ct.c_void_p, ct.c_void_p],
                               ct.byref(desc), None, ct.byref(texture)))
        return self.keep(texture)

    def texture_view(self, resource, writable):
        view = ct.c_void_p()
        self.check(self.method(self.device, 8 if writable else 7,
                               [ct.c_void_p, ct.c_void_p, ct.c_void_p], resource, None, ct.byref(view)))
        return self.keep(view)

    def read_texture(self, texture, width, height, fmt, stride):
        staging = self.texture2d(width, height, fmt, staging=True)
        self.method(self.context, 47, [ct.c_void_p, ct.c_void_p], staging, texture, result=None)
        mapped = base.Mapped()
        self.check(self.method(self.context, 14,
                               [ct.c_void_p, ct.c_uint, ct.c_uint, ct.c_uint, ct.c_void_p],
                               staging, 0, 1, 0, ct.byref(mapped)))
        result = np.frombuffer(ct.string_at(mapped.data, mapped.row * height), np.uint8)
        self.method(self.context, 15, [ct.c_void_p, ct.c_uint], staging, 0, result=None)
        return result.reshape(height, mapped.row)[:, :width * stride].copy()

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
    "R32G32_UINT": (17, 8),
    "R32G32B32A32_FLOAT": (2, 16),
    "R32G32B32A32_UINT": (3, 16),
    "R32G32B32_FLOAT": (6, 12),
    "R32G32B32_UINT": (7, 12),
    "R32G32_FLOAT": (16, 8),
    "R32G32_UINT": (17, 8),
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
        self.texture = None


class GeneratedCloth:
    def __init__(
        self,
        root,
        sections,
        comp,
        namespace="unit",
        hardware=False,
        legacy_region_copy=True,
    ):
        self.root = Path(root)
        self.sections = sections
        self.comp = comp
        self.ns = namespace
        self.legacy_region_copy = legacy_region_copy
        self.gpu = GPU(hardware=hardware)
        self.resources = {}
        self.shaders = {}
        self.bindings = {}
        self.dispatches = 0
        self.params = np.zeros((512, 4), np.float32)
        self.ini, self.ini_view = self.gpu.ini(self.params)
        self.gpu.bind(120, self.ini_view)
        self.variables = {
            "time": 0,
            "$cursorY": 600,
            "$cursorX": 800,
            "$screenH": 1080,
            "$isMouseButtonDown": 0,
            "$inputMode": 0,
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
        # Present normally replaces the INI's 1x1 startup dimensions.
        self.variables["$screenW"] = 1920
        self.variables["$screenH"] = 1080
        self.variables["$ssmtdrag_drag_enabled_" + namespace] = 2
        # Solver replays explicitly opt into an already grabbed garment.
        # Cold-idle tests clear this flag to exercise the native passthrough.
        if comp.get("cloth"):
            activity = f"$ssmtcloth_{namespace}_{comp['comp_name']}_"
            self.variables[activity + "activated"] = 1
            self.variables[activity + "activation_reset"] = self.variables.get(
                self.prefix + "reset", 0
            )
        self.force_full_solver = True

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

    def resource(self, name, copy_size=None, copy_source=None):
        if name in self.resources:
            current = self.resources[name]
            if not copy_size:
                return current
            declaration = self._declaration(name)
            if (
                copy_size
                and current.size != copy_size
                and not declaration.get("array")
                and not declaration.get("filename")
            ):
                # The loader recreates dynamic copies when the native SO
                # allocation changes between character instances.
                del self.resources[name]
            else:
                return current
        decl = self._declaration(name)
        if not decl:
            raise ValueError("Undeclared generated resource: " + name)
        fmt, element = FORMATS.get(
            decl.get("format", "UNKNOWN").replace("DXGI_FORMAT_", ""), (0, 0)
        )
        if decl.get("type", "").casefold() == "texture2d":
            width, height = int(decl["width"]), int(decl["height"])
            texture = self.gpu.texture2d(width, height, fmt, array=int(decl.get("array", 0)),
                                         msaa=int(decl.get("msaa", 0)), mips=int(decl.get("mips", 0)))
            resource = Resource(texture, width * height * element, element, fmt)
            resource.texture = (width, height)
            self.resources[name] = resource
            return resource
        stride = int(decl.get("stride", element))
        structured = "structured" in decl.get("type", "").lower()
        if (
            copy_source is not None
            and copy_source.structured
            and decl.get("misc_flags") != "none"
        ):
            structured = True
        binds = decl.get("bind_flags", "").split()
        writable = "rw" in decl.get("type", "").lower() or "unordered_access" in binds
        flags = sum(bit for name, bit in (("vertex_buffer", 1), ("index_buffer", 2)) if name in binds) or 8
        if writable:
            flags |= 128
        filename = decl.get("filename")
        if filename:
            raw = np.fromfile(self.root / filename.replace("\\", "/"), np.uint8)
        elif "data" in decl:
            values = re.split(r"[\s,]+", decl["data"].strip())
            if decl.get("format", "").endswith("UINT"):
                parsed = [int(v, 16) if v.lower().startswith("0x") else int(v) for v in values]
                payload = np.asarray(parsed, "<u4").view(np.uint8)
            else:
                payload = np.asarray([float(v) for v in values], "<f4").view(np.uint8)
            size = int(decl.get("array", "0")) * stride or payload.nbytes
            if payload.nbytes > size:
                raise ValueError("Inline resource data exceeds capacity: " + name)
            raw = np.zeros(size, np.uint8)
            raw[:payload.nbytes] = payload
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
            if value.texture is not None:
                value.views[writable] = self.gpu.texture_view(value.pointer, writable)
                return value.views[writable]
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
        if "===" in text or "!==" in text:
            symbol = "!==" if "!==" in text else "==="
            a, b = map(str.strip, text.split(symbol, 1))
            same = self.resources.get(a) is self.resources.get(b) or (
                a in self.resources
                and b in self.resources
                and self.resources[a].pointer == self.resources[b].pointer
            )
            return not same if symbol == "!==" else same
        if text in self.variables:
            return self.variables[text]
        if text and text[0] in "-.0123456789":
            try:
                return float(text) if "." in text or "e" in text.lower() else int(text)
            except ValueError:
                pass
        values = {"time": self.variables["time"]}

        def substitute(match):
            name = match.group()
            if name not in self.variables:
                return "0"
            alias = "V" + str(len(values))
            values[alias] = self.variables[name]
            return alias

        expression = re.sub(r"\$[\w]+", substitute, text)
        expression = expression.replace("&&", " and ").replace("||", " or ")
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
                if re.fullmatch("[xyzw][0-9]+", node.id):
                    return self.params[int(node.id[1:]), "xyzw".index(node.id[0])]
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

        if not hasattr(self, "_expression_cache"):
            self._expression_cache = {}
        if expression not in self._expression_cache:
            self._expression_cache[expression] = ast.parse(expression, mode="eval").body
        return evaluate(self._expression_cache[expression])

    def execute(self, name, depth=0):
        if depth > 12:
            raise ValueError("Generated command recursion")
        lines = self.sections["[" + name + "]"]
        active = True
        stack = []
        for index, raw in enumerate(lines):
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
            elif key.startswith("local $"):
                self.variables[key.removeprefix("local ")] = self._eval(value)
            elif re.fullmatch("[xyzw][0-9]+", key):
                self.params[int(key[1:]), "xyzw".index(key[0])] = self._eval(value)
            elif key == "run":
                self.execute(value, depth + 1)
            elif key == "cs":
                path = self.root / value
                if path not in self.shaders:
                    self.shaders[path] = self.gpu.shader(path)
                self.current_shader = self.shaders[path]
            elif key.casefold() == "dispatch":
                # Explicit solver replays keep exercising the solver even
                # without synthetic pointer input. Production always writes 0.
                self.params[127, 1] = float(self.force_full_solver)
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
                groups = tuple(int(part) for part in value.split(","))
                self.gpu.dispatch(self.current_shader, *groups)
                self.dispatches += 1
            elif key == "store":
                variable, resource, offset = (part.strip() for part in value.split(","))
                bound = self.resource(resource)
                values = self.gpu.read_bytes(bound.pointer, bound.size).view("<f4").ravel()
                self.variables[variable] = float(values[int(offset)])
            elif key == "clear":
                resource = self.resource(value.split()[0])
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
                    mode, _, reference = value.partition(" ")
                    source_name = reference if mode in ("copy", "ref") else value
                    source = self.resource(
                        source_name
                    )
                    if source is None:
                        self.resources.pop(key, None)
                        self.gpu.bind(slot, None, writable)
                        continue
                    if mode not in ("copy", "ref"):
                        same_register_type = bool(re.fullmatch(r"cs-[tu][0-9]+", source_name)) and source_name[3] == key[3]
                        mode = "ref" if source_name.startswith("Resource") or same_register_type else "copy"
                    bound = source
                    if mode == "copy":
                        identity = f"{name}:{index}:{key}"
                        structured = source.structured or (
                            source.fmt == 0 and source.stride > 0
                        )
                        cached = self.resources.get(identity)
                        if cached is None or (cached.size, cached.stride, cached.fmt, cached.structured) != (source.size, source.stride, source.fmt, structured):
                            self.external(
                                identity,
                                np.zeros(source.size, np.uint8),
                                source.stride,
                                source.fmt,
                                structured,
                                flags=8 | (128 if writable else 0),
                            )
                        bound = self.resources[identity]
                        self.gpu.method(
                            self.gpu.context,
                            47,
                            [ct.c_void_p, ct.c_void_p],
                            bound.pointer,
                            source.pointer,
                            result=None,
                        )
                    self.resources[key] = bound
                    view = self.view(bound, writable)
                else:
                    self.resources.pop(key, None)
                self.gpu.bind(slot, view, writable)
            elif key.startswith("Resource") or key in ("vb0", "vb2"):
                if value == "null":
                    self.resources.pop(key, None)
                    continue
                parts = value.split()
                mode = parts[0]
                source = parts[1] if mode in ("copy", "ref") else parts[0]
                if key.startswith("Resource") and mode not in ("copy", "ref"):
                    mode = "copy"
                region = re.fullmatch(r"(.+)->Region\((\d+),(\d+)\)", source)
                if region:
                    source = region[1]
                if mode == "ref" and re.fullmatch(r"cs-[tu][0-9]+", source) and source not in self.resources:
                    self.resources[key] = None
                    continue
                if source not in self.resources:
                    try:
                        self.resource(source)
                    except ValueError:
                        if "unless_null" in parts:
                            continue
                        raise
                src = self.resources[source]
                if mode == "copy":
                    size = int(region[3]) if region else src.size
                    target = self.resource(key, size, copy_source=src)
                    if target.size != size:
                        raise ValueError("Generated copy size mismatch: " + line)
                    if region:
                        offset = int(region[2])
                        if offset + size > src.size:
                            raise ValueError(
                                "Generated region outside native capacity: " + line
                            )
                        if self.legacy_region_copy and (offset or size != src.size):
                            raise ValueError(
                                "Loaded legacy backend issues CopyResource for Region views: "
                                + line
                            )
                        box = (ct.c_uint * 6)(offset, 0, 0, offset + size, 1, 1)
                        self.gpu.method(
                            self.gpu.context,
                            46,
                            [
                                ct.c_void_p,
                                ct.c_uint,
                                ct.c_uint,
                                ct.c_uint,
                                ct.c_uint,
                                ct.c_void_p,
                                ct.c_uint,
                                ct.c_void_p,
                            ],
                            target.pointer,
                            0,
                            0,
                            0,
                            0,
                            src.pointer,
                            0,
                            ct.byref(box),
                            result=None,
                        )
                        continue
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
                        decl.get("format"), (src.fmt, 0)
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
            lines = self.sections[section]
            at = next(
                i
                for i, line in enumerate(lines)
                if line.strip().startswith(prefix + f"SkinSource_s{slot} = ")
            )
            first = max(i for i in range(at) if "SSMT CLOTH DRAW BEGIN" in lines[i])
            last = next(
                i for i in range(at, len(lines)) if "SSMT CLOTH DRAW END" in lines[i]
            )
            self.sections[command] = lines[first + 1 : last]
            self.resources["so0"] = self.resources[
                f'{meta["native_family"]}_G{meta["group"]}_s{slot}'
            ]
            self.resources["cs-u0"] = self.resources["so0"]
            self.execute(command.strip("[]"))
        # The simulated capture aliases the SO only for its capture block.
        self.resources.pop("cs-u0", None)
        del self.sections[command]

    def frame(
        self,
        cursor=600,
        held=False,
        hit_row=None,
        slot=1,
        zone=0,
        cursor_x=None,
        camera_basis=None,
        advance_time=True,
        route_gesture=False,
    ):
        # Deferred command-list execution or another diagnostic dispatch can
        # reset context bindings. Each replay frame owns its INI view.
        self.gpu.bind(120, self.ini_view)
        if advance_time:
            self.variables["time"] += 1 / 60
        if self.force_full_solver:
            self.variables[f"$ssmtcloth_{self.ns}_{self.comp['comp_name']}_solve_until"] = self.variables["time"] + 1
        self.variables["$cursorY"] = cursor
        self.variables["$isMouseButtonDown"] = int(held)
        if cursor_x is not None:
            self.variables["$cursorX"] = cursor_x
        self.variables["$zz_ms_occ_" + str(self.comp["cloth"]["occ"])] = slot
        meta = self.comp["cloth"]
        self.resources["vb0"] = self.resources[
            f'{meta["native_family"]}_G{meta["group"]}_s{slot}'
        ]
        name = f"ResourceDragPinnedComponentInfo_{self.comp['comp_name']}_{self.ns}"
        if hit_row is not None:
            value = self.resource(name)
            data = np.zeros((value.size // 16, 4), np.float32)
            data[0, 0] = 0
            data[3] = [hit_row, hit_row, hit_row, 0]
            data[7, 3] = zone
            if camera_basis is not None:
                basis = np.asarray(camera_basis, np.float32)
                if basis.shape == (2, 4):
                    data[8:10] = basis
                elif basis.shape == (2, 3):
                    data[8:10, :3] = basis
                    data[8, 3] = data[9, 3] = 1
                else:
                    raise ValueError(
                        "camera_basis must contain two float3 or float4 rows"
                    )
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
        if not route_gesture:
            var = f"$ssmtcloth_{self.ns}_{self.comp['comp_name']}_"
            self.variables[var + "pointer_held"] = int(held)
            self.variables[var + "intent"] = 2 if held else 0
            self.resources[f"ResourceSSMTCloth_{self.comp['comp_name']}_{self.ns}_GestureHit"] = self.resource(name)
        self.execute(self.comp["cloth_bind"])
        output = self.resources["vb0"]
        return self.gpu.read_bytes(output.pointer, output.size).reshape(
            -1, meta["stride"]
        )

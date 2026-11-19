"""Packed frame tables and independently reusable mesh pose groups."""

import hashlib
import json
from pathlib import Path
import shutil
import struct
import tempfile
import zipfile
import zlib
import numpy


def hash_array(digest, array):
    values = numpy.ascontiguousarray(array)
    digest.update(str((values.shape, values.dtype.descr)).encode("utf-8"))
    digest.update(values.view(numpy.uint8))


def hash_mesh_topology(digest, mesh):
    sharp_edges = mesh.attributes.get("sharp_edge")
    custom_normals = mesh.attributes.get("custom_normal")
    fields = [
        (mesh.loops, "vertex_index", numpy.int32, 1),
        (mesh.loops, "edge_index", numpy.int32, 1),
        (mesh.polygons, "loop_total", numpy.int32, 1),
        (mesh.polygons, "use_smooth", numpy.bool_, 1),
    ]
    if sharp_edges is not None:
        fields.append((sharp_edges.data, "value", numpy.bool_, 1))
    digest.update(bytes([bool(mesh.has_custom_normals)]))
    exact_normals = not mesh.has_custom_normals
    if custom_normals is not None:
        digest.update(custom_normals.data_type.encode("ascii"))
        if custom_normals.data_type == "INT16_2D":
            fields.append((custom_normals.data, "value", numpy.int32, 2))
            exact_normals = True
        elif custom_normals.data_type == "FLOAT_VECTOR":
            fields.append((custom_normals.data, "vector", numpy.float32, 3))
            exact_normals = True
    if not exact_normals:
        fields.append((mesh.corner_normals, "vector", numpy.float32, 3))
    uv = mesh.uv_layers.get("TEXCOORD.xy") or mesh.uv_layers.active
    if uv is not None:
        digest.update(uv.name.encode("utf-8"))
        fields.append((uv.data, "uv", numpy.float32, 2))
    for collection, prop, dtype, width in fields:
        values = numpy.empty(len(collection) * width, dtype=dtype)
        collection.foreach_get(prop, values)
        hash_array(digest, values)
    return exact_normals


class FrameTableCache:
    def __init__(self, path, context, sample_version=""):
        self.path = Path(path)
        self.context = context
        self.sample_version = sample_version
        self.candidate = None
        self.samples = []
        self.skipped_samples = False
        self.cacheable = True
        self.base_files = None
        self.ini_data = None
        try:
            with zipfile.ZipFile(self.path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            if manifest["context"] == context and all(
                key in manifest for key in ("samples", "base", "files", "ini")
            ):
                self.candidate = manifest
        except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile):
            pass

    def _pose_group_path(self, mesh, ordinal):
        mesh_id = hashlib.sha256(mesh.encode("utf-8")).hexdigest()[:24]
        return self.path.with_suffix(".poses") / mesh_id / f"{ordinal}.bin"

    @staticmethod
    def _pose_row_blocks(shape):
        floats_per_row = shape[0] * shape[2]
        rows = max(1, (4 * 1024**2) // (4 * floats_per_row))
        for start in range(0, shape[1], rows):
            yield slice(start, min(start + rows, shape[1]))

    def load_pose_group(self, mesh, ordinal, signature, shape):
        path = self._pose_group_path(mesh, ordinal)
        expected = hashlib.sha256(signature.encode("ascii")).digest()
        try:
            with path.open("rb") as stream:
                if stream.read(32) != expected:
                    return None
                if struct.unpack("<III", stream.read(12)) != shape:
                    return None
                data = numpy.empty(shape, dtype=numpy.float32)
                for rows in self._pose_row_blocks(shape):
                    count = rows.stop - rows.start
                    size, = struct.unpack("<I", stream.read(4))
                    raw_size = 4 * count * shape[0] * shape[2]
                    if not 0 < size <= raw_size + raw_size // 50 + 128:
                        return None
                    decoder = zlib.decompressobj()
                    payload = decoder.decompress(stream.read(size), raw_size + 1)
                    if len(payload) != raw_size or not decoder.eof or decoder.unused_data:
                        return None
                    block = numpy.frombuffer(payload, dtype=numpy.float32).reshape(count, shape[2], shape[0])
                    data[:, rows] = block.transpose(2, 0, 1)
                if not stream.read(1):
                    return data
        except (OSError, ValueError, struct.error, zlib.error):
            pass
        return None

    def store_pose_group(self, mesh, ordinal, signature, poses):
        path = self._pose_group_path(mesh, ordinal)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pose-", dir=path.parent) as folder:
            temp = Path(folder) / "poses.bin"
            with temp.open("wb") as stream:
                stream.write(hashlib.sha256(signature.encode("ascii")).digest())
                stream.write(struct.pack("<III", *poses.shape))
                for rows in self._pose_row_blocks(poses.shape):
                    # Store each row's samples together so unchanged channels
                    # remain close enough for lossless compression.
                    block = numpy.ascontiguousarray(poses[:, rows].transpose(1, 2, 0), dtype=numpy.float32)
                    payload = zlib.compress(block, level=1)
                    stream.write(struct.pack("<I", len(payload)))
                    stream.write(payload)
            temp.replace(path)

    def reset_probe(self):
        self.candidate = None
        self.samples.clear()
        self.skipped_samples = False
        self.cacheable = True

    def record_sample(self, signature):
        index = len(self.samples)
        self.samples.append(signature)
        if signature is None:
            self.cacheable = False
        if self.candidate is not None:
            expected = self.candidate["samples"]
            if signature and index < len(expected) and signature == expected[index]:
                self.skipped_samples = True
                return True
            self.candidate = None
        return False

    @staticmethod
    def _buffer_digest(path, tangent_layout=None):
        if tangent_layout is not None:
            stride, start, count = tangent_layout
            rows = numpy.fromfile(path, dtype=numpy.uint8).reshape(-1, stride)
            rows[:, 4 * start:4 * (start + count)] = 0
            return hashlib.sha256(rows).hexdigest()
        with path.open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()

    @classmethod
    def _base_digests(cls, output, layouts=None):
        result = {}
        for path in sorted((output / "Meshes0000").glob("*.buf")):
            result[path.name] = cls._buffer_digest(path, (layouts or {}).get(path.name))
        return result

    def finish_probe(self, output):
        output = Path(output)
        if not self.cacheable:
            return False
        manifest = self.candidate
        layouts = manifest.get("tangent_layouts") if manifest and self.skipped_samples else None
        try:
            self.base_files = self._base_digests(output, layouts)
        except (OSError, ValueError):
            return False
        if not (
            self.skipped_samples and manifest
            and manifest["samples"] == self.samples
            and manifest["base"] == self.base_files
        ):
            return False

        try:
            # A conflicting worker may have replaced this bundle since the probe.
            # Stage and CRC-check all files before publishing any output.
            with zipfile.ZipFile(self.path) as archive, tempfile.TemporaryDirectory(
                prefix=".frame-table-restore-", dir=output
            ) as folder:
                if json.loads(archive.read("manifest.json")) != manifest:
                    return False
                files = [Path(name) for name in manifest["files"]]
                if not files or any(
                    len(path.parts) != 2 or ".." in path.parts or path.parts[0] not in ("Meshes0000", "res")
                    for path in files
                ):
                    return False
                staged = Path(folder)
                for path in files:
                    dest = staged / path
                    dest.parent.mkdir(exist_ok=True)
                    with archive.open(path.as_posix()) as source, dest.open("wb") as result:
                        shutil.copyfileobj(source, result, length=4 * 1024 * 1024)
                for path in files:
                    dest = output / path
                    dest.parent.mkdir(exist_ok=True)
                    if path.suffix == ".hlsl" and dest.is_file() and dest.read_bytes() == (staged / path).read_bytes():
                        continue
                    (staged / path).replace(dest)
            self.ini_data = manifest["ini"]
            return True
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            return False

    def store(self, output, files, ini_data, tangent_layouts=None):
        if not (self.cacheable and self.samples and self.base_files and files):
            return
        output = Path(output)
        base = dict(self.base_files)
        files = list(files)
        # MikkTSpace can produce tiny changes in tangent accumulation order.
        # Pair the original base tangent rows with their cached frame deltas,
        # while requiring all other bytes and the sampling inputs to match.
        for name, layout in (tangent_layouts or {}).items():
            path = Path("Meshes0000") / name
            if base.get(name) != self._buffer_digest(output / path):
                return
            base[name] = self._buffer_digest(output / path, layout)
            files.append(path)
        manifest = {
            "context": self.context,
            "samples": self.samples,
            "base": base,
            "tangent_layouts": tangent_layouts or {},
            "files": list(dict.fromkeys(Path(path).as_posix() for path in files)),
            "ini": ini_data,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="build-", dir=self.path.parent) as folder:
            bundle = Path(folder) / "frames.zip"
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_STORED) as archive:
                for path in manifest["files"]:
                    archive.write(output / path, path)
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            bundle.replace(self.path)

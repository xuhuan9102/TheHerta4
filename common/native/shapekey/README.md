# ShapeKey native kernels (Windows x64, ABI 1)

Runtime: `../herta_shapekey_windows_x64.dll`. Python adapter:
`common/shapekey_native.py`. Source/build project:
<https://github.com/v4806/herta-bc7-native>, `addon_native/shapekey_sample.cpp`.
The source repository retains its existing visibility. `BUILD.json` records the
exact source and DLL SHA256 used in this installation.

The C ABI provides batched relative-key evaluation, stable packed slot deltas,
and float32-to-float16 float3 packing. Compile with MSVC x64 C++20, `/O2 /MD
/EHsc /fp:precise`. SSE2 is sufficient; fast-math and FMA contraction must not be
enabled. Default worker count is four; small batches stay single-threaded.
Build out of tree with the source repository's `addon_native/CMakeLists.txt` and
`-DHERTA_BUILD_BC7_ENCODER=OFF` if only geometry/ShapeKey modules are needed.

Blender RNA is read on its owning thread. Native workers access only owned
NumPy arrays. Sampling bypasses dependency evaluation only for relative keys
without visible modifiers, animation/drivers, masks, muted keys or edit/pinned
shape modes. Other objects retain the evaluated-mesh sampler. Relative-key
chains and non-mirror coordinate conversion retain their existing semantics.

Slot packing retains NumPy's default isclose tolerances (rtol=1e-5, atol=1e-6),
channel selection, stable vertex ordering, global indices and positive zero
canonicalization. Half packing uses IEEE round-to-nearest-even and preserves
NaN payload/sign bits and global record-pair alignment across odd-sized groups.
Frame groups are packed without concatenating all float32 records. Large
contiguous arrays are written through buffer views rather than copied to bytes.

`HERTA_SHAPEKEY_FAST=0` restores evaluated-mesh sampling for diagnosis.
`HERTA_SHAPEKEY_NATIVE=0` disables these native kernels while retaining the
batch-sampling NumPy path. Missing/incompatible DLLs keep the NumPy paths.

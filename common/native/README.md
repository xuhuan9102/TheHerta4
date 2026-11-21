# DDS native acceleration

`herta_dds_windows_x64.dll` is an optional standalone C accelerator for BC7 base-mip
decoding and exact RGB mean pooling. `common/dds_pixel.py` loads it through `ctypes`.
It has no Python ABI or C runtime dependency. Other platforms, missing DLLs and
other DDS formats use the existing numpy decoder.

The endpoint rounding, reserved blocks, alpha, orientation and cropped edge blocks
match the existing numpy implementation. Fused pooling visits all base-mip pixels
and produces the same means as `_similarity_pool`, without allocating the complete
RGBA image. Pixel dedup still decodes the complete image when its digest is needed.

The DLL is reentrant. `ctypes.CDLL` releases the GIL, so the export planner's existing
bounded worker pool can execute independent files concurrently. There are no nested
thread pools in the native decoder.

Rebuild on Windows x64 with MSVC:

```powershell
python scripts/build_dds_native.py --vcvars '<Visual Studio>/VC/Auxiliary/Build/vcvars64.bat'
```

Tables are generated from the decoder's existing constants. The source, table
generator, generated `dds_tables.h` and tests are included; build intermediates
are stored in the system temporary directory. Close worker processes using the
DLL before rebuilding it.

Validation:

```powershell
python -m unittest discover -s tests -p test_dds_native.py
```

## Source build

The checked-in source can build the independent native modules with:

```powershell
cmake -S common/native -B build/native -A x64
cmake --build build/native --config Release
```

This builds the DDS decoder, geometry hash/packing, export geometry,
DDS-to-PNG, ShapeKey, BC7 texture encoding and PNG analysis DLLs. The required
Basis Universal transcoder snapshot is vendored at commit
`ee771a7f913a3233a252eb6d281b4446f6a3c9f5`.
When the ZZMI skeleton-fusion module from PR #87 is present, its CMake target is
included automatically.

```powershell
cmake -S common/native -B build/native -A x64
cmake --build build/native --config Release
```

Keep `/fp:precise` and the x64 SSE2 baseline. The BC7 and PNG analysis sources
and pinned Basis dependency are in `common/native/bc7f`.

## DDS to PNG

`herta_dds_png_windows_x64.dll` provides ABI 1 for an in-process, lossless PNG
conversion pipeline. Its complete source and pinned bcdec/libdeflate dependencies
are in `common/native/dds_png`; the root native CMake project builds it. The
snapshot origin and hashes are recorded in `dds_png/BUILD.json`. See
`DDS_PNG_THIRD_PARTY.txt` for licenses.

BC7/BC6H, common RGBA/BGRA DDS, and half/float DDS are supported. HDR becomes
16-bit RGBA without a gamma transform. sRGB inputs get sRGB/gAMA chunks; linear
inputs get neither. Eight file calls may run concurrently, without inner thread
pools. PNG compression level 1 is lossless and favors conversion speed.
`HERTA_DDS_PNG_NATIVE=0` selects the existing texconv fallback for diagnosis.
Unknown formats, unavailable DLLs and conversion errors also fall back.
The wrapper publishes a temporary PNG atomically. Existing PNG reuse, original
DDS retention, Blender colorspace/alpha and main-thread relinking are unchanged.

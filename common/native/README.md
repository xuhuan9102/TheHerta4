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
generator and tests are included; build intermediates are stored in the system
temporary directory. Close worker processes using the DLL before rebuilding it.

Validation:

```powershell
python -m unittest discover -s tests -p test_dds_native.py
```

## DDS to PNG

`herta_dds_png_windows_x64.dll` provides ABI 1 for an in-process, lossless PNG
conversion pipeline. The source is maintained in `v4806/herta-bc7-native`, under
`addon_native/dds_png`; build with that repository's CMake project. The shipped
DLL includes the Herta BC7 kernel with standard endpoint bit replication,
bcdec's BC6H decoder and libdeflate. See `DDS_PNG_THIRD_PARTY.txt` for licenses.

BC7/BC6H, common RGBA/BGRA DDS, and half/float DDS are supported. HDR becomes
16-bit RGBA without a gamma transform. sRGB inputs get sRGB/gAMA chunks; linear
inputs get neither. Eight file calls may run concurrently, without inner thread
pools. PNG compression level 1 is lossless and favors conversion speed.
`HERTA_DDS_PNG_NATIVE=0` selects the existing texconv fallback for diagnosis.
Unknown formats, unavailable DLLs and conversion errors also fall back.
The wrapper publishes a temporary PNG atomically. Existing PNG reuse, original
DDS retention, Blender colorspace/alpha and main-thread relinking are unchanged.

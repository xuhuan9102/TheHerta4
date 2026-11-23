# Native BC7 export encoder

PNG similarity analysis additionally uses `herta_png_analysis_windows_x64.dll`
(ABI 1, x64 SSE2 baseline), built from `png_analysis.cpp` and the same vendored
stb_image.h. `common/png_native.py` exposes raw RGBA8 or exact RGB mean pooling.
Only static 8-bit PNGs use it; other precision/animation inputs keep Blender's
fallback. It performs no gamma, alpha or profile transform, matching Non-Color
analysis. The current project's 178 supported PNGs matched Blender bytes and
the original pooled features exactly. `TextureExportPlan.prewarm_sources` uses
bounded workers without accessing bpy; representatives are selected in the
original registration order. The master Export Cache switch controls persisted
features. `HERTA_NATIVE_PNG_ANALYSIS=0` disables this optional accelerator.

`herta_texture_windows_x64.dll` is the installed Windows x64 SSE2-baseline
encoder, ABI 1. The Python adapter is `toolkit/dds_native_encoder.py`; the
single/batch export paths in `toolkit/tt_dds_conversion.py` call it by default
after checking reusable DDS artifacts.

Supported inputs: PNG bytes (including PNG stored under a .dds extension),
8-bit or supported low-bit palette/gray, power-of-two dimensions <=16384,
BC7_UNORM/BC7_UNORM_SRGB, full mip chain. Unsupported precision, dimensions,
ICC/custom gamma, other source containers and output formats fall back to
the existing texconv path. The selected merge node still decides overwrite.

BC7 is lossy. This encoder preserves output format and the current color/mip
rules, but its BC7 blocks are not byte-identical to texconv. UNORM inputs
ignore PNG sRGB metadata, matching --ignore-srgb; SRGB outputs use source-aware
filtering. Prevalidated full DDS artifacts can be restored without re-encoding.

The binary uses the dynamic MSVC runtime; missing runtime/ABI/platform support
causes an automatic texconv fallback. It has no AVX2 requirement. Set
`HERTA_NATIVE_DDS_ENCODER=0` to disable only native encoding.

Basis Universal/BC7f is pinned to commit
`ee771a7f913a3233a252eb6d281b4446f6a3c9f5` (Apache-2.0); its LICENSE and NOTICE
and required transcoder sources are retained under `vendor/basis_universal`.
stb_image v2.30 retains its dual license at the end of `stb_image.h`, original
Git blob `9eedabedc45b3e6fd88fae6f14a160b4d53272ec`. Custom wrapper code follows
TheHerta4's GPL-3.0 licensing.

With a normal MSVC + Windows SDK toolchain, configure
`common/native/CMakeLists.txt` and build Release/x64. `BASIS_UNIVERSAL_ROOT`
defaults to the vendored snapshot and remains overridable for dependency audits.
`BUILD.json` records the wrapper inputs and production DLL digest.

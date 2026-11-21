# Dedicated DDS to PNG engine

ABI 1: `herta_dds_png_abi_version()` and
`herta_dds_to_png(const wchar_t *source, const wchar_t *temporary_destination,
int compression_level, HertaPngStats *stats)`.
Status 0 means success, 1 unsupported/invalid DDS, 2 IO error, 3 allocation/encode
error. The caller owns destination publication and removal of failed temporary
files. Never pass the source itself as the destination. Paths are Unicode.

The pipeline reads the first 2D mip, decodes to RGBA, selects PNG None/Sub/Up
filters by sampled row scores, applies filters using SSE2, and compresses with
libdeflate. Every call owns its buffers and compressor. Parallelize files on the
caller; there is no internal thread pool or Blender/Python dependency.

Supported: BC7, unsigned/signed BC6H, RGBA8/BGRA8/BGRX8, R/RG/RGBA half and
R/RG/RGB/RGBA float, including legacy D3DFMT half/float. Arrays/cubemaps/volume
textures, incomplete payloads, unusual layouts and >64M pixel images are rejected
for fallback. HDR values are clamped and rounded to RGBA16 UNORM without sRGB
encoding. sRGB integer images retain metadata; linear images omit color chunks.

The BC7 kernel is compiled with `HERTA_PNG_STANDARD_ENDPOINTS`, matching standard
bit replication. The existing DDS analysis DLL keeps its historical rounding
when built without the definition. BC6H uses the pinned, unmodified bcdec header.
PNG container/filter code is original; libdeflate provides DEFLATE and checksums.
Dependency commits are recorded in `vendor/PINS.json`; third-party licenses are
retained. Build through the parent CMake project with MSVC x64, `/O2 /fp:precise`.

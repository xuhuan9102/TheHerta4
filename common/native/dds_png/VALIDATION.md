# Validation (2026-10-26)

Production sample: 51 DDS textures, including 17 BC7 sRGB, 14 BC7 linear,
10 BC6H unsigned and 10 legacy RGBA32. Every decoded PNG channel is identical
to the existing texconv output, including all 16 bits of the HDR output;
all PNG bit depths and color metadata match. Compressed PNG bytes differ.

Additional seeded boundary corpus: 17 DXGI formats (98, 99, 95, 96, 28, 29,
87, 88, 91, 93, 2, 6, 10, 16, 34, 41, 54). Covers all BC7 modes, signed and
unsigned BC6H, cropped edge blocks, odd image dimensions, negative/out-of-range
float values, NaN and infinities. PNG channel values and bit depth match texconv.
Single-channel float expands to grayscale; float-to-UNORM rounds by adding
0.5 after float32 multiplication, matching the reference path.

Three alternating runs through the addon's actual worker/conversion entry:

| Run | texconv (16 workers) | Native (8 active calls) |
| --- | ---: | ---: |
| 1 | 2.5149 s | 0.8459 s |
| 2 | 2.4574 s | 0.8654 s |
| 3 | 2.4314 s | 0.9241 s |
| Median | 2.4574 s | 0.8654 s |

This measures forced file conversion from existing source copies with warm
filesystem caches, not complete workspace import or cold-disk performance.
Median throughput improves 2.84x; time decreases 64.8%. Level 1 lossless PNG
totals 82,085,342 bytes versus 76,803,119 bytes for texconv (+6.9%).
Higher compression levels reduce size but increase time. Eight calls were a
useful balance on the test host; 16 did not give a stable improvement.

Blender 5.2.2 live validation of representative BC7 sRGB/linear, BC6H and raw
RGBA textures: full float buffers identical (max difference zero), colorspace
and alpha retained on relink, temporary image datablocks removed. Existing
project object/image counts were preserved. The addon's 49 PNG tests passed,
including Unicode paths, row padding, missing DLL fallback, invalid/truncated
DDS, failed partial output cleanup and atomic destination preservation.

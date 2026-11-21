set(deflate_root "${CMAKE_CURRENT_SOURCE_DIR}/vendor/libdeflate")
add_library(herta_dds_png_windows_x64 SHARED
    herta_dds_png.c ../dds_decode.c
    ${deflate_root}/lib/deflate_compress.c
    ${deflate_root}/lib/zlib_compress.c
    ${deflate_root}/lib/adler32.c
    ${deflate_root}/lib/crc32.c
    ${deflate_root}/lib/utils.c
    ${deflate_root}/lib/x86/cpu_features.c)
target_include_directories(herta_dds_png_windows_x64 PRIVATE ${deflate_root})
target_compile_definitions(herta_dds_png_windows_x64 PRIVATE
    HERTA_PNG_STANDARD_ENDPOINTS LIBDEFLATE_STATIC _CRT_SECURE_NO_WARNINGS)
target_compile_features(herta_dds_png_windows_x64 PRIVATE c_std_11)
target_compile_options(herta_dds_png_windows_x64 PRIVATE /O2 /fp:precise /utf-8)
set_property(TARGET herta_dds_png_windows_x64 PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")

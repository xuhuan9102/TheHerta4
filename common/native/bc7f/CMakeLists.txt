cmake_minimum_required(VERSION 3.20)
project(herta_texture_encoder LANGUAGES CXX)
if(NOT WIN32 OR NOT MSVC)
  message(FATAL_ERROR "The prebuilt encoder currently targets MSVC Windows x64")
endif()
set(
  BASIS_UNIVERSAL_ROOT
  "${CMAKE_CURRENT_SOURCE_DIR}/vendor/basis_universal"
  CACHE PATH
  "Basis Universal at ee771a7f913a3233a252eb6d281b4446f6a3c9f5"
)
if(NOT EXISTS "${BASIS_UNIVERSAL_ROOT}/transcoder/basisu_transcoder.cpp")
  message(FATAL_ERROR "Set BASIS_UNIVERSAL_ROOT to the pinned source checkout")
endif()
add_library(herta_texture_windows_x64 SHARED pipeline.cpp bc7f_bridge.cpp
  "${BASIS_UNIVERSAL_ROOT}/transcoder/basisu_transcoder.cpp")
target_include_directories(herta_texture_windows_x64 PRIVATE "${BASIS_UNIVERSAL_ROOT}/transcoder")
target_compile_features(herta_texture_windows_x64 PRIVATE cxx_std_17)
target_compile_definitions(herta_texture_windows_x64 PRIVATE BASISD_SUPPORT_KTX2_ZSTD=0 _CRT_SECURE_NO_WARNINGS)
# The pinned Basis header contains one CP1252 en dash in a comment.
target_compile_options(
  herta_texture_windows_x64
  PRIVATE /O2 /EHsc /source-charset:.1252 /execution-charset:.utf-8
)
set_property(TARGET herta_texture_windows_x64 PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")
# Keep the x64 SSE2 baseline; do not reintroduce /arch:AVX2 here.
add_library(herta_png_analysis_windows_x64 SHARED png_analysis.cpp)
target_compile_features(herta_png_analysis_windows_x64 PRIVATE cxx_std_17)
target_compile_definitions(herta_png_analysis_windows_x64 PRIVATE _CRT_SECURE_NO_WARNINGS)
target_compile_options(herta_png_analysis_windows_x64 PRIVATE /O2 /utf-8 /EHsc)
set_property(TARGET herta_png_analysis_windows_x64 PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")

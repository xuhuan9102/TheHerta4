cmake_minimum_required(VERSION 3.20)
project(herta_addon_native LANGUAGES C CXX)

if(NOT WIN32 OR NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
  message(FATAL_ERROR "The shipped native DLLs target Windows x64 MSVC")
endif()

add_library(herta_dds_windows_x64 SHARED dds_decode.c)
target_compile_definitions(herta_dds_windows_x64 PRIVATE HERTA_FREESTANDING_WINDOWS)

add_library(herta_geometry_windows_x64 SHARED geometry_pack.c)

foreach(target herta_dds_windows_x64 herta_geometry_windows_x64)
  target_compile_options(${target} PRIVATE /O2 /Zl /GS-)
  target_link_options(${target} PRIVATE /NODEFAULTLIB /NOENTRY)
endforeach()

add_subdirectory(dds_png)
add_subdirectory(export_geometry)
add_subdirectory(shapekey)

option(
  HERTA_BUILD_BC7_ENCODER
  "Build the BC7 encoder and PNG analysis DLLs; requires pinned Basis Universal sources"
  OFF
)
if(HERTA_BUILD_BC7_ENCODER)
  add_subdirectory(bc7f)
endif()

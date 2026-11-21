cmake_minimum_required(VERSION 3.20)
project(herta_shapekey LANGUAGES CXX)

if(NOT WIN32 OR NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
  message(FATAL_ERROR "The shipped ShapeKey library targets Windows x64 MSVC")
endif()

add_library(herta_shapekey_windows_x64 SHARED shapekey_sample.cpp)
target_compile_features(herta_shapekey_windows_x64 PRIVATE cxx_std_20)
target_compile_options(
  herta_shapekey_windows_x64
  PRIVATE /O2 /EHsc /fp:precise /utf-8
)
set_property(
  TARGET herta_shapekey_windows_x64
  PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreadedDLL"
)

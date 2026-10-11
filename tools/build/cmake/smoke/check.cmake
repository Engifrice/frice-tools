# Smoke test for the shipped cmake, run in script mode:
#     cmake -DSTAGE=<tool directory> -P check.cmake
# Fails unless cmake found its modules inside the tool directory itself.

cmake_minimum_required(VERSION 4.0)

include(CMakePrintHelpers)

file(REAL_PATH "${STAGE}" stage)
file(REAL_PATH "${CMAKE_ROOT}" root)
cmake_path(IS_PREFIX stage "${root}" NORMALIZE inside)
if(NOT inside)
  message(FATAL_ERROR "CMAKE_ROOT ${root} is not inside ${stage}")
endif()

message("frice-cmake-ok ${CMAKE_VERSION}")

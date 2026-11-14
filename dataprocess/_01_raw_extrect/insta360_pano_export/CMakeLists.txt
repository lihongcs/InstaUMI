cmake_minimum_required(VERSION 3.16)
project(instaumi_pano_export LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(INSTA360_MEDIASDK_ROOT "" CACHE PATH "Root directory of the extracted/installed Insta360 MediaSDK")

set(_sdk_include_hints
  "${INSTA360_MEDIASDK_ROOT}/include"
  "${INSTA360_MEDIASDK_ROOT}/MediaSDK/include"
  "/usr/include"
  "/usr/local/include"
)
set(_sdk_lib_hints
  "${INSTA360_MEDIASDK_ROOT}/lib"
  "${INSTA360_MEDIASDK_ROOT}/lib64"
  "${INSTA360_MEDIASDK_ROOT}/MediaSDK/lib"
  "${INSTA360_MEDIASDK_ROOT}/MediaSDK/lib64"
  "/usr/lib"
  "/usr/lib/x86_64-linux-gnu"
  "/usr/local/lib"
)

find_path(INSTA360_MEDIASDK_INCLUDE_DIR
  NAMES
    ins_stitcher.h
    ins_common.h
    ins_video_stitcher.h
    ins_video_stitcher.hpp
    video_stitcher.h
    ins_media_sdk.h
  PATHS ${_sdk_include_hints}
)

find_library(INSTA360_MEDIASDK_LIBRARY
  NAMES
    MediaSDK
    libMediaSDK
    mediasdk
    ins_media_sdk
  PATHS ${_sdk_lib_hints}
)

if(NOT INSTA360_MEDIASDK_INCLUDE_DIR)
  message(FATAL_ERROR "Cannot find Insta360 MediaSDK headers. Set -DINSTA360_MEDIASDK_ROOT=/path/to/MediaSDK or install libMediaSDK-dev.")
endif()

if(NOT INSTA360_MEDIASDK_LIBRARY)
  message(FATAL_ERROR "Cannot find libMediaSDK. Set -DINSTA360_MEDIASDK_ROOT=/path/to/MediaSDK or install libMediaSDK-dev.")
endif()

add_executable(insv_to_pano src/insv_to_pano.cpp)
target_include_directories(insv_to_pano PRIVATE "${INSTA360_MEDIASDK_INCLUDE_DIR}")
target_link_libraries(insv_to_pano PRIVATE "${INSTA360_MEDIASDK_LIBRARY}" pthread dl)

add_executable(insv_to_png_sequence src/insv_to_png_sequence.cpp)
target_include_directories(insv_to_png_sequence PRIVATE "${INSTA360_MEDIASDK_INCLUDE_DIR}")
target_link_libraries(insv_to_png_sequence PRIVATE "${INSTA360_MEDIASDK_LIBRARY}" pthread dl)

get_filename_component(_sdk_lib_dir "${INSTA360_MEDIASDK_LIBRARY}" DIRECTORY)
set_target_properties(
  insv_to_pano
  insv_to_png_sequence
  PROPERTIES
    BUILD_RPATH "${_sdk_lib_dir}"
    INSTALL_RPATH "${_sdk_lib_dir}"
)

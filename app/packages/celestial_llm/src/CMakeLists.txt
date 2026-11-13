# Celestial のスマホ内AI(llama.cpp)のビルド。
#
# スマホの CPU によって使える高速な命令が違うので、arm64 では3通りに作り分け、実行時に Dart 側が選ぶ:
#   libcelestial_llm.so         ARMv8.0(どの端末でも動く)
#   libcelestial_llm_dotprod.so ARMv8.2 + dotprod + fp16(2018年ごろ以降の多くの端末)
#   libcelestial_llm_i8mm.so    ARMv8.6 + i8mm(Pixel 8 などの新しい端末。行列計算がさらに速い)
# llama.cpp は版ごとに ExternalProject で静的ライブラリとして作り、小さなラッパーに静的リンクする。
cmake_minimum_required(VERSION 3.18)
project(celestial_llm_library VERSION 0.2.0 LANGUAGES C CXX)
include(ExternalProject)

set(LLAMA_DIR ${CMAKE_CURRENT_SOURCE_DIR}/llama.cpp)
set(LLAMA_ARGS
  -DCMAKE_BUILD_TYPE=Release
  -DBUILD_SHARED_LIBS=OFF
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON
  -DGGML_NATIVE=OFF
  -DGGML_OPENMP=OFF
  -DGGML_BACKEND_DL=OFF
  -DGGML_CPU_ALL_VARIANTS=OFF
  -DLLAMA_BUILD_COMMON=OFF
  -DLLAMA_BUILD_TESTS=OFF
  -DLLAMA_BUILD_TOOLS=OFF
  -DLLAMA_BUILD_EXAMPLES=OFF
  -DLLAMA_BUILD_SERVER=OFF
  -DLLAMA_BUILD_APP=OFF
  -DLLAMA_OPENSSL=OFF
  -DLLAMA_ALL_WARNINGS=OFF)
if (ANDROID)
  list(APPEND LLAMA_ARGS
    -DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}
    -DANDROID_ABI=${ANDROID_ABI}
    -DANDROID_PLATFORM=${ANDROID_PLATFORM}
    -DANDROID_NDK=${ANDROID_NDK}
    -DANDROID_STL=c++_static)
endif()
if (CMAKE_MAKE_PROGRAM)
  list(APPEND LLAMA_ARGS -DCMAKE_MAKE_PROGRAM=${CMAKE_MAKE_PROGRAM})
endif()

function(add_llm_variant SUFFIX ARCH)
  set(bin ${CMAKE_CURRENT_BINARY_DIR}/llama${SUFFIX})
  set(args ${LLAMA_ARGS})
  if (ARCH)
    list(APPEND args -DGGML_CPU_ARM_ARCH=${ARCH})
  endif()
  set(libs
    ${bin}/src/libllama.a
    ${bin}/ggml/src/libggml.a
    ${bin}/ggml/src/libggml-cpu.a
    ${bin}/ggml/src/libggml-base.a)
  ExternalProject_Add(llama_build${SUFFIX}
    SOURCE_DIR ${LLAMA_DIR}
    BINARY_DIR ${bin}
    CMAKE_ARGS ${args}
    BUILD_COMMAND ${CMAKE_COMMAND} --build ${bin} --config Release --target llama
    INSTALL_COMMAND ""
    BUILD_BYPRODUCTS ${libs})

  set(target celestial_llm${SUFFIX})
  add_library(${target} SHARED celestial_llm.cpp)
  add_dependencies(${target} llama_build${SUFFIX})
  target_include_directories(${target} PRIVATE ${LLAMA_DIR}/include ${LLAMA_DIR}/ggml/include)
  target_compile_features(${target} PRIVATE cxx_std_17)
  target_compile_options(${target} PRIVATE -O2 -fvisibility=hidden)
  target_link_libraries(${target} PRIVATE -Wl,--start-group ${libs} -Wl,--end-group)
  if (ANDROID)
    target_link_libraries(${target} PRIVATE log m dl)
    # Android 15 以降の 16KB ページに対応し、使わない記号を落として小さくする
    target_link_options(${target} PRIVATE "-Wl,-z,max-page-size=16384" "-Wl,--gc-sections" "-Wl,--exclude-libs,ALL" "-static-libstdc++")
  else()
    find_package(Threads REQUIRED)
    target_link_libraries(${target} PRIVATE Threads::Threads)
  endif()
endfunction()

if (ANDROID AND ANDROID_ABI STREQUAL "arm64-v8a")
  add_llm_variant("" "armv8-a")
  add_llm_variant("_dotprod" "armv8.2-a+dotprod+fp16")
  add_llm_variant("_i8mm" "armv8.6-a+dotprod+fp16+i8mm")
else()
  add_llm_variant("" "")
endif()

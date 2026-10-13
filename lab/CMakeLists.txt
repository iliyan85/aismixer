cmake_minimum_required(VERSION 3.24)

# B1 deliberately uses the native Windows VS2022 toolchain. Select the CUDA
# toolset with cmake -T; changing CMAKE_GENERATOR_TOOLSET here is unsupported.
if(NOT CMAKE_HOST_WIN32 OR NOT CMAKE_GENERATOR STREQUAL "Visual Studio 17 2022")
    message(FATAL_ERROR "B1 requires native Windows and -G \"Visual Studio 17 2022\" -A x64.")
endif()
if(NOT CMAKE_GENERATOR_PLATFORM STREQUAL "x64")
    message(FATAL_ERROR "B1 requires the x64 platform. Configure with -A x64.")
endif()

if(NOT DEFINED CMAKE_CUDA_ARCHITECTURES)
    set(CMAKE_CUDA_ARCHITECTURES 61 CACHE STRING "CUDA architectures (GTX 1050 Ti: 61)")
endif()

# Pin toolkit discovery to 12.9 instead of CUDA_PATH or the newest installed
# version. A newer driver reporting CUDA 13.0 does not require toolkit 13.x.
set(_lab_cuda_root "$ENV{CUDA_PATH_V12_9}")
if(NOT _lab_cuda_root)
    set(_lab_cuda_root "$ENV{ProgramFiles}/NVIDIA GPU Computing Toolkit/CUDA/v12.9")
endif()
if(CMAKE_GENERATOR_TOOLSET MATCHES "(^|,)cuda=([^,]+)")
    set(_lab_cuda_selection "${CMAKE_MATCH_2}")
    if(_lab_cuda_selection MATCHES "^[0-9]+([.][0-9]+)*$")
        if(NOT _lab_cuda_selection STREQUAL "12.9")
            message(FATAL_ERROR "B1 requires CUDA 12.9; requested toolset is ${_lab_cuda_selection}. CUDA 13.x cannot compile Pascal. Use -T cuda=12.9 or -T \"cuda=<CUDA 12.9 root>\" in a fresh build tree.")
        endif()
    else()
        set(_lab_cuda_root "${_lab_cuda_selection}")
    endif()
endif()
if(NOT CUDAToolkit_ROOT)
    file(TO_CMAKE_PATH "${_lab_cuda_root}" _lab_cuda_root)
    set(CUDAToolkit_ROOT "${_lab_cuda_root}" CACHE PATH "CUDA 12.9 toolkit root")
endif()

function(lab_require_cuda_12_9 compiler)
    if(NOT EXISTS "${compiler}")
        message(FATAL_ERROR "CUDA 12.9 nvcc was not found at ${compiler}. Check nvcc --version and set -DCUDAToolkit_ROOT=<CUDA 12.9 root>. Do not substitute CUDA 13.x.")
    endif()
    execute_process(COMMAND "${compiler}" --version
        RESULT_VARIABLE _nvcc_result OUTPUT_VARIABLE _nvcc_version
        ERROR_VARIABLE _nvcc_error)
    if(NOT _nvcc_result EQUAL 0 OR NOT _nvcc_version MATCHES "release 12[.]9[, ]")
        message(FATAL_ERROR "B1 requires an operational CUDA 12.9 compiler at ${compiler}. CUDA 13.x is unsupported for Pascal. nvcc output: ${_nvcc_version}${_nvcc_error}")
    endif()
endfunction()

# Check supplied/cached compilers before CUDA compiler identification can run.
lab_require_cuda_12_9("${CUDAToolkit_ROOT}/bin/nvcc.exe")
if(CMAKE_CUDA_COMPILER)
    lab_require_cuda_12_9("${CMAKE_CUDA_COMPILER}")
endif()

project(aismixer_lab LANGUAGES CXX)

# CXX initialization reveals the VS-selected toolkit without compiling CUDA.
# FindCUDAToolkit alone cannot choose a Visual Studio CUDA toolset.
if(NOT CMAKE_VS_PLATFORM_TOOLSET_CUDA STREQUAL "12.9")
    message(FATAL_ERROR "Visual Studio selected CUDA toolset '${CMAKE_VS_PLATFORM_TOOLSET_CUDA}', but B1 requires 12.9. If CUDA 12.9 VS integration is missing, use a fresh build tree with -T \"cuda=<CUDA 12.9 root>\"; that root must contain bin/nvcc.exe and extras/visual_studio_integration/MSBuildExtensions. Otherwise use -T cuda=12.9 with installed integration. No CUDA compilation was attempted.")
endif()
if(CMAKE_VS_PLATFORM_TOOLSET_CUDA_CUSTOM_DIR)
    foreach(_file "CUDA 12.9.props" "CUDA 12.9.targets" "Nvda.Build.CudaTasks.v12.9.dll")
        if(NOT EXISTS "${CMAKE_VS_PLATFORM_TOOLSET_CUDA_CUSTOM_DIR}/extras/visual_studio_integration/MSBuildExtensions/${_file}")
            message(FATAL_ERROR "CUDA 12.9 VS integration is incomplete: missing ${_file} under the selected toolkit's extras/visual_studio_integration/MSBuildExtensions. Diagnose or repair integration manually; B1 does not install software.")
        endif()
    endforeach()
endif()
if(NOT MSVC)
    message(FATAL_ERROR "B1 requires the Visual Studio 2022 MSVC host compiler.")
endif()

# Keep nvcc's normal host-compiler compatibility checks enabled.
enable_language(CUDA)
if(NOT CMAKE_CUDA_COMPILER_VERSION MATCHES "^12[.]9([.]|$)")
    message(FATAL_ERROR "Detected CUDA compiler ${CMAKE_CUDA_COMPILER_VERSION}; B1 requires 12.9.")
endif()
find_package(CUDAToolkit 12.9 REQUIRED)
if(NOT CUDAToolkit_VERSION MATCHES "^12[.]9([.]|$)")
    message(FATAL_ERROR "Detected CUDA runtime toolkit ${CUDAToolkit_VERSION}; B1 requires 12.9.")
endif()

add_library(aismixer_lab_gpu_engine STATIC cuda/engine.cu)
target_include_directories(aismixer_lab_gpu_engine PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/cuda")
target_link_libraries(aismixer_lab_gpu_engine PUBLIC CUDA::cudart)
# Philox uses cuRAND's device header API; no host cuRAND library is needed.
set_target_properties(aismixer_lab_gpu_engine PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO
    CUDA_STANDARD 17 CUDA_STANDARD_REQUIRED YES CUDA_EXTENSIONS NO
    CUDA_RUNTIME_LIBRARY Shared)

add_executable(aismixer_lab_gpu_smoke cuda/smoke_main.cpp)
target_link_libraries(aismixer_lab_gpu_smoke PRIVATE aismixer_lab_gpu_engine)
set_target_properties(aismixer_lab_gpu_smoke PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)

add_library(aismixer_lab_scene_engine STATIC cuda/scene_engine.cu)
target_include_directories(aismixer_lab_scene_engine PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/cuda")
target_link_libraries(aismixer_lab_scene_engine PUBLIC CUDA::cudart)
set_target_properties(aismixer_lab_scene_engine PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO
    CUDA_STANDARD 17 CUDA_STANDARD_REQUIRED YES CUDA_EXTENSIONS NO
    CUDA_RUNTIME_LIBRARY Shared)

add_executable(aismixer_lab_scene_smoke cuda/scene_smoke_main.cpp)
target_link_libraries(aismixer_lab_scene_smoke PRIVATE aismixer_lab_scene_engine)
set_target_properties(aismixer_lab_scene_smoke PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)

option(AISMIXER_LAB_BUILD_SCENE_RUNNER "Build the native YAML scene runner (requires local yaml-cpp >= 0.8)" OFF)

# These helpers use CUDA-free B2 types; validation/output need neither CUDA nor
# yaml-cpp. The YAML dependency is linked only into the optional operator app.
add_library(aismixer_lab_scene_host STATIC
    runner/scene_validation.cpp runner/scene_output.cpp)
target_include_directories(aismixer_lab_scene_host PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/runner" "${CMAKE_CURRENT_SOURCE_DIR}/cuda")
set_target_properties(aismixer_lab_scene_host PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
target_compile_options(aismixer_lab_scene_host PRIVATE /utf-8 /W4)

if(AISMIXER_LAB_BUILD_SCENE_RUNNER)
    find_package(yaml-cpp 0.8 CONFIG QUIET)
    if(NOT yaml-cpp_FOUND)
        message(FATAL_ERROR "AISMIXER_LAB_BUILD_SCENE_RUNNER=ON requires a locally provisioned yaml-cpp >= 0.8 CMake package. Set -Dyaml-cpp_DIR=<prefix>/lib/cmake/yaml-cpp or -DCMAKE_PREFIX_PATH=<prefix> (or an existing vcpkg toolchain). See lab/README.md. No dependency is downloaded. Set this option OFF to retain B1/B2 builds and tests.")
    endif()
    if(TARGET yaml-cpp::yaml-cpp)
        set(_lab_yaml_target yaml-cpp::yaml-cpp)
    elseif(TARGET yaml-cpp)
        set(_lab_yaml_target yaml-cpp)
    else()
        message(FATAL_ERROR "The local yaml-cpp package did not export yaml-cpp::yaml-cpp or yaml-cpp. Supply a compatible yaml-cpp >= 0.8 package; B1/B2 remain available with the runner option OFF.")
    endif()
    add_executable(aismixer_lab_scene runner/scene_cli_main.cpp runner/scene_yaml.cpp)
    target_link_libraries(aismixer_lab_scene PRIVATE
        aismixer_lab_scene_host aismixer_lab_scene_engine ${_lab_yaml_target})
    set_target_properties(aismixer_lab_scene PROPERTIES
        CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
    target_compile_options(aismixer_lab_scene PRIVATE /utf-8 /W4)
    message(STATUS "B2.1 runner enabled with local yaml-cpp target ${_lab_yaml_target}")
else()
    message(STATUS "B2.1 runner disabled (AISMIXER_LAB_BUILD_SCENE_RUNNER=OFF); enable explicitly with a local yaml-cpp package")
endif()

include(CTest)
if(BUILD_TESTING)
    add_test(NAME aismixer_lab_gpu_self_test COMMAND aismixer_lab_gpu_smoke --self-test)
    set_tests_properties(aismixer_lab_gpu_self_test PROPERTIES LABELS GPU TIMEOUT 120)
    # Launch-shape control exists only for the B2 validation build, not its API.
    target_compile_definitions(aismixer_lab_scene_engine PRIVATE AISMIXER_LAB_SCENE_TESTING)
    target_compile_definitions(aismixer_lab_scene_smoke PRIVATE AISMIXER_LAB_SCENE_TESTING)
    add_test(NAME aismixer_lab_scene_self_test COMMAND aismixer_lab_scene_smoke --self-test)
    set_tests_properties(aismixer_lab_scene_self_test PROPERTIES LABELS GPU TIMEOUT 120)

    add_executable(aismixer_lab_scene_host_tests tests/scene_host_tests.cpp)
    target_link_libraries(aismixer_lab_scene_host_tests PRIVATE aismixer_lab_scene_host)
    set_target_properties(aismixer_lab_scene_host_tests PROPERTIES
        CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
    target_compile_options(aismixer_lab_scene_host_tests PRIVATE /utf-8 /W4)
    add_test(NAME aismixer_lab_scene_host_test COMMAND aismixer_lab_scene_host_tests
        "${CMAKE_CURRENT_BINARY_DIR}/host-tests")
    set_tests_properties(aismixer_lab_scene_host_test PROPERTIES LABELS host TIMEOUT 30)

    if(AISMIXER_LAB_BUILD_SCENE_RUNNER)
        find_package(Python3 COMPONENTS Interpreter QUIET)
        if(Python3_Interpreter_FOUND)
            foreach(_mode host gpu interactive)
                add_test(NAME aismixer_lab_scene_cli_${_mode}_test
                    COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/scene_cli_tests.py"
                    --runner $<TARGET_FILE:aismixer_lab_scene>
                    --source-dir "${CMAKE_CURRENT_SOURCE_DIR}"
                    --work-dir "${CMAKE_CURRENT_BINARY_DIR}/cli-tests" --mode ${_mode})
                set_tests_properties(aismixer_lab_scene_cli_${_mode}_test PROPERTIES
                    WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/.." TIMEOUT 120)
            endforeach()
            set_tests_properties(aismixer_lab_scene_cli_host_test PROPERTIES LABELS host)
            set_tests_properties(aismixer_lab_scene_cli_gpu_test
                aismixer_lab_scene_cli_interactive_test PROPERTIES LABELS GPU)
        else()
            message(WARNING "B2.1 CLI acceptance tests are NOT configured: a local Python 3 interpreter is required for the test harness. The native runner itself does not depend on Python.")
        endif()
    endif()
endif()

message(STATUS "B1 toolchain: CUDA ${CMAKE_CUDA_COMPILER_VERSION}, MSVC ${CMAKE_CXX_COMPILER_VERSION}, architectures ${CMAKE_CUDA_ARCHITECTURES}")

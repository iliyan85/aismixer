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
        message(FATAL_ERROR "AISMIXER_LAB_BUILD_SCENE_RUNNER=ON requires a locally provisioned yaml-cpp >= 0.8 CMake package. Set -Dyaml-cpp_DIR=<prefix>/lib/cmake/yaml-cpp or -DCMAKE_PREFIX_PATH=<prefix> (or an existing vcpkg toolchain). See README.md (repository root). No dependency is downloaded. Set this option OFF to retain B1/B2 builds and tests.")
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

add_library(aismixer_lab_ais_codec STATIC
    ais/ais_bits.cpp ais/ais_encoder.cpp ais/nmea0183.cpp)
target_include_directories(aismixer_lab_ais_codec PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/ais")
set_target_properties(aismixer_lab_ais_codec PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
target_compile_options(aismixer_lab_ais_codec PRIVATE /utf-8 /W4)

add_executable(aismixer_lab_ais_smoke ais/ais_smoke_main.cpp)
target_link_libraries(aismixer_lab_ais_smoke PRIVATE aismixer_lab_ais_codec)
set_target_properties(aismixer_lab_ais_smoke PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
target_compile_options(aismixer_lab_ais_smoke PRIVATE /utf-8 /W4)

# B3.1 retains B2 unchanged; only the new geography engine uses device select.
add_library(aismixer_lab_geo_host STATIC geo/geo_host.cpp)
target_include_directories(aismixer_lab_geo_host PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/geo" "${CMAKE_CURRENT_SOURCE_DIR}/cuda")
target_link_libraries(aismixer_lab_geo_host PRIVATE bcrypt)
set_target_properties(aismixer_lab_geo_host PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
target_compile_options(aismixer_lab_geo_host PRIVATE /utf-8 /W4)

add_library(aismixer_lab_geo_engine STATIC geo/geo_engine.cu)
target_link_libraries(aismixer_lab_geo_engine PUBLIC aismixer_lab_geo_host CUDA::cudart)
set_target_properties(aismixer_lab_geo_engine PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO
    CUDA_STANDARD 17 CUDA_STANDARD_REQUIRED YES CUDA_EXTENSIONS NO
    CUDA_RUNTIME_LIBRARY Shared)
target_compile_options(aismixer_lab_geo_engine PRIVATE
    $<$<COMPILE_LANGUAGE:CUDA>:--fmad=false>)

add_library(aismixer_lab_geo_ais STATIC geo/geo_ais.cpp)
target_link_libraries(aismixer_lab_geo_ais PUBLIC aismixer_lab_geo_host aismixer_lab_ais_codec)
set_target_properties(aismixer_lab_geo_ais PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
target_compile_options(aismixer_lab_geo_ais PRIVATE /utf-8 /W4)

add_library(aismixer_lab_geo_output STATIC geo/geo_output.cpp)
target_link_libraries(aismixer_lab_geo_output PUBLIC aismixer_lab_geo_ais)
set_target_properties(aismixer_lab_geo_output PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
target_compile_options(aismixer_lab_geo_output PRIVATE /utf-8 /W4)

option(AISMIXER_LAB_BUILD_GEO_RUNNER "Build B3.1 offline geographic YAML operator (requires local yaml-cpp >= 0.8)" OFF)
if(AISMIXER_LAB_BUILD_GEO_RUNNER)
    if(NOT _lab_yaml_target)
        find_package(yaml-cpp 0.8 CONFIG QUIET)
        if(TARGET yaml-cpp::yaml-cpp)
            set(_lab_yaml_target yaml-cpp::yaml-cpp)
        elseif(TARGET yaml-cpp)
            set(_lab_yaml_target yaml-cpp)
        else()
            message(FATAL_ERROR "B3.1 geographic operator requires locally provisioned yaml-cpp >= 0.8. See README.md (repository root). No dependency is downloaded.")
        endif()
    endif()
    add_library(aismixer_lab_geo_config STATIC geo/geo_config.cpp)
    target_link_libraries(aismixer_lab_geo_config PUBLIC aismixer_lab_geo_host PRIVATE ${_lab_yaml_target})
    set_target_properties(aismixer_lab_geo_config PROPERTIES
        CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
    target_compile_options(aismixer_lab_geo_config PRIVATE /utf-8 /W4)
    add_executable(aismixer_lab_geo geo/geo_cli_main.cpp runner/scene_yaml.cpp)
    target_link_libraries(aismixer_lab_geo PRIVATE aismixer_lab_geo_config
        aismixer_lab_geo_engine aismixer_lab_geo_output aismixer_lab_scene_host ${_lab_yaml_target})
    set_target_properties(aismixer_lab_geo PROPERTIES
        CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
    target_compile_options(aismixer_lab_geo PRIVATE /utf-8 /W4)
    string(REPLACE ";" "," _lab_geo_architectures "${CMAKE_CUDA_ARCHITECTURES}")
    target_compile_definitions(aismixer_lab_geo PRIVATE
        AISMIXER_LAB_GEO_BUILD_INFO="MSVC_${CMAKE_CXX_COMPILER_VERSION}_CUDA_${CMAKE_CUDA_COMPILER_VERSION}_arch_${_lab_geo_architectures}")
endif()

# R0 is additive: the host runtime validates finalized NMEA bytes without an
# encoder or CUDA/YAML backend. The synthetic device bridge is test-only.
add_library(aismixer_lab_runtime STATIC
    runtime/time.cpp runtime/event.cpp runtime/scheduler.cpp runtime/fixture.cpp
    runtime/journal.cpp runtime/output.cpp runtime/playback.cpp runtime/rf_contract.cpp
    runtime/streaming_fixture.cpp)
target_include_directories(aismixer_lab_runtime PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/runtime")
set_target_properties(aismixer_lab_runtime PROPERTIES
    CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
target_compile_options(aismixer_lab_runtime PRIVATE /utf-8 /W4)
option(AISMIXER_LAB_BUILD_RUNTIME_RUNNER "Build the R0 synthetic fixture/recorded playback operator (no RF or networking)" OFF)
if(AISMIXER_LAB_BUILD_RUNTIME_RUNNER)
    add_executable(aismixer_lab_runtime_runner runtime/cli_main.cpp)
    target_link_libraries(aismixer_lab_runtime_runner PRIVATE aismixer_lab_runtime)
    set_target_properties(aismixer_lab_runtime_runner PROPERTIES
        CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
    target_compile_options(aismixer_lab_runtime_runner PRIVATE /utf-8 /W4)
endif()

option(AISMIXER_LAB_BUILD_WORLD "Build the W1 world/lifecycle CUDA operator (requires local yaml-cpp >= 0.8)" OFF)
if(AISMIXER_LAB_BUILD_WORLD)
    if(NOT _lab_yaml_target)
        find_package(yaml-cpp 0.8 CONFIG QUIET)
        if(TARGET yaml-cpp::yaml-cpp)
            set(_lab_yaml_target yaml-cpp::yaml-cpp)
        elseif(TARGET yaml-cpp)
            set(_lab_yaml_target yaml-cpp)
        else()
            message(FATAL_ERROR "W1 requires locally provisioned yaml-cpp >= 0.8. See README.md (repository root). No dependency is downloaded.")
        endif()
    endif()
    add_library(aismixer_lab_world_host STATIC world/world.cpp)
    target_include_directories(aismixer_lab_world_host PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/world")
    target_link_libraries(aismixer_lab_world_host PUBLIC aismixer_lab_runtime PRIVATE bcrypt)
    add_library(aismixer_lab_world_config STATIC world/config.cpp)
    target_link_libraries(aismixer_lab_world_config PUBLIC aismixer_lab_world_host PRIVATE ${_lab_yaml_target})
    add_library(aismixer_lab_world_engine STATIC world/cuda_engine.cu world/world_session.cpp)
    target_link_libraries(aismixer_lab_world_engine PUBLIC aismixer_lab_world_host CUDA::cudart)
    set_target_properties(aismixer_lab_world_engine PROPERTIES
        CUDA_STANDARD 17 CUDA_STANDARD_REQUIRED YES CUDA_EXTENSIONS NO CUDA_RUNTIME_LIBRARY Shared)
    target_compile_options(aismixer_lab_world_engine PRIVATE
        $<$<COMPILE_LANGUAGE:CUDA>:--fmad=false>)
    set_property(SOURCE world/cuda_engine.cu PROPERTY VS_SETTINGS "Warning=W4")
    add_executable(aismixer_lab_world world/cli_main.cpp)
    target_link_libraries(aismixer_lab_world PRIVATE aismixer_lab_world_config aismixer_lab_world_engine)
    foreach(_w1_target aismixer_lab_world_host aismixer_lab_world_config aismixer_lab_world_engine aismixer_lab_world)
        set_target_properties(${_w1_target} PROPERTIES CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
        target_compile_options(${_w1_target} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:/utf-8> $<$<COMPILE_LANGUAGE:CXX>:/W4>)
    endforeach()
    target_compile_options(aismixer_lab_world_engine PRIVATE $<$<COMPILE_LANGUAGE:CUDA>:-Xcompiler=/utf-8>)
    target_compile_definitions(aismixer_lab_world PRIVATE
        AISMIXER_LAB_WORLD_BUILD_INFO="MSVC_${CMAKE_CXX_COMPILER_VERSION}_CUDA_${CMAKE_CUDA_COMPILER_VERSION}_sm61_fmad_false")
endif()

include(CTest)
if(BUILD_TESTING)
    target_compile_definitions(aismixer_lab_runtime PRIVATE AISMIXER_LAB_RUNTIME_JOURNAL_TESTING)
    foreach(_runtime_test core journal playback rf_contract journal_v2 rf_boundary streaming contract)
        add_executable(aismixer_lab_runtime_${_runtime_test}_tests tests/runtime_${_runtime_test}_tests.cpp)
        target_link_libraries(aismixer_lab_runtime_${_runtime_test}_tests PRIVATE aismixer_lab_runtime)
        set_target_properties(aismixer_lab_runtime_${_runtime_test}_tests PROPERTIES
            CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
        target_compile_options(aismixer_lab_runtime_${_runtime_test}_tests PRIVATE /utf-8 /W4)
        add_test(NAME aismixer_lab_runtime_${_runtime_test}_test
            COMMAND aismixer_lab_runtime_${_runtime_test}_tests "${CMAKE_CURRENT_BINARY_DIR}/runtime-${_runtime_test}-tests")
        set_tests_properties(aismixer_lab_runtime_${_runtime_test}_test PROPERTIES
            LABELS "host;runtime" TIMEOUT 120 ENVIRONMENT "CUDA_VISIBLE_DEVICES=-1")
    endforeach()
    set_tests_properties(aismixer_lab_runtime_streaming_test PROPERTIES LABELS "host;runtime;stress" TIMEOUT 300)
    target_compile_definitions(aismixer_lab_runtime_journal_tests PRIVATE AISMIXER_LAB_RUNTIME_JOURNAL_TESTING)
    add_executable(aismixer_lab_runtime_cuda_bridge_tests tests/runtime_cuda_bridge_tests.cu)
    target_link_libraries(aismixer_lab_runtime_cuda_bridge_tests PRIVATE aismixer_lab_runtime CUDA::cudart)
    set_target_properties(aismixer_lab_runtime_cuda_bridge_tests PROPERTIES
        CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO
        CUDA_STANDARD 17 CUDA_STANDARD_REQUIRED YES CUDA_EXTENSIONS NO CUDA_RUNTIME_LIBRARY Shared)
    target_compile_options(aismixer_lab_runtime_cuda_bridge_tests PRIVATE
        $<$<COMPILE_LANGUAGE:CUDA>:-Xcompiler=/utf-8>)
    # CUDA's VS task appends its own host warning flag after AdditionalOptions.
    # Set its metadata too, so the default inherited /W1 cannot override /W4.
    set_property(SOURCE tests/runtime_cuda_bridge_tests.cu PROPERTY VS_SETTINGS "Warning=W4")
    add_test(NAME aismixer_lab_runtime_cuda_bridge_test COMMAND aismixer_lab_runtime_cuda_bridge_tests)
    set_tests_properties(aismixer_lab_runtime_cuda_bridge_test PROPERTIES LABELS "GPU;runtime" TIMEOUT 60)
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

    add_executable(aismixer_lab_ais_codec_tests tests/ais_codec_tests.cpp)
    target_link_libraries(aismixer_lab_ais_codec_tests PRIVATE aismixer_lab_ais_codec)
    set_target_properties(aismixer_lab_ais_codec_tests PROPERTIES
        CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
    target_compile_options(aismixer_lab_ais_codec_tests PRIVATE /utf-8 /W4)
    add_test(NAME aismixer_lab_ais_codec_test COMMAND aismixer_lab_ais_codec_tests)
    add_test(NAME aismixer_lab_ais_smoke_test COMMAND aismixer_lab_ais_smoke --self-test)
    set_tests_properties(aismixer_lab_ais_codec_test aismixer_lab_ais_smoke_test
        PROPERTIES LABELS "host;AIS" TIMEOUT 30 ENVIRONMENT "CUDA_VISIBLE_DEVICES=-1")
    find_package(Python3 COMPONENTS Interpreter QUIET)
    if(Python3_Interpreter_FOUND)
        if(AISMIXER_LAB_BUILD_RUNTIME_RUNNER)
            add_test(NAME aismixer_lab_runtime_cli_oracle_test
                COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/runtime_cli_oracle.py"
                --exe $<TARGET_FILE:aismixer_lab_runtime_runner>
                --work-dir "${CMAKE_CURRENT_BINARY_DIR}/runtime-cli-tests")
            set_tests_properties(aismixer_lab_runtime_cli_oracle_test PROPERTIES
                LABELS "host;runtime" TIMEOUT 120 ENVIRONMENT "CUDA_VISIBLE_DEVICES=-1")
            add_test(NAME aismixer_lab_runtime_streaming_oracle_test
                COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/runtime_streaming_oracle.py"
                --exe $<TARGET_FILE:aismixer_lab_runtime_runner>
                --work-dir "${CMAKE_CURRENT_BINARY_DIR}/runtime-streaming-cli-tests")
            set_tests_properties(aismixer_lab_runtime_streaming_oracle_test PROPERTIES
                LABELS "host;runtime;stress" TIMEOUT 300 ENVIRONMENT "CUDA_VISIBLE_DEVICES=-1")
        endif()
        add_test(NAME aismixer_lab_ais_wire_oracle_test
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/ais_wire_oracle.py"
            --exe $<TARGET_FILE:aismixer_lab_ais_smoke>)
        set_tests_properties(aismixer_lab_ais_wire_oracle_test
            PROPERTIES LABELS "host;AIS" TIMEOUT 30 ENVIRONMENT "CUDA_VISIBLE_DEVICES=-1")
    else()
        message(WARNING "AIS independent wire oracle is NOT configured: a local Python 3 interpreter is required for the standard-library-only test. The codec itself does not depend on Python.")
    endif()
    target_compile_definitions(aismixer_lab_geo_engine PRIVATE AISMIXER_LAB_GEO_TESTING)
    add_executable(aismixer_lab_geo_host_tests tests/geo_host_tests.cpp)
    target_link_libraries(aismixer_lab_geo_host_tests PRIVATE aismixer_lab_geo_ais)
    set_target_properties(aismixer_lab_geo_host_tests PROPERTIES
        CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
    target_compile_options(aismixer_lab_geo_host_tests PRIVATE /utf-8 /W4)
    add_test(NAME aismixer_lab_geo_host_test COMMAND aismixer_lab_geo_host_tests)
    set_tests_properties(aismixer_lab_geo_host_test PROPERTIES
        LABELS "host;geo" TIMEOUT 30 ENVIRONMENT "CUDA_VISIBLE_DEVICES=-1")

    target_compile_definitions(aismixer_lab_geo_output PRIVATE AISMIXER_LAB_GEO_OUTPUT_TESTING)
    add_executable(aismixer_lab_geo_output_tests tests/geo_output_tests.cpp)
    target_compile_definitions(aismixer_lab_geo_output_tests PRIVATE AISMIXER_LAB_GEO_OUTPUT_TESTING)
    target_link_libraries(aismixer_lab_geo_output_tests PRIVATE aismixer_lab_geo_output)
    set_target_properties(aismixer_lab_geo_output_tests PROPERTIES
        CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
    target_compile_options(aismixer_lab_geo_output_tests PRIVATE /utf-8 /W4)
    add_test(NAME aismixer_lab_geo_output_test COMMAND aismixer_lab_geo_output_tests
        "${CMAKE_CURRENT_BINARY_DIR}/geo-output-tests")
    set_tests_properties(aismixer_lab_geo_output_test PROPERTIES
        LABELS "host;geo;AIS" TIMEOUT 30 ENVIRONMENT "CUDA_VISIBLE_DEVICES=-1")

    add_executable(aismixer_lab_geo_gpu_tests tests/geo_gpu_tests.cpp)
    target_compile_definitions(aismixer_lab_geo_gpu_tests PRIVATE AISMIXER_LAB_GEO_TESTING)
    target_link_libraries(aismixer_lab_geo_gpu_tests PRIVATE
        aismixer_lab_geo_engine aismixer_lab_scene_engine)
    set_target_properties(aismixer_lab_geo_gpu_tests PROPERTIES
        CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
    target_compile_options(aismixer_lab_geo_gpu_tests PRIVATE /utf-8 /W4)
    add_test(NAME aismixer_lab_geo_gpu_test COMMAND aismixer_lab_geo_gpu_tests)
    set_tests_properties(aismixer_lab_geo_gpu_test PROPERTIES LABELS "GPU;geo" TIMEOUT 180)
    add_test(NAME aismixer_lab_geo_unavailable_test COMMAND aismixer_lab_geo_gpu_tests --expect-no-gpu)
    set_tests_properties(aismixer_lab_geo_unavailable_test PROPERTIES
        LABELS "host;geo" TIMEOUT 30 ENVIRONMENT "CUDA_VISIBLE_DEVICES=-1")
    if(AISMIXER_LAB_BUILD_GEO_RUNNER)
        add_executable(aismixer_lab_geo_config_tests tests/geo_config_tests.cpp)
        target_link_libraries(aismixer_lab_geo_config_tests PRIVATE aismixer_lab_geo_config)
        set_target_properties(aismixer_lab_geo_config_tests PROPERTIES
            CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
        target_compile_options(aismixer_lab_geo_config_tests PRIVATE /utf-8 /W4)
        add_test(NAME aismixer_lab_geo_config_test COMMAND aismixer_lab_geo_config_tests
            "${CMAKE_CURRENT_SOURCE_DIR}/geo/profiles" "${CMAKE_CURRENT_BINARY_DIR}/geo-config-tests")
        set_tests_properties(aismixer_lab_geo_config_test PROPERTIES
            LABELS "host;geo" TIMEOUT 30 ENVIRONMENT "CUDA_VISIBLE_DEVICES=-1")
    endif()
    if(AISMIXER_LAB_BUILD_GEO_RUNNER AND Python3_Interpreter_FOUND)
        add_test(NAME aismixer_lab_geo_wire_oracle_test
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/geo_wire_oracle.py"
            --exe $<TARGET_FILE:aismixer_lab_geo>
            --domain "${CMAKE_CURRENT_SOURCE_DIR}/geo/profiles/black_sea_demo.yaml"
            --source-dir "${CMAKE_CURRENT_SOURCE_DIR}"
            --work-dir "${CMAKE_CURRENT_BINARY_DIR}/geo-tests")
        set_tests_properties(aismixer_lab_geo_wire_oracle_test PROPERTIES
            LABELS "GPU;geo;AIS" TIMEOUT 180)
        add_test(NAME aismixer_lab_geo_cli_closure_test
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/geo_cli_closure_tests.py"
            --exe $<TARGET_FILE:aismixer_lab_geo>
            --source-dir "${CMAKE_CURRENT_SOURCE_DIR}"
            --work-dir "${CMAKE_CURRENT_BINARY_DIR}/geo-cli-tests")
        set_tests_properties(aismixer_lab_geo_cli_closure_test PROPERTIES
            LABELS "GPU;geo;AIS" TIMEOUT 180)
    endif()
endif()

if(BUILD_TESTING AND AISMIXER_LAB_BUILD_WORLD)
    foreach(_w1_test host geodesy config gpu)
        add_executable(aismixer_lab_world_${_w1_test}_tests tests/world_${_w1_test}_tests.cpp)
        if(_w1_test STREQUAL "gpu")
            target_link_libraries(aismixer_lab_world_${_w1_test}_tests PRIVATE aismixer_lab_world_engine)
        elseif(_w1_test STREQUAL "config")
            target_link_libraries(aismixer_lab_world_${_w1_test}_tests PRIVATE aismixer_lab_world_config)
        else()
            target_link_libraries(aismixer_lab_world_${_w1_test}_tests PRIVATE aismixer_lab_world_host)
        endif()
        set_target_properties(aismixer_lab_world_${_w1_test}_tests PROPERTIES CXX_STANDARD 17 CXX_STANDARD_REQUIRED YES CXX_EXTENSIONS NO)
        target_compile_options(aismixer_lab_world_${_w1_test}_tests PRIVATE /utf-8 /W4)
        if(_w1_test STREQUAL "config")
            add_test(NAME aismixer_lab_world_${_w1_test}_test COMMAND aismixer_lab_world_${_w1_test}_tests
                "${CMAKE_CURRENT_SOURCE_DIR}/world/profiles" "${CMAKE_CURRENT_BINARY_DIR}/world-config-tests")
        else()
            add_test(NAME aismixer_lab_world_${_w1_test}_test COMMAND aismixer_lab_world_${_w1_test}_tests)
        endif()
        set_tests_properties(aismixer_lab_world_${_w1_test}_test PROPERTIES TIMEOUT 180)
        if(_w1_test STREQUAL "gpu")
            set_tests_properties(aismixer_lab_world_${_w1_test}_test PROPERTIES LABELS "GPU;world")
        else()
            set_tests_properties(aismixer_lab_world_${_w1_test}_test PROPERTIES LABELS "host;world" ENVIRONMENT "CUDA_VISIBLE_DEVICES=-1")
        endif()
    endforeach()
    find_package(Python3 COMPONENTS Interpreter QUIET)
    if(Python3_Interpreter_FOUND)
        foreach(_w1_mode host gpu)
            add_test(NAME aismixer_lab_world_cli_${_w1_mode}_test
                COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/world_cli_tests.py"
                --exe $<TARGET_FILE:aismixer_lab_world> --mode ${_w1_mode}
                --source-dir "${CMAKE_CURRENT_SOURCE_DIR}"
                --work-dir "${CMAKE_CURRENT_BINARY_DIR}/world-cli-tests")
            set_tests_properties(aismixer_lab_world_cli_${_w1_mode}_test PROPERTIES TIMEOUT 300)
        endforeach()
        set_tests_properties(aismixer_lab_world_cli_host_test PROPERTIES LABELS "host;world" ENVIRONMENT "CUDA_VISIBLE_DEVICES=-1")
        set_tests_properties(aismixer_lab_world_cli_gpu_test PROPERTIES LABELS "GPU;world;stress")
    else()
        message(WARNING "W1 independent CLI acceptance is NOT configured: existing local Python3 is required; no tools are installed.")
    endif()
endif()

message(STATUS "B1 toolchain: CUDA ${CMAKE_CUDA_COMPILER_VERSION}, MSVC ${CMAKE_CXX_COMPILER_VERSION}, architectures ${CMAKE_CUDA_ARCHITECTURES}")

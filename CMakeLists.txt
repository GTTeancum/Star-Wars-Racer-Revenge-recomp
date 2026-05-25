cmake_minimum_required(VERSION 3.21)
project("RacerRevengeRecomp" LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Build PS2Recomp runtime library (brings in raylib, ps2_runtime, etc.)
add_subdirectory("tools/PS2Recomp/ps2xRuntime")

# Gather all generated recompiled function files
file(GLOB GENERATED_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/src/generated/*.cpp"
)

# Exclude files > 2MB that MSVC can't handle — compiled separately with clang-cl.
# Run build_large.bat to compile these with clang-cl, then rebuild.
set(CLANG_OBJ_DIR "${CMAKE_CURRENT_SOURCE_DIR}/src/clang_objs")

# Find all files > 2MB and exclude them from MSVC, link clang .obj if available
set(CLANG_OBJS "")
set(NEED_STUBS FALSE)
file(GLOB ALL_GEN_CPPS "${CMAKE_CURRENT_SOURCE_DIR}/src/generated/*.cpp")
foreach(src ${ALL_GEN_CPPS})
    file(SIZE ${src} fsize)
    if(fsize GREATER 2000000)
        get_filename_component(fname ${src} NAME_WLE)
        list(FILTER GENERATED_SOURCES EXCLUDE REGEX "${fname}\\.cpp")
        # sub_0031D200_0x31d200 is replaced by the master dispatcher + chunks system
        if("${fname}" STREQUAL "sub_0031D200_0x31d200")
            message(STATUS "sub_0031D200 handled via master dispatcher + chunk .obj files")
        elseif(EXISTS "${CLANG_OBJ_DIR}/${fname}.obj")
            list(APPEND CLANG_OBJS "${CLANG_OBJ_DIR}/${fname}.obj")
        else()
            set(NEED_STUBS TRUE)
            message(STATUS "Large file excluded (no .obj): ${fname}.cpp (${fsize} bytes)")
        endif()
    endif()
endforeach()

# Pick up all pre-compiled sub_0031D200 chunk .obj files (compiled with clang-cl
# from src/generated_chunks/ by build_chunks.bat)
file(GLOB D200_CHUNK_OBJS "${CLANG_OBJ_DIR}/sub_0031D200_chunk_*.obj")
if(D200_CHUNK_OBJS)
    list(APPEND CLANG_OBJS ${D200_CHUNK_OBJS})
    message(STATUS "Linking ${CMAKE_MATCH_1} sub_0031D200 chunk .obj files")
else()
    message(STATUS "sub_0031D200 chunks not yet compiled — run src/generated_chunks/build_chunks.bat")
endif()

# Master dispatcher for sub_0031D200 (small file, compiled by MSVC)
# Only include if chunk .obj files are available — the dispatcher calls chunk
# functions that don't exist until clang-cl has compiled them.  Without .obj
# files we fall back to the interpreter stub in large_function_stubs.cpp.
set(CHUNK_MASTER "${CMAKE_CURRENT_SOURCE_DIR}/src/generated_chunks/sub_0031D200_0x31d200.cpp")
if(EXISTS "${CHUNK_MASTER}" AND D200_CHUNK_OBJS)
    list(APPEND GENERATED_SOURCES "${CHUNK_MASTER}")
    # Tell large_function_stubs.cpp to skip its sub_0031D200_0x31d200
    # interpreter fallback — the master dispatcher provides it instead.
    add_compile_definitions(D200_CHUNKS_AVAILABLE)
    message(STATUS "Including sub_0031D200 master dispatcher (${list_length D200_CHUNK_OBJS} chunk .obj files)")
else()
    message(STATUS "sub_0031D200 chunks not yet compiled -- interpreter stub active (run build_chunks_parallel.py)")
endif()

# Our game executable
add_executable(racer_revenge
    src/main.cpp
    src/ps2_mips_interp.cpp
    ${GENERATED_SOURCES}
)

# Link precompiled clang .obj files
if(CLANG_OBJS)
    target_link_libraries(racer_revenge PRIVATE ${CLANG_OBJS})
endif()

# Always include stubs — covers deleted/missing large files that other functions reference
target_sources(racer_revenge PRIVATE src/large_function_stubs.cpp)

# Include paths
target_include_directories(racer_revenge PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/tools/PS2Recomp/ps2xRuntime/include
    ${CMAKE_CURRENT_SOURCE_DIR}/src/generated
    ${CMAKE_CURRENT_SOURCE_DIR}/src/generated_chunks
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

# Link the PS2 runtime (static lib) and raylib
target_link_libraries(racer_revenge PRIVATE ps2_runtime raylib)

# MSVC settings
if(MSVC)
    target_compile_options(racer_revenge PRIVATE /MP2 /bigobj /FS)
    target_link_options(racer_revenge PRIVATE "/FORCE:MULTIPLE")
    foreach(src ${GENERATED_SOURCES})
        set_source_files_properties(${src} PROPERTIES COMPILE_FLAGS "/Od")
    endforeach()
endif()

# Copy ELF next to executable for runtime loading
add_custom_command(TARGET racer_revenge POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${CMAKE_CURRENT_SOURCE_DIR}/SLUS_202.68"
        "$<TARGET_FILE_DIR:racer_revenge>/SLUS_202.68"
    COMMENT "Copying ELF to output directory"
)

@echo off
setlocal enabledelayedexpansion

REM Test that a single chunk compiles with clang-cl.
REM Bounded validation of the chunking experiment end-to-end path.

call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x64 >nul 2>&1
set PATH=C:\Program Files\LLVM\bin;%PATH%

set ROOT=C:\Programming\GitHub\Star-Wars-Racer-Revenge-recomp
set OUT=%ROOT%\src\clang_objs
set SRC=%ROOT%\src\generated_chunks

set CLANGFLAGS=/c /Od /bigobj /std:c++20 /EHsc /MD /W0
set INCLUDES=-I"%ROOT%\tools\PS2Recomp\ps2xRuntime\include" -I"%ROOT%\tools\PS2Recomp\ps2xRuntime\src\lib\Kernel" -I"%ROOT%\src\generated" -I"%ROOT%\include" -I"%ROOT%\build\_deps\raylib-src\src" -I"%ROOT%\build\_deps\raylib-src\src\external\glfw\include" -I"%SRC%"

if not exist "%OUT%" mkdir "%OUT%"

echo === Compiling master ===
clang-cl %CLANGFLAGS% %INCLUDES% -Fo"%OUT%\sub_0031D200_0x31d200.obj" "%SRC%\sub_0031D200_0x31d200.cpp" 2>&1
if errorlevel 1 (echo MASTER FAILED & exit /b 1)
echo MASTER OK

echo === Compiling chunk 0000 (smallest) ===
clang-cl %CLANGFLAGS% %INCLUDES% -Fo"%OUT%\sub_0031D200_chunk_0000.obj" "%SRC%\sub_0031D200_chunk_0000.cpp" 2>&1
if errorlevel 1 (echo CHUNK 0000 FAILED & exit /b 1)
echo CHUNK 0000 OK

echo === Compiling chunk 0019 (largest 11.5MB) ===
clang-cl %CLANGFLAGS% %INCLUDES% -Fo"%OUT%\sub_0031D200_chunk_0019.obj" "%SRC%\sub_0031D200_chunk_0019.cpp" 2>&1
if errorlevel 1 (echo CHUNK 0019 FAILED & exit /b 1)
echo CHUNK 0019 OK

echo === ALL TESTS PASSED ===

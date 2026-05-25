@echo off
setlocal enabledelayedexpansion

REM Compile just the 4 previously-failed chunks to verify the fix_chunk_recompiler_bugs.py
REM patches make them buildable.

call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x64 >nul 2>&1
set PATH=C:\Program Files\LLVM\bin;%PATH%

set ROOT=C:\Programming\GitHub\Star-Wars-Racer-Revenge-recomp
set OUT=%ROOT%\src\clang_objs
set SRC=%ROOT%\src\generated_chunks

set CLANGFLAGS=/c /Od /bigobj /std:c++20 /EHsc /MD /W0 /arch:AVX
set INCLUDES=-I"%ROOT%\tools\PS2Recomp\ps2xRuntime\include" -I"%ROOT%\tools\PS2Recomp\ps2xRuntime\src\lib\Kernel" -I"%ROOT%\src\generated" -I"%ROOT%\include" -I"%ROOT%\build\_deps\raylib-src\src" -I"%ROOT%\build\_deps\raylib-src\src\external\glfw\include" -I"%SRC%"

for %%c in (0264 0452 0464 0481) do (
    echo === Compiling chunk %%c ===
    clang-cl %CLANGFLAGS% %INCLUDES% -Fo"%OUT%\sub_0031D200_chunk_%%c.obj" "%SRC%\sub_0031D200_chunk_%%c.cpp" 2>&1
    if errorlevel 1 (echo CHUNK %%c FAILED & exit /b 1)
    echo CHUNK %%c OK
)

echo === ALL FIXED CHUNKS COMPILE ===

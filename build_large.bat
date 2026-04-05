@echo off
setlocal

call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x64 >nul 2>&1

set PATH=C:\Program Files\LLVM\bin;%PATH%
set SRCDIR=D:\Programming\GitHub\Racer Revenge decomp\src\generated
set OUTDIR=D:\Programming\GitHub\Racer Revenge decomp\src\clang_objs
set INCDIR1=D:\Programming\GitHub\Racer Revenge decomp\tools\PS2Recomp\ps2xRuntime\include
set INCDIR2=D:\Programming\GitHub\Racer Revenge decomp\src\generated
set INCDIR3=D:\Programming\GitHub\Racer Revenge decomp\include
set INCDIR4=D:\Programming\GitHub\Racer Revenge decomp\build\_deps\raylib-src\src
set INCDIR5=D:\Programming\GitHub\Racer Revenge decomp\build\_deps\raylib-src\src\external\glfw\include

echo Compiling large files with clang-cl...
echo.

for %%f in (
    entry_2a3c80_0x2a4b70
    sub_001BA130_0x1ba130
    sub_001BEEB0_0x1beeb0
    sub_001EEC40_0x1eec40
    sub_0026FD80_0x26fd80
    sub_002A3AD0_0x2a3ad0
) do (
    if exist "%SRCDIR%\%%f.cpp" (
        echo   Compiling %%f.cpp...
        clang-cl /c /Od /bigobj /std:c++20 /EHsc /MD -I"%INCDIR1%" -I"%INCDIR2%" -I"%INCDIR3%" -I"%INCDIR4%" -I"%INCDIR5%" -Fo"%OUTDIR%\%%f.obj" "%SRCDIR%\%%f.cpp"
        if errorlevel 1 (echo   FAILED: %%f) else (echo   OK: %%f)
    )
)

echo.
echo Skipping 2.1GB files (sub_0031D200, entry_31d280) - too large even for clang.
echo Done.

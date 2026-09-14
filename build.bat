@echo off
setlocal

REM Keep this launcher ASCII-only. Changing code page while CMD is reading a
REM UTF-8 batch file can corrupt its read position and execute text fragments.
set "_ORIG_CP="
for /f "tokens=2 delims=:" %%c in ('chcp 2^>nul') do (
    for /f "tokens=1 delims= " %%d in ("%%c") do set "_ORIG_CP=%%d"
)

chcp 65001 >nul 2>&1
if errorlevel 1 (
    echo [ERROR] UTF-8 console setup failed. Build stopped.
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "AEGIS_BUILD_WRAPPER=1"

call "%~dp0scripts\build_impl.bat" %*
set "_BUILD_EXIT=%ERRORLEVEL%"

if defined _ORIG_CP chcp %_ORIG_CP% >nul 2>&1
endlocal & exit /b %_BUILD_EXIT%

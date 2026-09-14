@echo off
setlocal enabledelayedexpansion

REM Switch the console to UTF-8 before the first non-ASCII line in this file.
REM Restore the original code page before exiting.
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

REM Aegis Desk 포터블 빌드 스크립트 — 사내 PC에서 실행
REM
REM 사용법:
REM   build.bat          클린 빌드 (배포용 — 기본값)
REM   build.bat fast     캐시 재사용 빌드 (개발 중 반복 확인용, 배포 금지)
REM
REM   기본을 '클린'으로 둔 이유: 이건 배포용 빌드다. PyInstaller 증분 캐시는
REM   spec 변경·의존성 업그레이드 시 stale 상태로 깨진 결과물을 만들 수 있고,
REM   그렇게 만들어진 exe가 테스터에게 나가는 비용이 빌드 몇 분보다 훨씬 크다.
REM   빌드는 릴리스 단위라 자주 하지도 않는다. 반복 확인이 필요할 때만 'fast'로
REM   명시해서 캐시를 쓴다.
REM
REM 사전 체크리스트 (빌드 전 확인!):
REM   1. knowmate\config.yaml 의 embedding.base_url / llm.base_url 을
REM      실제 사내 서버 IP로 채웠는가? (10.x.x.x 그대로면 테스터 앱이 동작 안 함)
REM   2. .venv 에 requirements.txt 전체가 설치되어 있는가?
REM      (pyinstaller도 requirements.txt에 고정 버전으로 포함돼 있다)
REM
REM 결과물: dist\AegisDesk\  (이 폴더를 zip으로 압축해 테스터에게 배포)

cd /d "%~dp0"

set "BUILD_MODE=clean"
if /i "%~1"=="fast" set "BUILD_MODE=fast"
set "BUILD_META_DIR=%CD%\.aegisdesk_build"

if not exist ".venv\Scripts\python.exe" (
    echo [오류] .venv 가 없습니다. 먼저 가상환경을 만들고 requirements.txt 를 설치하세요.
    call :restore_cp
    pause
    exit /b 1
)

REM dist\ 는 모드와 무관하게 항상 지운다 — PyInstaller 는 --noconfirm 으로 덮어쓸 뿐
REM 이전 빌드에만 있던 파일을 지우지는 않는다. 남겨두면 더 이상 필요 없는 DLL·구 UI
REM 파일이 그대로 배포 zip 에 섞여 나간다.
echo === 이전 배포 결과물 정리 (dist) ===
if exist "dist" rmdir /s /q "dist"
if exist "dist" (
    echo [오류] dist 폴더를 완전히 삭제하지 못했습니다. 실행 중인 AegisDesk를 종료하고 다시 시도하세요.
    call :restore_cp
    pause
    exit /b 1
)

if /i "%BUILD_MODE%"=="clean" (
    echo === 클린 빌드: 빌드 캐시 삭제 ===
    if exist "build" rmdir /s /q "build"
    if exist "build" (
        echo [오류] build 폴더를 완전히 삭제하지 못했습니다. 파일 점유를 확인하세요.
        call :restore_cp
        pause
        exit /b 1
    )
    set "PYI_FLAGS=--noconfirm --clean"
) else (
    echo === 빠른 빌드: 빌드 캐시 재사용 ^(배포용으로 쓰지 말 것^) ===
    set "PYI_FLAGS=--noconfirm"
)

if exist "!BUILD_META_DIR!" rmdir /s /q "!BUILD_META_DIR!"
if exist "!BUILD_META_DIR!" (
    echo [오류] 이전 빌드 출처 정보 폴더를 삭제하지 못했습니다.
    call :restore_cp
    pause
    exit /b 1
)

echo === 빌드 환경 및 소스 버전 확인 ===
.venv\Scripts\python.exe scripts\build_guard.py --output-dir "!BUILD_META_DIR!"
if errorlevel 1 (
    echo [오류] 빌드 환경 확인 실패. 위 내용을 확인하세요.
    call :cleanup_build_meta
    call :restore_cp
    pause
    exit /b 1
)
set "AEGIS_BUILD_INFO_DIR=!BUILD_META_DIR!"

echo === PyInstaller 빌드 시작 ^(%BUILD_MODE%^) ===
.venv\Scripts\python.exe -m PyInstaller !PYI_FLAGS! AegisDesk.spec

if errorlevel 1 (
    echo [오류] 빌드 실패. 위 로그를 확인하세요.
    call :cleanup_build_meta
    call :restore_cp
    pause
    exit /b 1
)

REM 빌드 직후 자체 점검 — 번들 리소스·WebEngine 프로세스 실행파일·지연 import
REM 모듈·lancedb 버전·빌드 출처·로그 폴더를 확인한다. --windowed 빌드라 콘솔
REM 출력이 붙지 않으므로 종료 코드로 판정하고 상세 내용은 파일로 받는다.
echo === 빌드 자체 점검 ^(--selftest^) ===
"dist\AegisDesk\AegisDesk.exe" --selftest --selftest-report "dist\selftest.log" 2> "dist\selftest.stderr.log"
set "SELFTEST_EXIT=!ERRORLEVEL!"
if not exist "dist\selftest.log" (
    echo [오류] 자체 점검 보고서가 생성되지 않았습니다.
    set "SELFTEST_EXIT=1"
) else (
    for %%F in ("dist\selftest.log") do if %%~zF EQU 0 (
        echo [오류] 자체 점검 보고서가 비어 있습니다.
        set "SELFTEST_EXIT=1"
    )
)
if not "!SELFTEST_EXIT!"=="0" (
    echo [오류] 자체 점검 실패 — 번들에 빠진 것이 있습니다. 배포하지 마세요.
    echo.
    if exist "dist\selftest.log" type "dist\selftest.log"
    if exist "dist\selftest.stderr.log" type "dist\selftest.stderr.log"
    echo.
    echo         상세: dist\selftest.log
    call :cleanup_build_meta
    call :restore_cp
    pause
    exit /b 1
)
if exist "dist\selftest.log" type "dist\selftest.log"

echo.
echo === 빌드 완료 ^(%BUILD_MODE%^) ===
echo 결과물: dist\AegisDesk\AegisDesk.exe
echo 이 폴더(dist\AegisDesk\)를 통째로 zip으로 압축해 테스터에게 배포하세요.
echo.
if /i "%BUILD_MODE%"=="fast" (
    echo [주의] 캐시 재사용 빌드입니다. 배포 전에는 반드시 'build.bat' 으로
    echo        클린 빌드를 다시 하세요.
    echo.
)
echo [필수] 자체 점검은 '번들에 파일이 있는지'만 확인합니다. WebEngine이 실제로
echo        화면을 그리는지는 창을 띄워야만 알 수 있으므로, 배포 전 사내 PC에서
echo        dist\AegisDesk\AegisDesk.exe 를 직접 실행해 흰 화면 없이 뜨는지
echo        한 번 확인하세요.
pause
call :cleanup_build_meta
call :restore_cp
endlocal
exit /b 0

REM ── 콘솔 코드페이지 복원 ──────────────────────────────────────────────
REM 스크립트가 65001로 바꿔놓은 것을 원래대로 되돌린다. 값을 못 읽었으면
REM (locale에 따라 chcp 출력 형식이 다를 수 있음) 아무 것도 하지 않는다 —
REM 복원 실패가 빌드 결과에 영향을 주지는 않는다.
:restore_cp
if defined _ORIG_CP chcp %_ORIG_CP% >nul 2>&1
exit /b 0

:cleanup_build_meta
if defined BUILD_META_DIR if exist "!BUILD_META_DIR!" rmdir /s /q "!BUILD_META_DIR!"
exit /b 0

@echo off
setlocal enabledelayedexpansion

REM 이 파일은 UTF-8(BOM 없음)로 저장돼 있다. cmd는 배치 파일을 '현재 콘솔
REM 코드페이지'로 해석하므로, 콘솔이 949(한국어 Windows 기본)인 상태에서
REM 실행하면 아래 한글 메시지가 전부 깨진다 — 특히 탐색기에서 .bat을
REM 더블클릭하면 새 콘솔이 시스템 기본값으로 열려 항상 이 상황이 된다.
REM 그래서 스크립트가 스스로 65001(UTF-8)로 맞추고, 끝나면 원래 값으로
REM 돌려놓는다(다른 도구가 949를 기대할 수 있으므로).
REM 참고: 한글이 든 REM 주석은 화면에 출력되지 않아 파싱에 영향이 없다
REM (UTF-8 한글 바이트는 모두 0x80 이상이라 cmd 메타문자와 충돌하지 않는다).
set "_ORIG_CP="
for /f "tokens=2 delims=:" %%c in ('chcp 2^>nul') do (
    for /f "tokens=1 delims= " %%d in ("%%c") do set "_ORIG_CP=%%d"
)
chcp 65001 >nul 2>&1

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

if not exist ".venv\Scripts\python.exe" (
    echo [오류] .venv 가 없습니다. 먼저 가상환경을 만들고 requirements.txt 를 설치하세요.
    call :restore_cp
    pause
    exit /b 1
)

REM PyInstaller 고정 버전 확인 — 번들 구성(수집되는 Qt 리소스·WebEngine 프로세스)이
REM 버전에 따라 달라져 requirements.txt에 정확히 고정돼 있다. 다르면 경고만 하고
REM 진행한다(빌드를 막을 만큼 확실한 장애는 아니지만 배포 전에 알아야 한다).
REM 첫 공백까지만 잘라 "pyinstaller==6.21.0" 을 얻고, 접두사를 지워 버전만 남긴다
REM (뒤에 붙은 한글 주석은 공백 구분으로 자연히 떨어져 나간다).
set "PINNED_PYI="
set "PINNED_LINE="
set "ACTUAL_PYI="
for /f "tokens=1 delims= " %%a in ('findstr /b /c:"pyinstaller==" requirements.txt') do set "PINNED_LINE=%%a"
if defined PINNED_LINE set "PINNED_PYI=!PINNED_LINE:pyinstaller==!"
for /f "delims=" %%v in ('.venv\Scripts\python.exe -m PyInstaller --version 2^>nul') do set "ACTUAL_PYI=%%v"
if not defined ACTUAL_PYI (
    echo [오류] PyInstaller 가 .venv 에 없습니다.
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    call :restore_cp
    pause
    exit /b 1
)
if not "!PINNED_PYI!"=="!ACTUAL_PYI!" (
    echo [경고] PyInstaller 버전이 고정값과 다릅니다: 설치=!ACTUAL_PYI! / 고정=!PINNED_PYI!
    echo         번들 구성이 달라질 수 있습니다. requirements.txt 로 재설치를 권장합니다.
    echo.
)

REM dist\ 는 모드와 무관하게 항상 지운다 — PyInstaller 는 --noconfirm 으로 덮어쓸 뿐
REM 이전 빌드에만 있던 파일을 지우지는 않는다. 남겨두면 더 이상 필요 없는 DLL·구 UI
REM 파일이 그대로 배포 zip 에 섞여 나간다.
echo === 이전 배포 결과물 정리 (dist) ===
if exist "dist" rmdir /s /q "dist"

if /i "%BUILD_MODE%"=="clean" (
    echo === 클린 빌드: 빌드 캐시 삭제 ===
    if exist "build" rmdir /s /q "build"
    set "PYI_FLAGS=--noconfirm --clean"
) else (
    echo === 빠른 빌드: 빌드 캐시 재사용 ^(배포용으로 쓰지 말 것^) ===
    set "PYI_FLAGS=--noconfirm"
)

echo === PyInstaller 빌드 시작 ^(%BUILD_MODE%^) ===
.venv\Scripts\python.exe -m PyInstaller AegisDesk.spec !PYI_FLAGS!

if errorlevel 1 (
    echo [오류] 빌드 실패. 위 로그를 확인하세요.
    call :restore_cp
    pause
    exit /b 1
)

REM 빌드 직후 자체 점검 — 번들 리소스·WebEngine 프로세스 실행파일·지연 import
REM 모듈·lancedb 버전·로그 폴더를 확인한다. --windowed 빌드라 콘솔 출력이 붙지
REM 않으므로 종료 코드로 판정하고 상세 내용은 파일로 받는다.
echo === 빌드 자체 점검 ^(--selftest^) ===
"dist\AegisDesk\AegisDesk.exe" --selftest 2> "dist\selftest.log"
if errorlevel 1 (
    echo [오류] 자체 점검 실패 — 번들에 빠진 것이 있습니다. 배포하지 마세요.
    echo.
    if exist "dist\selftest.log" type "dist\selftest.log"
    echo.
    echo         상세: dist\selftest.log
    echo         (정본 보고서: %%APPDATA%%\AegisDesk\logs\selftest.log)
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

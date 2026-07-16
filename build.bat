@echo off
REM Aegis Desk 포터블 빌드 스크립트 — 사내 PC에서 실행
REM
REM 사전 체크리스트 (빌드 전 확인!):
REM   1. knowmate\config.yaml 의 embedding.base_url / llm.base_url 을
REM      실제 사내 서버 IP로 채웠는가? (10.x.x.x 그대로면 테스터 앱이 동작 안 함)
REM   2. .venv 에 pyinstaller 가 설치되어 있는가? (pip install pyinstaller)
REM   3. .venv 에 requirements.txt 전체가 설치되어 있는가?
REM
REM 결과물: dist\AegisDesk\  (이 폴더를 zip으로 압축해 테스터에게 배포)

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [오류] .venv 가 없습니다. 먼저 가상환경을 만들고 requirements.txt 를 설치하세요.
    pause
    exit /b 1
)

echo === 이전 빌드 결과 정리 ===
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

echo === PyInstaller 빌드 시작 ===
.venv\Scripts\python.exe -m PyInstaller AegisDesk.spec --noconfirm

if errorlevel 1 (
    echo [오류] 빌드 실패. 위 로그를 확인하세요.
    pause
    exit /b 1
)

echo.
echo === 빌드 완료 ===
echo 결과물: dist\AegisDesk\AegisDesk.exe
echo 이 폴더(dist\AegisDesk\)를 통째로 zip으로 압축해 테스터에게 배포하세요.
echo.
echo [필수] 배포 전 사내 PC에서 dist\AegisDesk\AegisDesk.exe 를 직접 실행해
echo        흰 화면 없이 정상 동작하는지, 로그가 %%APPDATA%%\AegisDesk\logs 에
echo        생성되는지 확인하세요.
pause

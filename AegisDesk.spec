# -*- mode: python ; coding: utf-8 -*-
"""Aegis Desk PyInstaller 빌드 스펙.

사내 PC(파이썬·의존성 설치된 .venv)에서 build.bat으로 실행한다.
onedir 채택 이유: QWebEngineView 리소스(수백MB)를 매 실행 압축해제하는
onefile은 시동이 느리고 폐쇄망 백신 오탐이 잦다.
"""
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# PyQt6 전체(WebEngine 프로세스·리소스·번역 파일 포함) 수집
for pkg in ("PyQt6",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# LanceDB/PyArrow/Pandas 등은 동적 import가 많아 hiddenimports로 명시
hiddenimports += [
    "lancedb",
    "pyarrow",
    "pandas",
    "yaml",
    "cryptography",
    "win32crypt",
    "win32com",
    # win32timezone: pywin32가 COM 날짜/시간(VT_DATE) 값을 변환할 때 지연 import.
    # Excel 날짜 셀 파싱 시 필요한데 정적 분석으로는 안 잡혀 exe에서만
    # "No module named win32timezone"으로 실패했다(소스 실행은 정상).
    "win32timezone",
    "pywintypes",
    "pythoncom",
    "docx",
    "openpyxl",
    "xlrd",
    "pptx",
    "fitz",
]

# UI 리소스(HTML/JS/CSS/assets) + config.yaml 템플릿을 번들에 포함
# main.py의 resource_path()가 이 상대경로("knowmate/...")를 그대로 찾는다.
datas += [
    ("knowmate/app/ui", "knowmate/app/ui"),
    ("knowmate/config.yaml", "knowmate"),
]

a = Analysis(
    ["knowmate/app/main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AegisDesk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # --windowed: 콘솔창 숨김 (로그는 파일로 남음)
    icon="knowmate/app/ui/assets/aegisdesk.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AegisDesk",       # 결과: dist/AegisDesk/
)

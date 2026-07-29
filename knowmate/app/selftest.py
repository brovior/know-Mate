"""포터블 빌드(frozen) 자체 점검 — `AegisDesk.exe --selftest`.

**목적**: "소스에서는 되는데 exe에서만 안 되는" 부류의 번들 누락을 배포 전에
자동으로 잡는다. 이 앱은 `--windowed`(console=False) 빌드라 exe를 실행해도
콘솔 출력이 없어, 지금까지는 사람이 직접 띄워보고 눈으로 확인하는 수밖에
없었다(build.bat 말미의 안내문). 실제로 그 방식으로는 `win32timezone` 지연
import 누락을 배포 후에야 발견한 적이 있다(커밋 a6b12e6).

**점검 대상 = frozen일 때만 깨질 수 있는 지점**만 고른다. 일반 로직 회귀는
pytest가 담당하므로 여기서 중복하지 않는다:

  1. 번들 리소스 경로(`resource_path`) — UI 파일·아이콘 실재 여부(흰 화면 직결)
  2. 번들 config 템플릿(`_bundled_config_source`) — 최초 실행 시드 소스
  3. QtWebEngineProcess 실행 파일 — 없으면 화면이 흰색으로 뜬다
  4. 지연 import 모듈(win32timezone 등) — 정적 분석으로 안 잡히는 것들
  5. lancedb 번들 버전 — 검증 범위 밖 버전이 섞였는지
  6. 로그 폴더 쓰기 가능 여부 — 실패 시 진단 수단 자체를 잃는다

**결과 전달**: 종료 코드(0=통과, 1=실패)와 stderr 출력. `--windowed` exe는
stdout/stderr가 어디에도 안 붙지만 **종료 코드는 그대로 전달**되므로
build.bat이 `errorlevel`로 판정할 수 있다. 사람이 원인을 보려면 리다이렉트
(`AegisDesk.exe --selftest 2> out.txt`)하면 된다.

**한계(중요)**: 이 점검은 *번들 구성*만 본다 — 파일이 있고 import가 되는지.
WebEngine이 실제로 페이지를 렌더링하는지는 창을 띄워야만 알 수 있으므로
**여전히 사람이 한 번 실행해 눈으로 확인해야 한다**. 여기서 3번(프로세스
실행 파일 존재)까지 통과하면 흰 화면 원인 중 가장 흔한 것은 배제된다.
"""
from __future__ import annotations

import sys
from pathlib import Path

# 지연 import(정적 분석으로 안 잡혀 hiddenimports에 명시한 것들)를 실제로 import해본다.
# spec의 hiddenimports와 짝을 이룬다 — 거기서 빠지면 여기서 실패한다.
_WINDOWS_LAZY_IMPORTS = ("win32timezone", "win32crypt", "pythoncom", "pywintypes")
_COMMON_IMPORTS = ("lancedb", "pyarrow", "pandas", "yaml", "cryptography",
                   "docx", "openpyxl", "xlrd", "pptx", "fitz")


def _check_bundled_resources(failures: list[str]) -> None:
    """UI 리소스·아이콘·config 템플릿이 번들에 실재하는지 확인한다."""
    from knowmate.app.main import UI_DIR, APP_ICON

    for label, path in (
        ("UI 폴더", UI_DIR),
        ("index.html", UI_DIR / "index.html"),
        ("app.js", UI_DIR / "app.js"),
        ("styles.css", UI_DIR / "styles.css"),
        ("앱 아이콘", APP_ICON),
    ):
        if not Path(path).exists():
            failures.append(f"번들 리소스 누락: {label} ({path})")

    from knowmate.config import _bundled_config_source
    template = _bundled_config_source()
    if not template.exists():
        failures.append(f"번들 config 템플릿 누락: {template}")


def _check_webengine_process(failures: list[str]) -> None:
    """QtWebEngineProcess 실행 파일이 번들에 있는지 확인한다(흰 화면 최다 원인).

    PyQt6 설치 레이아웃에 따라 위치가 달라 후보를 여러 개 본다. 하나라도
    있으면 통과 — 경로를 하드코딩해 오탐을 내는 것보다 낫다.
    """
    try:
        import PyQt6.QtCore as qtcore
    except Exception as exc:
        failures.append(f"PyQt6.QtCore import 실패: {type(exc).__name__}")
        return

    qt_root = Path(qtcore.__file__).resolve().parent
    exe_name = "QtWebEngineProcess.exe" if sys.platform == "win32" else "QtWebEngineProcess"
    candidates = list(qt_root.rglob(exe_name))
    if not candidates:
        failures.append(
            f"{exe_name}를 번들에서 찾지 못함 — 실행 시 흰 화면이 될 가능성이 높다 "
            f"(탐색 기준: {qt_root})"
        )


def _check_lazy_imports(failures: list[str]) -> None:
    """정적 분석으로 안 잡히는 지연 import 모듈이 실제로 import되는지 확인한다."""
    import importlib

    names = list(_COMMON_IMPORTS)
    if sys.platform == "win32":
        names += list(_WINDOWS_LAZY_IMPORTS)

    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures.append(f"모듈 import 실패: {name} ({type(exc).__name__}: {exc})")


def _check_lancedb_version(failures: list[str]) -> None:
    """번들된 lancedb가 실측 검증 버전인지 확인한다(빌드 실수 조기 발견)."""
    from knowmate.lancedb_compat import check_lancedb_version

    warning = check_lancedb_version()
    if warning:
        failures.append(f"lancedb 버전: {warning}")


def _check_log_dir_writable(failures: list[str]) -> None:
    """로그 폴더가 생성·기록 가능한지 확인한다 — 실패하면 진단 수단 자체를 잃는다."""
    from knowmate.config import get_data_dir

    try:
        log_dir = get_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / ".selftest_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        failures.append(f"로그 폴더 쓰기 불가: {type(exc).__name__}: {exc}")


def run_selftest() -> int:
    """번들 자체 점검을 수행하고 종료 코드를 반환한다(0=통과, 1=실패).

    각 점검은 독립적으로 실행해 **한 항목이 실패해도 나머지를 계속 확인한다**
    — 빌드 담당자가 한 번에 모든 누락을 보고 고칠 수 있도록.
    """
    failures: list[str] = []
    checks = (
        ("번들 리소스", _check_bundled_resources),
        ("WebEngine 프로세스", _check_webengine_process),
        ("지연 import 모듈", _check_lazy_imports),
        ("lancedb 버전", _check_lancedb_version),
        ("로그 폴더", _check_log_dir_writable),
    )
    for label, check in checks:
        try:
            check(failures)
        except Exception as exc:  # 점검 자체의 예외도 실패로 취급(조용한 통과 금지)
            failures.append(f"[{label}] 점검 중 예외: {type(exc).__name__}: {exc}")

    frozen = bool(getattr(sys, "frozen", False))
    print(f"[selftest] frozen={frozen} platform={sys.platform}", file=sys.stderr)
    if failures:
        print(f"[selftest] 실패 {len(failures)}건:", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1

    print("[selftest] 전체 통과", file=sys.stderr)
    if not frozen:
        print(
            "[selftest] 주의: frozen이 아닌 소스 실행에서 돌았다 — "
            "번들 누락 검증은 exe로 실행해야 의미가 있다.",
            file=sys.stderr,
        )
    return 0

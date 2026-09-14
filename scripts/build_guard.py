"""배포 빌드 환경을 검증하고 exe에 포함할 Git 출처 정보를 생성한다."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PIN_PATTERN = re.compile(r"^pyinstaller==([^\s#]+)", re.IGNORECASE)


def _pinned_pyinstaller_version(requirements_path: Path) -> str:
    """requirements.txt에서 정확히 고정된 PyInstaller 버전을 읽는다."""
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        match = PIN_PATTERN.match(line.strip())
        if match:
            return match.group(1)
    raise RuntimeError("requirements.txt에 pyinstaller== 고정값이 없습니다")


def _git(*args: str, required: bool = True, allow_empty: bool = False) -> str:
    """저장소에서 Git 명령을 실행하고 한 줄 결과를 반환한다."""
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    value = result.stdout.strip()
    if required and (result.returncode or (not value and not allow_empty)):
        detail = result.stderr.strip() or "결과 없음"
        raise RuntimeError(f"git {' '.join(args)} 실패: {detail}")
    return value if result.returncode == 0 else ""


def _git_bytes(*args: str) -> bytes:
    """작업 트리 지문 계산용 Git 결과를 원본 바이트로 반환한다."""
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} 실패: {detail or '결과 없음'}")
    return result.stdout


def _source_fingerprint() -> str:
    """tracked 변경과 untracked 파일 내용을 포함한 짧은 작업 트리 지문을 만든다."""
    digest = hashlib.sha256()
    digest.update(_git_bytes("diff", "HEAD", "--binary", "--no-ext-diff"))
    untracked = _git_bytes(
        "ls-files", "--others", "--exclude-standard", "-z",
    ).split(b"\0")
    for raw_path in sorted(path for path in untracked if path):
        digest.update(b"\0untracked\0")
        digest.update(raw_path)
        path = REPO_ROOT / os.fsdecode(raw_path)
        if path.is_file():
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
    return digest.hexdigest()[:8]


def _build_info() -> dict[str, object]:
    """현재 체크아웃을 식별할 최소 빌드 정보를 만든다."""
    commit = _git("rev-parse", "HEAD")
    branch = _git("branch", "--show-current", required=False) or "detached"
    dirty = bool(_git("status", "--porcelain", allow_empty=True))
    origin_main = _git("rev-parse", "--verify", "origin/main", required=False)
    return {
        "commit": commit,
        "short_commit": commit[:7],
        "branch": branch,
        "dirty": dirty,
        "source_fingerprint": _source_fingerprint() if dirty else "",
        "origin_main": origin_main,
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def prepare(output_dir: Path) -> None:
    """도구 버전을 검증하고 PyInstaller가 포함할 모듈을 생성한다."""
    root_module = REPO_ROOT / "aegisdesk_build_info.py"
    if root_module.exists():
        raise RuntimeError(
            "저장소 루트의 aegisdesk_build_info.py가 생성 정보를 가릴 수 있습니다. "
            "파일을 옮긴 뒤 다시 빌드하세요"
        )

    pinned = _pinned_pyinstaller_version(REPO_ROOT / "requirements.txt")
    actual = importlib.metadata.version("pyinstaller")
    if actual != pinned:
        raise RuntimeError(
            f"PyInstaller 버전 불일치: 설치={actual} / 고정={pinned}. "
            "requirements.txt로 다시 설치하세요"
        )

    info = _build_info()
    output_dir.mkdir(parents=True, exist_ok=True)
    module_path = output_dir / "aegisdesk_build_info.py"
    module_path.write_text(
        "# build_guard.py가 생성한 배포 출처 정보입니다.\n"
        f"BUILD_INFO = {info!r}\n",
        encoding="utf-8",
    )

    print(f"[확인] PyInstaller {actual}")
    print(
        f"[확인] 빌드 소스: branch={info['branch']} commit={info['short_commit']} "
        f"dirty={info['dirty']}"
    )
    if info["dirty"]:
        print(
            "[주의] 커밋되지 않은 변경이 포함된 빌드입니다: "
            f"source={info['source_fingerprint']}"
        )
    if info["origin_main"] and info["commit"] != info["origin_main"]:
        origin_short = str(info["origin_main"])[:7]
        print(f"[주의] 현재 HEAD가 로컬 origin/main과 다릅니다: origin/main={origin_short}")


def main() -> int:
    """명령행 인자를 처리하고 검증 실패를 종료 코드로 전달한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.output_dir)
    except Exception as exc:
        print(f"[오류] 빌드 환경 검증 실패: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

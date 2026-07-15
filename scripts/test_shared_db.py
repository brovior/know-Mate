"""공유 폴더 벡터DB(5b) 사전 기술 검증 스크립트 v2.

사내 PC에서 실행해 공유 폴더가 공용 LanceDB를 감당할 수 있는지 확인한다.
v2: SMB 직접 쓰기 실패(RustPanic) 시, 대안 구조 2가지를 추가 판정한다.
    A. 로컬 생성 → 공유 폴더 복사 → SMB에서 직접 읽기   (마스터=복사 배포)
    B. 공유 폴더 → 로컬 캐시 복사 → 로컬에서 읽기       (사용자=로컬 캐시)

사용법:
    python scripts/test_shared_db.py "K:\\공용\\우리파트"

산출물은 <대상폴더>/_aegisdesk_test/ 와 로컬 임시폴더에만 만들고 끝나면 삭제한다.
"""
from __future__ import annotations

import os

# 러스트 패닉 시 상세 원인 출력 (lancedb import 전에 설정)
os.environ.setdefault("RUST_BACKTRACE", "1")

import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

VECTOR_DIM = 1024  # rag/embedding.py 와 동일 차원

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """검증 결과를 기록하고 즉시 출력한다."""
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    results.append((name, ok, detail))


def _schema():
    import pyarrow as pa
    return pa.schema([
        pa.field("chunk_id", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("text", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
        pa.field("is_deleted", pa.bool_()),
    ])


def _rows(target: Path, n: int = 20) -> list[dict]:
    import random
    return [
        {
            "chunk_id": f"test-{i}",
            "file_path": f"{target}/dummy/doc{i}.txt",
            "text": f"테스트 청크 {i}",
            "vector": [random.random() for _ in range(VECTOR_DIM)],
            "is_deleted": False,
        }
        for i in range(n)
    ]


def _build_db(db_dir: Path, target: Path) -> None:
    """db_dir에 chunks 테이블(20행)을 생성한다."""
    import lancedb
    db = lancedb.connect(str(db_dir))
    table = db.create_table("chunks", schema=_schema())
    table.add(_rows(target))


def _search_db(db_dir: Path) -> int:
    """db_dir의 chunks 테이블을 벡터 검색해 히트 수를 반환한다."""
    import lancedb
    import random
    db = lancedb.connect(str(db_dir))
    table = db.open_table("chunks")
    qvec = [random.random() for _ in range(VECTOR_DIM)]
    hits = (
        table.search(qvec)
        .where("is_deleted = false")
        .limit(5)
        .to_arrow()
        .to_pandas()
    )
    return len(hits)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    target = Path(sys.argv[1])
    print(f"\n=== 공유 폴더 벡터DB 사전 검증 v2: {target} ===\n")

    # ── 1. 폴더 접근 ──────────────────────────────────────────────
    try:
        entries = os.listdir(target)
        check("1. 폴더 접근·목록 조회", True, f"{len(entries)}개 항목")
    except OSError as exc:
        check("1. 폴더 접근·목록 조회", False, str(exc))
        print("\n폴더 자체에 접근이 안 됩니다. EFSS 드라이브(M: 계열)라면 구조적으로 불가합니다.")
        return 1

    test_dir = target / "_aegisdesk_test"
    local_tmp = Path(tempfile.mkdtemp(prefix="aegis_sharetest_"))

    try:
        # ── 2~4. 파일시스템 기본 동작 ─────────────────────────────
        try:
            test_dir.mkdir(exist_ok=True)
            check("2. 하위 폴더 생성", True, str(test_dir))
        except OSError as exc:
            check("2. 하위 폴더 생성", False, str(exc))
            return 1

        try:
            f = test_dir / "write_test.txt"
            f.write_text("aegis desk write test", encoding="utf-8")
            ok = f.read_text(encoding="utf-8") == "aegis desk write test"
            f.unlink()
            check("3. 파일 쓰기/읽기/삭제", ok)
        except OSError as exc:
            check("3. 파일 쓰기/읽기/삭제", False, str(exc))
            return 1

        try:
            cur = test_dir / "current.txt"
            cur.write_text("db_v1", encoding="utf-8")
            tmp = test_dir / "current.tmp"
            tmp.write_text("db_v2", encoding="utf-8")
            os.replace(tmp, cur)
            check("4. 원자적 교체(os.replace)", cur.read_text(encoding="utf-8") == "db_v2")
        except OSError as exc:
            check("4. 원자적 교체(os.replace)", False, str(exc))

        # ── LanceDB 준비 ──────────────────────────────────────────
        try:
            import lancedb  # noqa: F401
            import pyarrow  # noqa: F401
            print(f"\n  lancedb 버전: {lancedb.__version__}")
        except ImportError as exc:
            check("5. LanceDB", False, f"패키지 미설치: {exc}")
            return 1

        # ── 5. SMB 직접 쓰기+검색 (단계별 분리) ───────────────────
        smb_write_ok = False
        try:
            db_dir = test_dir / "db_direct"
            t0 = time.time()
            _build_db(db_dir, target)
            check("5a. SMB 직접 생성·저장", True, f"{time.time() - t0:.2f}초")
            t0 = time.time()
            n = _search_db(db_dir)
            check("5b. SMB 직접 검색", n == 5, f"{time.time() - t0:.2f}초, {n}건")
            smb_write_ok = True
        except BaseException as exc:  # RustPanic은 Exception이 아닐 수 있음
            check("5. SMB 직접 쓰기/검색", False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            print("  → 대안 구조 판정으로 진행합니다.\n")

        # ── 6. 대안 A: 로컬 생성 → 공유 복사 → SMB에서 읽기 ───────
        smb_read_ok = False
        try:
            local_db = local_tmp / "db_local"
            _build_db(local_db, target)          # 로컬에서 생성 (마스터 시뮬레이션)
            shared_copy = test_dir / "db_copied"
            t0 = time.time()
            shutil.copytree(local_db, shared_copy)
            copy_sec = time.time() - t0
            t0 = time.time()
            n = _search_db(shared_copy)          # SMB 경로에서 직접 검색
            check("6. 대안A: 로컬생성→공유복사→SMB 읽기", n == 5,
                  f"복사 {copy_sec:.2f}초 / 검색 {time.time() - t0:.2f}초")
            smb_read_ok = True
        except BaseException as exc:
            check("6. 대안A: 로컬생성→공유복사→SMB 읽기", False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()

        # ── 7. 대안 B: 공유 → 로컬 캐시 복사 → 로컬에서 읽기 ──────
        try:
            src = test_dir / "db_copied"
            if not src.exists():
                local_db = local_tmp / "db_local"
                if not local_db.exists():
                    _build_db(local_db, target)
                src = local_db
            cache_db = local_tmp / "db_cache"
            t0 = time.time()
            shutil.copytree(src, cache_db)
            copy_sec = time.time() - t0
            t0 = time.time()
            n = _search_db(cache_db)
            check("7. 대안B: 공유→로컬캐시→로컬 읽기", n == 5,
                  f"복사 {copy_sec:.2f}초 / 검색 {time.time() - t0:.2f}초")
        except BaseException as exc:
            check("7. 대안B: 공유→로컬캐시→로컬 읽기", False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()

    finally:
        for d, label in ((test_dir, "공유 테스트 폴더"), (local_tmp, "로컬 임시 폴더")):
            try:
                shutil.rmtree(d, ignore_errors=False)
                print(f"\n정리 완료: {label} 삭제됨 ({d})")
            except OSError as exc:
                print(f"\n[주의] {label} 삭제 실패 — 수동 삭제 필요: {d} ({exc})")

    # ── 요약·판정 ────────────────────────────────────────────────
    print("\n=== 결과 요약 ===")
    for name, ok, _ in results:
        print(f"  {'✅' if ok else '❌'} {name}")

    def passed(prefix: str) -> bool:
        return any(ok for name, ok, _ in results if name.startswith(prefix))

    print("\n=== 판정 ===")
    if passed("5a") and passed("5b"):
        print("  SMB 직접 읽기·쓰기 모두 가능 → 원안(공유 폴더 라이브 DB) 그대로 진행")
    elif passed("6"):
        print("  SMB 직접 쓰기는 불가, 읽기는 가능")
        print("  → 구조 확정: 마스터는 로컬에서 인덱싱 후 공유 폴더로 복사 배포,")
        print("     사용자는 SMB에서 직접 읽기 (버전 스왑과 자연 결합)")
    elif passed("7"):
        print("  SMB 위 LanceDB 읽기/쓰기 모두 불가, 파일 복사는 가능")
        print("  → 구조 확정: 공유 폴더는 배포 채널로만 사용,")
        print("     사용자 앱이 DB를 로컬 캐시로 복사 후 오픈 (검색은 오히려 더 빠름)")
    else:
        print("  모든 구조 불가 — 기존 로직(각자 로컬 인덱싱) 유지 권장")
    return 0


if __name__ == "__main__":
    sys.exit(main())

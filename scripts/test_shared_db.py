"""공유 폴더 벡터DB(5b) 사전 기술 검증 스크립트.

사내 PC에서 실행해 공유 폴더가 공용 LanceDB를 감당할 수 있는지 확인한다.

사용법:
    python scripts/test_shared_db.py "K:\\공용\\우리파트"

검증 항목:
    1. 폴더 접근·목록 조회
    2. 파일 쓰기/읽기/삭제
    3. 하위 폴더 생성/삭제
    4. 원자적 교체 (current.txt 스왑 방식의 핵심)
    5. LanceDB 생성·저장·벡터검색
    6. 두 번째 연결로 읽기 (다른 사용자 시뮬레이션)

모든 산출물은 <대상폴더>/_aegisdesk_test/ 아래에만 만들고 끝나면 삭제한다.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

VECTOR_DIM = 1024  # rag/embedding.py 와 동일 차원

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """검증 결과를 기록하고 즉시 출력한다."""
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    results.append((name, ok, detail))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    target = Path(sys.argv[1])
    print(f"\n=== 공유 폴더 벡터DB 사전 검증: {target} ===\n")

    # ── 1. 폴더 접근 ──────────────────────────────────────────────
    try:
        entries = os.listdir(target)
        check("1. 폴더 접근·목록 조회", True, f"{len(entries)}개 항목")
    except OSError as exc:
        check("1. 폴더 접근·목록 조회", False, str(exc))
        print("\n폴더 자체에 접근이 안 됩니다. EFSS 드라이브(M: 계열)라면 구조적으로 불가합니다.")
        return 1

    test_dir = target / "_aegisdesk_test"

    try:
        # ── 2. 하위 폴더 생성 ─────────────────────────────────────
        try:
            test_dir.mkdir(exist_ok=True)
            check("2. 하위 폴더 생성", True, str(test_dir))
        except OSError as exc:
            check("2. 하위 폴더 생성", False, str(exc))
            print("\n쓰기 권한이 없습니다. 파트장/IT에 폴더 쓰기 권한을 확인하세요.")
            return 1

        # ── 3. 파일 쓰기/읽기/삭제 ────────────────────────────────
        try:
            f = test_dir / "write_test.txt"
            f.write_text("aegis desk write test", encoding="utf-8")
            ok = f.read_text(encoding="utf-8") == "aegis desk write test"
            f.unlink()
            check("3. 파일 쓰기/읽기/삭제", ok)
        except OSError as exc:
            check("3. 파일 쓰기/읽기/삭제", False, str(exc))
            return 1

        # ── 4. 원자적 교체 (버전 스왑의 핵심) ─────────────────────
        try:
            cur = test_dir / "current.txt"
            cur.write_text("db_v1", encoding="utf-8")
            tmp = test_dir / "current.tmp"
            tmp.write_text("db_v2", encoding="utf-8")
            os.replace(tmp, cur)  # 스왑과 동일한 연산
            ok = cur.read_text(encoding="utf-8") == "db_v2"
            check("4. 원자적 교체(os.replace)", ok)
        except OSError as exc:
            check("4. 원자적 교체(os.replace)", False, str(exc))

        # ── 5. LanceDB 생성·저장·검색 ─────────────────────────────
        try:
            import lancedb
            import pyarrow as pa
        except ImportError as exc:
            check("5. LanceDB 생성·검색", False, f"패키지 미설치: {exc}")
            print("\nlancedb/pyarrow가 설치된 환경(프로그램 실행 환경)에서 다시 실행하세요.")
            return 1

        try:
            import random
            db_dir = test_dir / "db_v1"
            db = lancedb.connect(str(db_dir))
            schema = pa.schema([
                pa.field("chunk_id", pa.string()),
                pa.field("file_path", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
                pa.field("is_deleted", pa.bool_()),
            ])
            table = db.create_table("chunks", schema=schema)
            rows = [
                {
                    "chunk_id": f"test-{i}",
                    "file_path": f"{target}/dummy/doc{i}.txt",
                    "text": f"테스트 청크 {i}",
                    "vector": [random.random() for _ in range(VECTOR_DIM)],
                    "is_deleted": False,
                }
                for i in range(20)
            ]
            t0 = time.time()
            table.add(rows)
            write_sec = time.time() - t0
            check("5a. LanceDB 테이블 생성·20행 저장", True, f"{write_sec:.2f}초")

            qvec = [random.random() for _ in range(VECTOR_DIM)]
            t0 = time.time()
            hits = (
                table.search(qvec)
                .where("is_deleted = false")
                .limit(5)
                .to_arrow()
                .to_pandas()
            )
            search_sec = time.time() - t0
            check("5b. 벡터 검색", len(hits) == 5, f"{search_sec:.2f}초, {len(hits)}건")
        except Exception as exc:
            check("5. LanceDB 생성·검색", False, f"{type(exc).__name__}: {exc}")
            return 1

        # ── 6. 두 번째 연결 읽기 (다른 사용자 시뮬레이션) ─────────
        try:
            db2 = lancedb.connect(str(db_dir))
            table2 = db2.open_table("chunks")
            df2 = table2.search(qvec).limit(3).to_arrow().to_pandas()
            check("6. 별도 연결 읽기(사용자 시뮬레이션)", len(df2) == 3, f"{len(df2)}건")
        except Exception as exc:
            check("6. 별도 연결 읽기(사용자 시뮬레이션)", False, f"{type(exc).__name__}: {exc}")

    finally:
        # ── 정리 ─────────────────────────────────────────────────
        try:
            shutil.rmtree(test_dir, ignore_errors=False)
            print(f"\n정리 완료: {test_dir} 삭제됨")
        except OSError as exc:
            print(f"\n[주의] 테스트 폴더 삭제 실패 — 수동 삭제 필요: {test_dir} ({exc})")

    # ── 요약 ─────────────────────────────────────────────────────
    print("\n=== 결과 요약 ===")
    all_ok = all(ok for _, ok, _ in results)
    for name, ok, _ in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(
        "\n판정: "
        + ("공유 폴더 공용 벡터DB 구조적으로 가능 — 5b 진행 OK"
           if all_ok
           else "일부 항목 실패 — 위 FAIL 항목의 오류 메시지를 확인하세요")
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

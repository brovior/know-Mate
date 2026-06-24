"""인덱스 저장 상태 점검 스크립트.

LanceDB(chunks 테이블)를 열어 저장 건수·파일별 청크 수·벡터 차원을 출력하고,
선택한 청크의 text 컬럼을 복호화해 정상 저장 여부를 눈으로 확인한다.

사용법:
    .venv/Scripts/python.exe scripts/inspect_index.py            # 요약만
    .venv/Scripts/python.exe scripts/inspect_index.py --text 3   # 청크 3개 복호화 미리보기
"""
import argparse
import os
import sys

import lancedb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knowmate.config import get_config
from knowmate.rag.indexer import TABLE_NAME, VECTOR_DIM
from knowmate.secure.crypto import get_crypto_manager


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", type=int, default=0, help="복호화해서 미리볼 청크 수")
    args = parser.parse_args()

    db_path = os.path.join(os.environ.get("APPDATA", "."), "KnowMate", "index")
    print(f"DB 경로 : {db_path}")
    print(f"테이블  : {TABLE_NAME}\n")

    db = lancedb.connect(db_path)
    if TABLE_NAME not in db.table_names():
        print(f"[!] '{TABLE_NAME}' 테이블이 없습니다. 아직 인덱싱되지 않았습니다.")
        return

    table = db.open_table(TABLE_NAME)
    df = table.to_arrow().to_pandas()

    total = len(df)
    active = int((~df["is_deleted"]).sum())
    deleted = total - active
    print(f"총 청크 : {total} (활성 {active} / soft-deleted {deleted})")

    if total == 0:
        print("[!] 저장된 청크가 없습니다.")
        return

    # 벡터 차원 검증
    dim = len(df.iloc[0]["vector"])
    flag = "OK" if dim == VECTOR_DIM else f"불일치! 기대값 {VECTOR_DIM}"
    print(f"벡터 차원: {dim} ({flag})\n")

    print("파일별 청크 수:")
    for path, cnt in df.groupby("file_path").size().items():
        print(f"  {cnt:>4}  {path}")

    if args.text > 0:
        print(f"\n복호화 미리보기 (상위 {args.text}개):")
        crypto = get_crypto_manager(get_config())
        for _, row in df.head(args.text).iterrows():
            try:
                plain = crypto.decrypt(row["text"])
                preview = plain[:120].replace("\n", " ")
                print(f"  [{row['file_type']}|{row['chunk_index']}] {preview}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [복호화 실패] {exc}")


if __name__ == "__main__":
    main()

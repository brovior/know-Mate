"""검색이 빈 결과를 주는 원인 진단 스크립트.

실제 config·임베딩 클라이언트·LanceDB로 쿼리를 돌려, 어느 단계에서
결과가 0이 되는지(원시 히트 / 점수 / threshold / scope)를 단계별로 출력한다.

사용법:
    PYTHONUTF8=1 .venv/Scripts/python.exe scripts/diag_search.py "검색할 질문"
"""
import os
import sys

import lancedb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knowmate.config import get_config
from knowmate.rag.embedding import get_embedding_client
from knowmate.rag.indexer import TABLE_NAME


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "테스트"
    cfg = get_config()
    embed_cfg = cfg.get("embedding", {})
    search_cfg = cfg.get("search", {})
    threshold = search_cfg.get("score_threshold", 0.4)

    print(f"질의      : {query!r}")
    print(f"임베딩 모드: {embed_cfg.get('mode')}")
    print(f"threshold : {threshold}\n")

    # 1) 질의 임베딩
    client = get_embedding_client(cfg)
    vec = client.embed([query])[0]
    print(f"질의 벡터 차원: {len(vec)}")

    # 2) LanceDB 원시 검색
    db_path = os.path.join(os.environ.get("APPDATA", "."), "AegisDesk", "index")
    db = lancedb.connect(db_path)
    try:
        table = db.open_table(TABLE_NAME)
    except Exception:
        print("[!] chunks 테이블 없음")
        return

    raw = (
        table.search(vec)
        .where("is_deleted = false")
        .limit(10)
        .to_arrow()
        .to_pandas()
    )
    print(f"원시 히트 : {len(raw)}건")
    if raw.empty:
        print("[!] 벡터 검색이 0건 — 인덱스가 비었거나 벡터 컬럼 문제")
        return

    if "_distance" not in raw.columns:
        print("[!] _distance 컬럼 없음 — metric 확인 필요")
        return

    raw["score"] = 1.0 - raw["_distance"] / 2.0
    print("\n상위 히트 (거리 / 점수):")
    for _, r in raw.iterrows():
        mark = "PASS" if r["score"] >= threshold else "drop"
        fname = os.path.basename(str(r["file_path"]))
        print(f"  [{mark}] dist={r['_distance']:.4f} score={r['score']:.4f}  {fname}")

    passed = int((raw["score"] >= threshold).sum())
    print(f"\nthreshold({threshold}) 통과: {passed}/{len(raw)}건")
    if passed == 0:
        best = raw["score"].max()
        print(f"[!] 전부 탈락. 최고 점수 {best:.4f} < {threshold}")
        print("    → 임베딩 모드 불일치(인덱스와 질의가 다른 모델) 또는 threshold 과다 의심")


if __name__ == "__main__":
    main()

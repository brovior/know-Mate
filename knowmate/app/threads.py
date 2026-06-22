"""대화 스레드 저장 관리 (CLAUDE.md 6-12)."""
import json
import os
from pathlib import Path
from typing import Any

THREADS_FILE = Path(os.environ.get("APPDATA", ".")) / "KnowMate" / "threads.json"
_EMPTY: dict[str, list] = {"knowledge": [], "mes": []}


def load_threads() -> dict[str, list]:
    """threads.json을 읽어 반환한다. 없거나 손상됐으면 빈 구조를 반환한다."""
    if not THREADS_FILE.exists():
        return {"knowledge": [], "mes": []}
    try:
        return json.loads(THREADS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"knowledge": [], "mes": []}


def save_threads(data: dict[str, list]) -> None:
    """원자적 교체로 threads.json에 저장한다."""
    THREADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = THREADS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(THREADS_FILE)


def upsert_thread(mode: str, thread: dict[str, Any], max_threads: int = 50) -> None:
    """mode의 스레드 목록에 thread를 저장한다. id 기준으로 upsert하고 최신순 정렬."""
    data = load_threads()
    threads: list = data.setdefault(mode, [])
    for i, t in enumerate(threads):
        if t.get("id") == thread.get("id"):
            threads[i] = thread
            break
    else:
        threads.insert(0, thread)
    data[mode] = threads[:max_threads]
    save_threads(data)


def delete_thread(mode: str, thread_id: str) -> None:
    """mode에서 thread_id를 제거한다."""
    data = load_threads()
    data[mode] = [t for t in data.get(mode, []) if t.get("id") != thread_id]
    save_threads(data)

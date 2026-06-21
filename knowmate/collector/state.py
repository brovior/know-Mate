"""index_state.json 관리 모듈 (CLAUDE.md 6-12 원자적 교체 방식)."""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_state(state_file: Path) -> dict:
    """state_file을 읽어 dict로 반환한다. 파일이 없으면 빈 dict를 반환한다."""
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("state 파일 읽기 실패, 초기화: %s (%s)", state_file, exc)
        return {}


def save_state(state_file: Path, state: dict) -> None:
    """state를 원자적으로 교체 저장한다 (tmp 파일 작성 후 replace)."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(state_file)
    logger.debug("state 저장 완료: %s (%d건)", state_file, len(state))

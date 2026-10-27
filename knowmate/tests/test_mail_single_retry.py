"""선택 메일 재시도의 범위와 최신 UID 보호를 실제 임시 DB로 검증한다."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from knowmate.collector import failure_state, mail_scanner
from knowmate.collector.mail_discovery import MailDiscoverySnapshot
from knowmate.collector.mail_scan_state import load_mail_scan_state, save_mail_scan_state
from knowmate.rag.email_indexer import EmailIndexer, MailIndexState
from knowmate.rag.embedding import EmbeddingClient
from knowmate.secure.mysingle_reader import parse_mail_file

MailContext = tuple[Path, EmailIndexer, dict]


def _mail(path: Path, uid: str, body: str, mtime: float) -> Path:
    """합성 메일과 결정적인 파일 시각을 만든다."""
    path.write_text(
        f"From: a@example.invalid\nMessage-ID: <{uid}@example.invalid>\nSubject: synthetic\n"
        f"Content-Type: text/plain; charset=utf-8\n\n{body}", encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def context(tmp_path: Path) -> MailContext:
    """실제 파일·fake 임베딩·격리 DB와 수집 설정을 제공한다."""
    root = tmp_path / "source"
    root.mkdir()
    indexer = EmailIndexer(tmp_path / "db", EmbeddingClient(base_url="", host_header="", fake=True))
    cfg = {"collector": {"watch_folders": [str(root)], "exclude_files": []},
           "mail": {"enabled": True, "max_mails_per_scan": 500}, "chunking": {}}
    return root, indexer, cfg


def _scan(root: Path, indexer: EmailIndexer, cfg: dict, **kwargs: object) -> tuple[int, int]:
    """임시 상태 경로로 메일 스캔을 실행한다."""
    return mail_scanner.run_mail_scan(
        [str(root)], indexer, cfg, state_file=root.parent / "mail_state.json",
        failure_file=root.parent / "failures.json", **kwargs,
    )


def _rows(indexer: EmailIndexer) -> list[dict]:
    """본문·벡터 없이 저장 세대 식별 메타데이터만 가져온다."""
    return indexer.table.search().select(["chunk_id", "source_file", "mail_uid", "mtime"]).to_arrow().to_pylist()


def test_single_retry_leaves_other_mail_cursor_pending_and_discovery_unchanged(
    context: MailContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """한 파일을 처리해도 다른 메일과 전체 스캔 상태는 바뀌지 않는다."""
    root, indexer, cfg = context
    selected = _mail(root / "selected.eml", "selected", "selected synthetic body", 1000)
    other = _mail(root / "other.eml", "other", "other synthetic body", 2000)
    state_file = root.parent / "mail_state.json"
    state = load_mail_scan_state(state_file)
    state["cursor"] = {"mtime": 999.0, "path": "c:/unrelated.eml"}
    state["pending_deletes"] = ["unrelated-pending-id"]
    save_mail_scan_state(state_file, state)
    discovery = MailDiscoverySnapshot()
    discovery.items = [{"path": str(other)}]
    discovery.signature = ("retained",)
    discovery.refreshed_at = 20.0
    discovery_before = copy.deepcopy(discovery.__dict__)
    parsed_paths = []
    real_parse = parse_mail_file

    def parse_selected(path: str) -> dict:
        """비대상 파일이 열리지 않았는지 기록한다."""
        parsed_paths.append(path)
        return real_parse(path)

    monkeypatch.setattr("knowmate.secure.mysingle_reader.parse_mail_file", parse_selected)
    monkeypatch.setattr(mail_scanner, "_iter_scanned_mail_items", lambda *_a, **_k: pytest.fail("global discovery"))
    assert _scan(root, indexer, cfg, target_path=str(selected), discovery_snapshot=discovery)[0] == 1
    assert parsed_paths == [str(selected)]
    assert {row["source_file"] for row in _rows(indexer)} == {str(selected.resolve())}
    after = load_mail_scan_state(state_file)
    assert after["cursor"] == state["cursor"]
    assert after["pending_deletes"] == state["pending_deletes"]
    assert discovery.__dict__ == discovery_before
    assert indexer.last_mail_scan_completion.value == "UNKNOWN"


@pytest.mark.parametrize("cache_state", ["absent", "stale", "deleted_source"])
def test_old_selected_copy_preserves_newer_uid_without_cache_proof(context: MailContext, cache_state: str) -> None:
    """캐시가 없거나 손상돼도 DB의 최신 메일 세대를 되돌리지 않는다."""
    root, indexer, cfg = context
    latest = _mail(root / "latest.eml", "shared", "newer body", 2000)
    selected = _mail(root / "old.eml", "shared", "older different body", 1000)
    indexer.index_mail(parse_mail_file(str(latest)), 2000)
    if cache_state == "stale":
        state = load_mail_scan_state(root.parent / "mail_state.json")
        state["files"][str(latest).replace("\\", "/").casefold()] = {"mtime": 1, "size": 1,
            "mail_uid": "eml:<shared@example.invalid>", "index_version": "old", "uid_resolution_version": 1}
        save_mail_scan_state(root.parent / "mail_state.json", state)
    elif cache_state == "deleted_source":
        latest.unlink()
    before = _rows(indexer)
    assert _scan(root, indexer, cfg, target_path=str(selected)) == (0, 1)
    assert _rows(indexer) == before


def test_equal_timestamp_different_source_is_deferred(context: MailContext) -> None:
    """내용을 메타데이터로 판별할 수 없는 동시각 충돌은 변경하지 않는다."""
    root, indexer, cfg = context
    first = _mail(root / "first.eml", "shared", "first body", 1000)
    selected = _mail(root / "selected.eml", "shared", "different body", 1000)
    indexer.index_mail(parse_mail_file(str(first)), 1000)
    before = _rows(indexer)
    assert _scan(root, indexer, cfg, target_path=str(selected)) == (0, 1)
    assert _rows(indexer) == before
    assert str(selected) in failure_state.load_failures(root.parent / "failures.json")


@pytest.mark.parametrize("damage", ["incomplete", "legacy_version", "nonfinite", "pending_part"])
def test_uncertain_uid_generation_does_not_get_replaced(context: MailContext, damage: str) -> None:
    """불완전하거나 예약 삭제로 잘린 세대를 선택 재시도가 덮어쓰지 않는다."""
    root, indexer, cfg = context
    latest = _mail(root / "latest.eml", "shared", "newer body", 2000)
    selected = _mail(root / "selected.eml", "shared", "older body", 1000)
    if damage == "pending_part":
        indexer._chunk_size, indexer._overlap = 10, 0
    ids = indexer.index_mail(parse_mail_file(str(latest)), 2000)
    if damage == "incomplete":
        indexer.table.update(values={"chunk_total": 2})
    elif damage == "legacy_version":
        indexer.table.update(values={"source_meta": json.dumps({"_index_version": "old"})})
    elif damage == "nonfinite":
        indexer.table.update(values_sql={"mtime": "CAST('Infinity' AS DOUBLE)"})
    else:
        assert len(ids) > 1
        state = load_mail_scan_state(root.parent / "mail_state.json")
        state["pending_deletes"] = [ids[0]]
        save_mail_scan_state(root.parent / "mail_state.json", state)
    before = _rows(indexer)
    assert ids
    assert _scan(root, indexer, cfg, target_path=str(selected)) == (0, 1)
    assert _rows(indexer) == before


def test_target_can_replace_older_complete_generation(context: MailContext) -> None:
    """선택 파일이 더 최신이면 새 저장 후 정확한 이전 세대만 교체한다."""
    root, indexer, cfg = context
    older = _mail(root / "older.eml", "shared", "old body", 1000)
    selected = _mail(root / "selected.eml", "shared", "new body", 2000)
    indexer.index_mail(parse_mail_file(str(older)), 1000)
    assert _scan(root, indexer, cfg, target_path=str(selected))[0] == 1
    assert {row["mtime"] for row in _rows(indexer)} == {2000}


@pytest.mark.parametrize("options", [{"enabled": False}, {"max_mails_per_scan": 0}])
def test_disabled_selected_mail_has_no_side_effects(context: MailContext, options: dict) -> None:
    """개별 버튼으로 비활성화 또는 처리량0 설정을 우회하지 않는다."""
    root, indexer, cfg = context
    selected = _mail(root / "selected.eml", "one", "body", 1000)
    cfg["mail"].update(options)
    rejected = []
    assert _scan(root, indexer, cfg, target_path=str(selected), on_target_rejected=rejected.append) == (0, 0)
    assert rejected == ["unavailable"]
    assert _rows(indexer) == []
    assert not (root.parent / "mail_state.json").exists()


def test_handed_failure_map_does_not_reload_stale_disk(context: MailContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """문서 저장 실패 뒤 메일 단계가 canonical map을 disk로 되돌리지 않는다."""
    root, indexer, cfg = context
    selected = _mail(root / "selected.eml", "one", "body", 1000)
    canonical = {}
    failure_state.note_failure(canonical, "other-document.txt", failure_state.KIND_UNKNOWN_TRANSIENT,
                               "read", None, 1, 1, 1)
    canonical_before = dict(canonical)
    calls = []

    def persist() -> bool:
        """canonical map을 전달받은 worker 저장 실패를 모사한다."""
        calls.append(dict(canonical))
        return False

    failure_state.note_failure(canonical, str(selected), failure_state.KIND_UNKNOWN_TRANSIENT,
                               "parse", None, 1, 1, 1)
    monkeypatch.setattr(failure_state, "load_failures", lambda *_a: pytest.fail("stale disk reload"))
    assert _scan(root, indexer, cfg, target_path=str(selected), preloaded_failures=canonical,
                 persist_failures=persist)[0] == 1
    assert canonical == canonical_before
    assert calls == [canonical_before]

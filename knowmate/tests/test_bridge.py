"""Bridge 건수 조회 로직(knowmate/app/bridge.py) 단위 테스트.

배경: getIndexStatus/_on_worker_finished가 이전에는 chunks·emails 테이블
**전체**(1024차원 벡터·AES 암호화 원문 포함)를 `table.to_arrow().to_pandas()`로
로드했다 — 유휴 자동 인덱싱이 60초마다 도는 동안 변경 파일이 0건이어도 매번
호출돼 상주 메모리가 계속 쌓이는 원인이었다(A-0002가 purge에서 고친 것과 같은
안티패턴이 다른 위치에 있었음). 이 테스트는 ① 필요한 컬럼만 projection 조회하는지,
② 변경이 없던 사이클은 DB를 아예 열지 않는지를 검증한다.
"""
import pyarrow as pa
import pytest

from knowmate.app.bridge import Bridge


def _chunks_arrow(rows: list[dict]) -> pa.Table:
    """chunks 테이블 projection 조회 결과를 흉내내는 Arrow 테이블."""
    return pa.table({
        "file_path": pa.array([r["file_path"] for r in rows], type=pa.string()),
        "scope": pa.array([r["scope"] for r in rows], type=pa.string()),
        "is_deleted": pa.array([r["is_deleted"] for r in rows], type=pa.bool_()),
        **({"indexed_at": pa.array([r.get("indexed_at", "") for r in rows], type=pa.string())}
           if rows and "indexed_at" in rows[0] else {}),
    })


def _emails_arrow(rows: list[dict]) -> pa.Table:
    return pa.table({
        "mail_uid": pa.array([r["mail_uid"] for r in rows], type=pa.string()),
        "is_deleted": pa.array([r["is_deleted"] for r in rows], type=pa.bool_()),
    })


class _FakeQueryBuilder:
    """table.search()의 반환값 — select(cols)만 지원하고, 요청한 컬럼만 돌려준다."""

    def __init__(self, arrow_table: pa.Table):
        self._arrow_table = arrow_table
        self.requested_columns: list[str] | None = None

    def select(self, columns: list[str]):
        self.requested_columns = list(columns)
        self._selected = self._arrow_table.select(columns)
        return self

    def to_arrow(self) -> pa.Table:
        return self._selected


class _FakeTable:
    """LanceDB 테이블 대역 — `.to_arrow()`(전체 로드)를 호출하면 즉시 실패시켜
    회귀를 잡는다. projection 경로(`search().select().to_arrow()`)만 지원한다."""

    def __init__(self, arrow_table: pa.Table):
        self._arrow_table = arrow_table
        self.last_query_builder: _FakeQueryBuilder | None = None

    def search(self):
        self.last_query_builder = _FakeQueryBuilder(self._arrow_table)
        return self.last_query_builder

    def to_arrow(self):
        raise AssertionError(
            "table.to_arrow()가 select() 없이 직접 호출됨 — 전체 테이블(벡터·암호문 포함) "
            "로드로 회귀했다. search().select([...]).to_arrow()를 사용해야 한다."
        )

    def to_pandas(self):
        raise AssertionError("table.to_pandas() 직접 호출 금지(원칙10) — search().select() 경유해야 함")


class _FakeIndexer:
    def __init__(self, arrow_table: pa.Table):
        self.table = _FakeTable(arrow_table)


class _FakeWorker:
    """CollectorWorker 대역. last_cycle_changed로 스킵 판정을 제어한다."""

    def __init__(self, chunks_rows, email_rows, last_cycle_changed=True, running=False):
        self._indexer = _FakeIndexer(_chunks_arrow(chunks_rows))
        self._email_indexer = _FakeIndexer(_emails_arrow(email_rows)) if email_rows is not None else None
        self.last_cycle_changed = last_cycle_changed
        self._running = running

    def isRunning(self):
        return self._running


def _make_bridge(worker) -> Bridge:
    bridge = Bridge()
    bridge._worker = worker
    return bridge


_SAMPLE_CHUNKS = [
    {"file_path": "a.docx", "scope": "local", "is_deleted": False, "indexed_at": "2026-07-24T01:00:00+00:00"},
    {"file_path": "a.docx", "scope": "local", "is_deleted": False, "indexed_at": "2026-07-24T01:00:00+00:00"},  # 같은 파일 2청크
    {"file_path": "b.xlsx", "scope": "shared", "is_deleted": False, "indexed_at": "2026-07-24T02:00:00+00:00"},
    {"file_path": "c.docx", "scope": "local", "is_deleted": True, "indexed_at": "2026-07-24T03:00:00+00:00"},  # 삭제됨(제외)
]
_SAMPLE_MAILS = [
    {"mail_uid": "knox:1", "is_deleted": False},
    {"mail_uid": "knox:2", "is_deleted": False},
    {"mail_uid": "knox:3", "is_deleted": True},
]


class TestComputeDocMailCountsUsesProjection:
    def test_only_requests_needed_columns_without_indexed_at(self):
        worker = _FakeWorker(_SAMPLE_CHUNKS, _SAMPLE_MAILS)
        bridge = _make_bridge(worker)

        bridge._compute_doc_mail_counts(want_last_indexed=False)

        cols = worker._indexer.table.last_query_builder.requested_columns
        assert set(cols) == {"file_path", "scope", "is_deleted"}
        assert "vector" not in cols and "text" not in cols

    def test_requests_indexed_at_when_needed(self):
        worker = _FakeWorker(_SAMPLE_CHUNKS, _SAMPLE_MAILS)
        bridge = _make_bridge(worker)

        bridge._compute_doc_mail_counts(want_last_indexed=True)

        cols = worker._indexer.table.last_query_builder.requested_columns
        assert "indexed_at" in cols

    def test_email_projection_excludes_vector_and_text(self):
        worker = _FakeWorker(_SAMPLE_CHUNKS, _SAMPLE_MAILS)
        bridge = _make_bridge(worker)

        bridge._compute_doc_mail_counts()

        cols = worker._email_indexer.table.last_query_builder.requested_columns
        assert set(cols) == {"mail_uid", "is_deleted"}

    def test_counts_unique_file_paths_excluding_deleted(self):
        worker = _FakeWorker(_SAMPLE_CHUNKS, _SAMPLE_MAILS)
        bridge = _make_bridge(worker)

        local_count, shared_count, mail_count, _ = bridge._compute_doc_mail_counts()

        assert local_count == 1   # a.docx만(c.docx는 is_deleted=True로 제외)
        assert shared_count == 1  # b.xlsx
        assert mail_count == 2    # knox:1, knox:2(knox:3은 삭제됨)

    def test_last_indexed_derived_from_max_indexed_at(self):
        worker = _FakeWorker(_SAMPLE_CHUNKS, _SAMPLE_MAILS)
        bridge = _make_bridge(worker)

        *_counts, last_indexed = bridge._compute_doc_mail_counts(want_last_indexed=True)

        assert last_indexed != ""

    def test_full_table_load_would_raise(self):
        """회귀 방지: 대역 테이블이 to_arrow()/to_pandas() 직접 호출을 예외로 잡는지 자체 검증."""
        worker = _FakeWorker(_SAMPLE_CHUNKS, _SAMPLE_MAILS)
        with pytest.raises(AssertionError):
            worker._indexer.table.to_arrow()


class TestComputeDocMailCountsFallback:
    def test_query_exception_falls_back_to_previous_cached_values(self):
        """일시적 조회 실패로 화면 숫자가 0으로 튀지 않고 직전 값을 유지해야 한다."""
        worker = _FakeWorker(_SAMPLE_CHUNKS, _SAMPLE_MAILS)
        bridge = _make_bridge(worker)
        bridge._compute_doc_mail_counts()  # 정상 1회 계산해 캐시를 채운다
        prev_local, prev_shared, prev_mail = bridge._local_count, bridge._shared_count, bridge._mail_count
        assert prev_local > 0

        class _BoomTable:
            def search(self):
                raise RuntimeError("DB 조회 실패(시뮬레이션)")

        worker._indexer.table = _BoomTable()
        worker._email_indexer.table = _BoomTable()

        local_count, shared_count, mail_count, _ = bridge._compute_doc_mail_counts()

        assert (local_count, shared_count, mail_count) == (prev_local, prev_shared, prev_mail)

    def test_no_indexer_attribute_returns_zero_defaults(self):
        bridge = Bridge()
        bridge._worker = None

        local_count, shared_count, mail_count, last_indexed = bridge._compute_doc_mail_counts()

        assert (local_count, shared_count, mail_count) == (0, 0, 0)


class TestOnWorkerFinishedSkipsWhenUnchanged:
    def test_recomputes_when_last_cycle_changed_true(self):
        worker = _FakeWorker(_SAMPLE_CHUNKS, _SAMPLE_MAILS, last_cycle_changed=True)
        bridge = _make_bridge(worker)

        bridge._on_worker_finished("인덱싱 완료 - 처리 3건")

        assert worker._indexer.table.last_query_builder is not None  # DB 조회 발생
        assert bridge._doc_count == 2

    def test_skips_db_query_when_last_cycle_changed_false(self):
        worker = _FakeWorker(_SAMPLE_CHUNKS, _SAMPLE_MAILS, last_cycle_changed=False)
        bridge = _make_bridge(worker)
        # 이전 사이클에서 이미 값을 채워둔 상태를 흉내낸다
        bridge._local_count, bridge._shared_count, bridge._mail_count = 5, 2, 3
        bridge._doc_count = 7

        bridge._on_worker_finished("인덱싱 완료 - 처리 0건")

        assert worker._indexer.table.last_query_builder is None  # DB 조회가 아예 없었음
        assert bridge._doc_count == 7  # 직전 값 유지

    def test_missing_last_cycle_changed_attribute_defaults_to_recompute(self):
        """구버전 워커(테스트 더블 등)가 이 속성을 아직 안 가진 경우, 안전한 방향으로
        재계산한다(스킵을 기본값으로 삼지 않는다)."""
        worker = _FakeWorker(_SAMPLE_CHUNKS, _SAMPLE_MAILS)
        del worker.last_cycle_changed
        bridge = _make_bridge(worker)

        bridge._on_worker_finished("인덱싱 완료")

        assert worker._indexer.table.last_query_builder is not None


class _FakeDeleteTable:
    """delete()만 기록하는 indexer.table 대역 (excludeFile 청크 삭제 검증용)."""

    def __init__(self):
        self.deleted_where: list[str] = []

    def delete(self, where: str) -> None:
        self.deleted_where.append(where)


class _FakeIndexerForDelete:
    def __init__(self):
        self.table = _FakeDeleteTable()


class _FakeWorkerForFailures:
    """실패 파일 관리 슬롯(getFailures/retryFile/excludeFile) 테스트용 워커 대역."""

    def __init__(self, failure_file, state_file, running=False):
        self._failure_file = failure_file
        self._state_file = state_file
        self._indexer = _FakeIndexerForDelete()
        self._running = running
        self._get_now = lambda: 1_700_000_000.0

    def isRunning(self):
        return self._running

    def start(self):
        self._running = True


class TestFailureManagementSlots:
    def _patch_collector_config(self, monkeypatch, exclude_files=None):
        """config.get_config/update_exclude_files를 인메모리 dict로 대체한다
        (%APPDATA% 실제 파일을 건드리지 않기 위해)."""
        import knowmate.config as config_module
        state = {"collector": {"exclude_files": list(exclude_files or [])}}
        monkeypatch.setattr(config_module, "get_config", lambda: state)

        def _update(paths):
            state["collector"]["exclude_files"] = paths
        monkeypatch.setattr(config_module, "update_exclude_files", _update)
        return state

    def test_get_failures_reports_pending_record(self, tmp_path, monkeypatch):
        from knowmate.collector import failure_state

        failure_file = tmp_path / "index_failure.json"
        records = {}
        failure_state.note_failure(
            records, str(tmp_path / "broken.xlsx"), failure_state.KIND_NEEDS_USER_ACTION,
            "open", None, mtime=100.0, size=10, now=1_699_999_000.0,
        )
        failure_state.save_failures(failure_file, records)
        self._patch_collector_config(monkeypatch)

        worker = _FakeWorkerForFailures(failure_file, tmp_path / "state.json")
        bridge = _make_bridge(worker)

        cards = __import__("json").loads(bridge.getFailures())

        assert len(cards) == 1
        assert cards[0]["kind"] == "NEEDS_USER_ACTION"
        assert cards[0]["excluded"] is False
        assert cards[0]["next_retry_ts"] is not None  # 7일 안전밸브 — 영구 무시 아님

    def test_get_failures_escalation_matches_backoff_computation(self, tmp_path, monkeypatch):
        """6a AC-4: bridge.getFailures()의 escalation 필드가 backoff_seconds()가
        실제로 쓰는 것과 같은 escalation_state() 호출 결과여야 한다 — 판정 로직이
        두 곳에서 갈라지면 화면과 실제 대기 시간이 어긋난다(2차 리뷰 M-2)."""
        from knowmate.collector import failure_state

        failure_file = tmp_path / "index_failure.json"
        target = str(tmp_path / "broken.xlsx")
        records = {}
        for i in range(3):
            failure_state.note_failure(
                records, target, failure_state.KIND_OPEN_ERROR, "open", None,
                mtime=100.0, size=10, now=1_699_999_000.0 + i,
            )
        failure_state.save_failures(failure_file, records)
        self._patch_collector_config(monkeypatch)

        worker = _FakeWorkerForFailures(failure_file, tmp_path / "state.json")
        bridge = _make_bridge(worker)

        cards = __import__("json").loads(bridge.getFailures())

        assert cards[0]["consecutive_failures"] == 3
        assert cards[0]["escalation"] == "NEEDS_ACTION"

    def test_get_failures_marks_excluded_files(self, tmp_path, monkeypatch):
        from knowmate.collector import failure_state

        failure_file = tmp_path / "index_failure.json"
        target = str(tmp_path / "temp.xlsx")
        records = {}
        failure_state.note_failure(
            records, target, failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=1.0, size=1, now=1_699_999_000.0,
        )
        failure_state.save_failures(failure_file, records)
        self._patch_collector_config(monkeypatch, exclude_files=[target])

        worker = _FakeWorkerForFailures(failure_file, tmp_path / "state.json")
        bridge = _make_bridge(worker)

        cards = __import__("json").loads(bridge.getFailures())

        assert len(cards) == 1
        assert cards[0]["excluded"] is True
        assert cards[0]["next_retry_ts"] is None

    def test_retry_file_sets_force_retry_and_starts_worker(self, tmp_path, monkeypatch):
        from knowmate.collector import failure_state

        failure_file = tmp_path / "index_failure.json"
        target = str(tmp_path / "broken.xlsx")
        records = {}
        failure_state.note_failure(
            records, target, failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
            mtime=1.0, size=1, now=1000.0,
        )
        failure_state.save_failures(failure_file, records)

        worker = _FakeWorkerForFailures(failure_file, tmp_path / "state.json")
        bridge = _make_bridge(worker)

        result = bridge.retryFile(target)

        assert result == "ok"
        assert worker.isRunning() is True
        assert failure_state.load_failures(failure_file)[target].force_retry is True

    def test_retry_file_refuses_when_worker_running(self, tmp_path):
        worker = _FakeWorkerForFailures(tmp_path / "f.json", tmp_path / "s.json", running=True)
        bridge = _make_bridge(worker)

        result = bridge.retryFile(str(tmp_path / "x.xlsx"))

        assert result == "busy"

    def test_exclude_file_adds_to_config_and_deletes_chunks(self, tmp_path, monkeypatch):
        state = self._patch_collector_config(monkeypatch)
        worker = _FakeWorkerForFailures(tmp_path / "f.json", tmp_path / "s.json")
        bridge = _make_bridge(worker)
        target = str(tmp_path / "excluded.xlsx")

        result = bridge.excludeFile(target)

        assert result == "ok"
        assert target in state["collector"]["exclude_files"]
        assert len(worker._indexer.table.deleted_where) == 1
        assert target in worker._indexer.table.deleted_where[0]

    def test_exclude_file_is_idempotent(self, tmp_path, monkeypatch):
        target = str(tmp_path / "excluded.xlsx")
        state = self._patch_collector_config(monkeypatch, exclude_files=[target])
        worker = _FakeWorkerForFailures(tmp_path / "f.json", tmp_path / "s.json")
        bridge = _make_bridge(worker)

        bridge.excludeFile(target)

        assert state["collector"]["exclude_files"].count(target) == 1

    def test_unexclude_file_removes_from_config(self, tmp_path, monkeypatch):
        target = str(tmp_path / "excluded.xlsx")
        state = self._patch_collector_config(monkeypatch, exclude_files=[target])
        worker = _FakeWorkerForFailures(tmp_path / "f.json", tmp_path / "s.json")
        bridge = _make_bridge(worker)

        result = bridge.unexcludeFile(target)

        assert result == "ok"
        assert target not in state["collector"]["exclude_files"]

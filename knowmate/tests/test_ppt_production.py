"""Production PPT safety, unchanged text and frozen deadlines without Office."""
from __future__ import annotations
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from knowmate.secure import com_reader
from knowmate.secure.ppt_operation import PptOperation, PptOperationError, PptPreparationPending
from knowmate.secure.ppt_payload import PptPayloadError, decode_slide, fatal_hresult


class Collection(list):
    @property
    def Count(self):
        return len(self)


def shape(text):
    return SimpleNamespace(Type=1, HasTable=False, HasTextFrame=True,
                           TextFrame=SimpleNamespace(TextRange=SimpleNamespace(Text=text)))


def reply(text="candidate", **updates):
    value = dict(schema=1, slide=1, counts=dict(shapes=1, groups=0, tables=0, cells=0, errors=0),
                 nodes=[dict(kind="text", text=text)], seconds=0.01, read_errors=[], errors_omitted=0)
    value.update(updates)
    return json.dumps(value)


@pytest.fixture
def office(tmp_path, monkeypatch):
    path = tmp_path / "original.ppt"
    path.write_bytes(b"not a document body")
    prs = SimpleNamespace(Slides=Collection([SimpleNamespace(Shapes=Collection([shape("baseline")]))]), Close=Mock())
    app = SimpleNamespace(Presentations=SimpleNamespace(Open=Mock(return_value=prs)))
    service = SimpleNamespace(ProtocolVersion=lambda: 3, ReaderVersion=lambda: "1.0.4.0", ExtractSlide=Mock(return_value=reply()))
    app.COMAddIns = SimpleNamespace(Item=Mock(return_value=SimpleNamespace(Connect=True, Object=service)))
    monkeypatch.setattr(com_reader, "_get_ppt_app", lambda: app)
    monkeypatch.setattr(com_reader._tls, "ppt_service", None, raising=False)
    monkeypatch.setattr(com_reader._tls, "ppt", app, raising=False)
    return path, prs, app, service


def test_addin_is_queried_once_per_exact_app_reference(office):
    path, prs, app, service = office
    reader = com_reader.PowerPointComReader("auto")
    assert reader.parse(str(path)) == "candidate"
    assert reader.parse(str(path)) == "candidate"
    app.COMAddIns.Item.assert_called_once_with("AegisDesk.PptReader")
    assert app.Presentations.Open.call_count == prs.Close.call_count == 2


def test_disabled_service_uses_com_without_enabling_it(office):
    path, prs, app, service = office
    disabled = SimpleNamespace(Connect=False, Object=service)
    app.COMAddIns.Item.return_value = disabled
    assert com_reader.PowerPointComReader("auto").parse(str(path)) == "baseline"
    assert disabled.Connect is False
    service.ExtractSlide.assert_not_called()
    with pytest.raises(PptPreparationPending):
        com_reader.PowerPointComReader("addin").parse(str(path))
    assert app.Presentations.Open.call_count == 1


def test_invalid_mode_selects_com_and_never_calls_service(office):
    path, prs, app, service = office
    assert com_reader.PowerPointComReader("garbage").parse(str(path)) == "baseline"
    app.COMAddIns.Item.assert_not_called()


def test_general_failure_falls_back_once_in_the_same_open_document(office):
    path, prs, app, service = office
    service.ExtractSlide.side_effect = RuntimeError("PRIVATE_CONTENT")
    assert com_reader.PowerPointComReader("auto").parse(str(path)) == "baseline"
    app.Presentations.Open.assert_called_once()
    service.ExtractSlide.assert_called_once()
    prs.Close.assert_called_once()


def test_partial_candidate_discarded_and_strict_mode_rejects(office):
    path, prs, app, service = office
    service.ExtractSlide.return_value = reply("PARTIAL", counts=dict(shapes=1, groups=0, tables=0, cells=0, errors=1),
        read_errors=[dict(stage="text_read", kind="com", shape="1", row=0, column=0, hresult=0x80004005)])
    assert com_reader.PowerPointComReader("auto").parse(str(path)) == "baseline"
    with pytest.raises(PptPayloadError):
        com_reader.PowerPointComReader("addin").parse(str(path))


def wrapped_rpc():
    exc = RuntimeError("SECRET")
    exc.hresult = 0x80020009
    exc.excepinfo = (0, "SECRET", "probe_extract_failed:text_read:com:800706BA", None, 0, 0x80004005)
    return exc


def test_wrapped_rpc_never_falls_back_or_closes_and_invalidates_service(office):
    path, prs, app, service = office
    service.ExtractSlide.side_effect = wrapped_rpc()
    assert fatal_hresult(wrapped_rpc()) == 0x800706BA
    with pytest.raises(com_reader.OfficeComPoisonError):
        com_reader.PowerPointComReader("auto").parse(str(path))
    prs.Close.assert_not_called()
    assert com_reader._tls.ppt is None and com_reader._tls.ppt_service is None


def test_fatal_close_invalidates_tls_and_no_body_is_logged(office, caplog):
    path, prs, app, service = office
    prs.Close.side_effect = wrapped_rpc()
    with caplog.at_level("DEBUG"), pytest.raises(com_reader.OfficeComPoisonError):
        com_reader.PowerPointComReader("auto").parse(str(path))
    assert com_reader._tls.ppt is None and com_reader._tls.ppt_service is None
    assert "SECRET" not in caplog.text


def test_normal_close_failure_is_not_a_success(office):
    path, prs, app, service = office
    prs.Close.side_effect = RuntimeError("SECRET")
    operation = PptOperation(str(path), None)
    with pytest.raises(PptOperationError, match="close_unconfirmed"):
        com_reader.PowerPointComReader("com", operation=operation).parse(str(path))
    assert operation.outcome.close_ok is False
    with pytest.raises(PptOperationError):
        operation.validate_commit()


@pytest.mark.parametrize("mode", ["auto", "com", "addin"])
def test_preparation_pending_never_dispatches(office, mode):
    path, prs, app, service = office
    with pytest.raises(PptPreparationPending):
        com_reader.PowerPointComReader(mode, "ppt_pending").parse(str(path))
    app.Presentations.Open.assert_not_called()


def test_swallowed_com_shape_errors_are_not_partial_success(office):
    path, prs, app, service = office
    class Broken:
        Type = 1
        HasTextFrame = False
        @property
        def HasTable(self):
            raise RuntimeError("SECRET")
    prs.Slides[0].Shapes = Collection([shape("valid partial"), Broken()])
    with pytest.raises(PptPayloadError, match="partial_com_read"):
        com_reader.PowerPointComReader("com").parse(str(path))
    prs.Close.assert_called_once()


def test_empty_slide_is_skipped_in_both_production_paths(office):
    path, prs, app, service = office
    prs.Slides = Collection([SimpleNamespace(Shapes=Collection()), SimpleNamespace(Shapes=Collection([shape("baseline")]))])
    service.ExtractSlide.side_effect = [reply(""), reply("baseline", slide=2)]
    assert com_reader.PowerPointComReader("addin").parse(str(path)) == "baseline"
    assert com_reader.PowerPointComReader("com").parse(str(path)) == "baseline"


@pytest.mark.parametrize("late", ["success", "general_error", "close"])
def test_deadline_does_not_depend_on_killed_or_watchdog_fired(office, late):
    path, prs, app, service = office
    now = [0.0]
    operation = PptOperation(str(path), 5, clock=lambda: now[0])
    def extract(*args):
        if late != "close":
            now[0] = 6
        if late == "general_error":
            raise RuntimeError("SECRET")
        return reply()
    service.ExtractSlide.side_effect = extract
    if late == "close":
        prs.Close.side_effect = lambda: now.__setitem__(0, 6)
    with pytest.raises(PptOperationError, match="deadline_expired"):
        com_reader.PowerPointComReader("auto", operation=operation).parse(str(path))
    assert operation.outcome.expired and service.ExtractSlide.call_count == 1
    prs.Close.assert_called_once()


def test_long_embedding_does_not_reopen_frozen_com_deadline(office):
    path, prs, app, service = office
    now = [0.0]
    operation = PptOperation(str(path), 5, clock=lambda: now[0])
    com_reader.PowerPointComReader("auto", operation=operation).parse(str(path))
    frozen = operation.outcome
    now[0] = 900
    operation.validate_commit()
    assert operation.outcome is frozen and not frozen.expired
    next_file = PptOperation(str(path), 5, clock=lambda: now[0])
    assert next_file.deadline == 905


@pytest.mark.parametrize("change", ["cancel", "source"])
def test_commit_guard_rechecks_after_embedding_without_publishing(office, change):
    from knowmate.rag.indexer import Indexer
    from knowmate.secure.crypto import FakeCryptoManager
    from knowmate.rag.embedding import VECTOR_DIM
    path, prs, app, service = office
    cancelled = [False]
    operation = PptOperation(str(path), None, cancelled=lambda: cancelled[0])
    text = com_reader.PowerPointComReader("auto", operation=operation).parse(str(path))
    def embed(batch):
        if change == "cancel":
            cancelled[0] = True
        else:
            path.write_bytes(b"modified source")
        return [[0.0] * VECTOR_DIM for _ in batch]
    indexer = Indexer.__new__(Indexer)
    indexer._embed = SimpleNamespace(embed=embed)
    indexer._crypto = FakeCryptoManager()
    indexer._chunk_size, indexer._overlap, indexer._batch_size = 400, 80, 32
    indexer._max_chunks_per_file, indexer._xlsx_max_rows_per_sheet = 500, 2000
    indexer._table = SimpleNamespace(add=Mock())
    indexer.set_commit_guard(operation.validate_commit)
    with pytest.raises(PptOperationError):
        indexer.index_file(str(path), text, path.stat().st_mtime, "scope")
    indexer._table.add.assert_not_called()


@pytest.mark.parametrize("updates", [dict(schema=True), dict(slide=True), dict(seconds=10**500),
    dict(read_errors=[dict(stage="text_read", kind="com", shape="1", row=0, column=0, hresult=0)]),
    dict(counts=dict(shapes=1, groups=1, tables=1, cells=0, errors=0)),
    dict(nodes=[dict(kind="table", cells=[["a"], ["b", "c"]])]),
    dict(counts=dict(shapes=1, groups=0, tables=0, cells=1, errors=0))])
def test_production_decoder_rejects_malformed_schema_and_counts(updates):
    with pytest.raises(PptPayloadError):
        decode_slide(reply(**updates), 1, strict=True)


@pytest.mark.parametrize("scenario", ["long_embed", "cancel", "source", "expired", "disabled", "pending"])
def test_collector_wires_frozen_guard_to_real_lance_commit(tmp_path, monkeypatch, scenario):
    """Exercise the actual worker, durable old generation and DB add boundary."""
    from knowmate.collector.scheduler import CollectorWorker
    from knowmate.collector.state import save_state, load_state
    from knowmate.collector.failure_state import load_failures
    from knowmate.rag.indexer import Indexer, DOC_INDEX_VERSION
    from knowmate.rag.embedding import EmbeddingClient
    from knowmate.secure import AutoReader
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    folder = tmp_path / "docs"
    folder.mkdir()
    ppt = folder / "first.ppt"
    ppt.write_bytes(b"source revision one")
    (folder / "second.txt").write_text("plain document")
    embed = EmbeddingClient(base_url="http://localhost", host_header="test", fake=True)
    indexer = Indexer(tmp_path / "db", embed, maintenance_config={"enabled": False})
    old_stat = ppt.stat()
    uid = indexer.document_uid(str(ppt))
    old_revision = indexer.document_revision(str(ppt), mtime=old_stat.st_mtime,
                                           mtime_ns=old_stat.st_mtime_ns, size=old_stat.st_size)
    old_ids = indexer.index_file(str(ppt), "old intact text", old_stat.st_mtime, "local",
                                 doc_uid=uid, revision=old_revision)
    old = indexer.recover_generation(uid, old_revision)
    indexer.table_was_recreated = False
    state_path = tmp_path / "state.json"
    save_state(state_path, {str(ppt): dict(mtime=old_stat.st_mtime, mtime_ns=old_stat.st_mtime_ns,
        size=old_stat.st_size, index_version=DOC_INDEX_VERSION, indexed_at=old["indexed_at"],
        chunk_ids=old_ids, doc_uid=uid, revision=old_revision, generation_id=old["generation_id"], method="com")})
    ppt.write_bytes(b"source revision two enlarged")
    now = [0.0]

    class FakeAuto(AutoReader):
        def __init__(self):
            super().__init__(ppt_reader_mode="com")
            self.records = []
        def set_ppt_operation(self, operation):
            super().set_ppt_operation(operation)
            if operation is not None:
                now[0] = operation.started
                operation._clock = lambda: now[0]
        def extract(self, path):
            self._last_com_used = path.endswith(".ppt")
            if path.endswith(".ppt"):
                operation = self._ppt_operation
                assert operation is not None
                self.records.append(operation)
                if scenario == "pending":
                    raise PptPreparationPending("installation_pending")
                operation.check()
                if scenario == "expired":
                    now[0] = operation.deadline + 1
                operation.finish(close_ok=True, successful=True)
                return "new complete PPT text"
            return "plain complete text"

    extractor = FakeAuto()
    config = dict(extractor="auto", collector=dict(watch_folders=[str(folder)], max_file_size_mb=30,
        com_timeout_base_sec=0 if scenario == "disabled" else 5, com_timeout_per_mb_sec=0,
        com_timeout_max_sec=5, drm_idle_threshold_sec=480, purge_enabled=False),
        mail={"enabled": False})
    worker = CollectorWorker(config, indexer, extractor, state_file=state_path,
        failure_file=tmp_path / "failures.json", purge_meta_file=tmp_path / "purge.json",
        get_idle_seconds=lambda: 0, com_cleanup_fn=lambda **kw: None, com_restart_fn=lambda: None)
    real_embed = embed.embed
    def during_embed(batch):
        if any("new complete PPT" in value for value in batch):
            if scenario == "long_embed":
                now[0] += 900
            elif scenario == "cancel":
                worker._cancelled = True
            elif scenario == "source":
                ppt.write_bytes(b"changed while embedding")
        return real_embed(batch)
    monkeypatch.setattr(embed, "embed", during_embed)
    messages = []
    worker.finished.connect(messages.append)
    worker._run_cycle()
    ppt_rows = indexer.table.search().where("file_type = 'ppt'").select(["chunk_id", "generation_id"]).limit(None).to_arrow().to_pylist()
    if scenario in {"long_embed", "disabled"}:
        assert ppt_rows and {row["generation_id"] for row in ppt_rows} != {old["generation_id"]}
        assert set(old_ids).isdisjoint(row["chunk_id"] for row in ppt_rows)
    else:
        assert {row["chunk_id"] for row in ppt_rows} == set(old_ids)
        assert load_state(state_path)[str(ppt)]["generation_id"] == old["generation_id"]
    if scenario == "pending":
        assert not load_failures(tmp_path / "failures.json")
        assert indexer.table.search().where("file_type = 'txt'").select(["chunk_id"]).to_arrow().num_rows > 0
        assert any("PPT 준비 대기 1건" in message for message in messages)
    if scenario == "expired":
        assert any("PPT 강제 종료 없는 시간초과 1건" in message for message in messages)
        assert not any("COM 시간초과 강제해제" in message for message in messages)
    if scenario == "disabled":
        assert extractor.records[0].deadline is None
    assert extractor._ppt_operation is None
    assert indexer._commit_guard is None


def test_strict_addin_error_is_content_free_to_collector(office):
    path, prs, app, service = office
    service.ExtractSlide.side_effect = RuntimeError("PRIVATE_TEXT_FROM_OFFICE")
    with pytest.raises(PptPayloadError) as error:
        com_reader.PowerPointComReader("addin").parse(str(path))
    assert str(error.value) == "ppt_read_failed"
    assert "PRIVATE" not in str(error.value)
    prs.Close.assert_called_once()


@pytest.mark.parametrize("error", ["office_busy", "cleanup_pending"])
@pytest.mark.parametrize("stage", ["dispatch", "service", "read"])
def test_office_control_errors_defer_without_fallback_or_sanitizing(office, monkeypatch, error, stage):
    from knowmate.secure.office_guard import OfficeBusyError, OfficeCleanupPendingError
    path, prs, app, service = office
    exc = OfficeBusyError("external_office") if error == "office_busy" else OfficeCleanupPendingError("POWERPNT.EXE", "cleanup_pending")
    if stage == "dispatch":
        monkeypatch.setattr(com_reader, "_get_ppt_app", Mock(side_effect=exc))
    elif stage == "service":
        app.COMAddIns.Item.side_effect = exc
    else:
        service.ExtractSlide.side_effect = exc
    with pytest.raises(type(exc)) as caught:
        com_reader.PowerPointComReader("auto").parse(str(path))
    assert caught.value is exc
    if stage != "read":
        app.Presentations.Open.assert_not_called()
    else:
        assert service.ExtractSlide.call_count == 1
        prs.Close.assert_called_once()


@pytest.mark.parametrize("changed", [False, True])
def test_candidate_setup_check_precedes_machine_lock_and_rechecks_installation(office, monkeypatch, changed):
    from contextlib import contextmanager
    from knowmate.secure import ppt_policy, ppt_installation, ppt_launch
    path, prs, app, service = office
    monkeypatch.setattr(com_reader._tls, "ppt", None)
    monkeypatch.setattr(ppt_policy, "auto_shutdown_enabled", lambda: True)
    inside = []
    @contextmanager
    def lock():
        inside.append(True)
        try:
            yield
        finally:
            inside.pop()
    def checker():
        assert not inside, "helper check would contend with the held machine mutex"
        return SimpleNamespace(status="ready", office_bitness=64)
    stamps = iter([("installed",), ("changed",) if changed else ("installed",)])
    monkeypatch.setattr(ppt_installation, "MachineReadGuard", lock)
    monkeypatch.setattr(ppt_installation, "check_installation", checker)
    monkeypatch.setattr(ppt_installation, "_validate_helper", lambda path: True)
    monkeypatch.setattr(ppt_launch, "installation_stamp", lambda: next(stamps))
    ready = []
    def app_getter():
        assert inside
        ready.append(com_reader._tls.ppt_launch_ready)
        return app
    monkeypatch.setattr(com_reader, "_get_ppt_app", app_getter)
    assert com_reader.PowerPointComReader("auto").parse(str(path)) == "candidate"
    assert (ready[0] is None) is changed

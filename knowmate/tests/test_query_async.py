"""Question worker admission, GUI responsiveness and safe shutdown boundaries."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QEventLoop, QObject, QTimer, pyqtSignal

from knowmate.app.bridge import Bridge
from knowmate.app.query_controller import QueryController
from knowmate.app.query_worker import QueryWorker


def request(number="one", **overrides):
    return {"query": "synthetic question", "mode": "knowledge", "scopes": ["local"],
            "request_id": number, "thread_id": "thread-one", "view_generation": 1, **overrides}


def until(predicate, timeout=3000):
    """Drive a real Qt loop, never GUI sleep or processEvents reentrancy."""
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(2)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    poll.start()
    deadline.start(timeout)
    if not predicate():
        loop.exec()
    poll.stop()
    deadline.stop()
    assert predicate(), "condition did not complete before deadline"


class Registry:
    def __init__(self, handle, events):
        self.handle = handle
        self.events = events

    def get(self, mode):
        self.events.append(("get", threading.get_ident()))
        return self

    def close(self):
        self.events.append(("close", threading.get_ident()))


def make_bridge(tmp_path, handle):
    events = []
    workers = []

    def registry(config, path):
        events.append(("build", threading.get_ident()))
        assert path == tmp_path
        return Registry(handle, events)

    def factory(*args, **kwargs):
        worker = QueryWorker(*args, registry_factory=registry, **kwargs)
        workers.append(worker)
        return worker

    bridge = Bridge(query_config={"ui": {"query_timeout_seconds": 3}},
                    query_data_dir=tmp_path, query_worker_factory=factory)
    responses, states = [], []
    bridge.responseReady.connect(lambda value: responses.append(json.loads(value)))
    bridge.queryStateChanged.connect(lambda value: states.append(json.loads(value)))
    return bridge, responses, states, events, workers


def test_real_qthread_keeps_gui_timer_and_bridge_slots_responsive(tmp_path):
    release = threading.Event()
    entered = threading.Event()
    ui_thread = threading.get_ident()
    handled = []

    def handle(query, context):
        handled.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return [{"type": "text", "content": "synthetic answer"}]

    bridge, responses, states, events, workers = make_bridge(tmp_path, handle)
    ticks = []
    heartbeat = QTimer()
    heartbeat.setInterval(5)
    heartbeat.timeout.connect(lambda: ticks.append(threading.get_ident()))
    try:
        bridge.sendQuery(json.dumps(request()))
        bridge.sendQuery(json.dumps(request("two")))
        heartbeat.start()
        until(lambda: entered.is_set() and len(ticks) >= 3)
        assert len(workers) == 1
        assert json.loads(bridge.getQueryState())["busy"]
        assert bridge.getVersion()
        assert responses == [{**{k: request("two")[k] for k in ("request_id", "thread_id", "mode", "view_generation")},
                              "status": "rejected", "blocks": [{"type": "text", "content": "이전 질문 처리가 끝난 뒤 다시 시도해 주세요."}]}]
        assert set(ticks) == {ui_thread}
        assert handled[0] != ui_thread
        release.set()
        until(lambda: not bridge._query.busy)
        assert responses[-1]["status"] == "success"
        assert states[-1]["state"] == "IDLE"
        assert states[-1]["request_id"] == "one"
        assert all(tid == handled[0] for _, tid in events)
    finally:
        heartbeat.stop()
        release.set()
        for worker in workers:
            if not sip.isdeleted(worker):
                worker.wait(3000)


@pytest.mark.parametrize("outcome", ["error", "serialization", "cancel"])
def test_worker_failure_is_safe_and_never_leaks_exception_content(tmp_path, caplog, outcome):
    secret = "SYNTHETIC_SECRET_QUERY_CHUNK_TOKEN"

    def handle(query, context):
        if outcome == "serialization":
            return [{"type": "text", "content": {object()}}]
        if outcome == "cancel":
            from knowmate.rag.embedding_cancel import EmbeddingCancelledError
            raise EmbeddingCancelledError(secret)
        raise RuntimeError(secret)

    bridge, responses, _, _, workers = make_bridge(tmp_path, handle)
    bridge.sendQuery(json.dumps(request()))
    until(lambda: not bridge._query.busy)
    assert len(responses) == 1
    assert responses[0]["status"] == ("cancelled" if outcome == "cancel" else "error")
    assert secret not in json.dumps(responses) + caplog.text
    assert "Traceback" not in json.dumps(responses) + caplog.text
    assert sip.isdeleted(workers[0]) or workers[0].wait(0)


@pytest.mark.parametrize("status", ["cancelled", "timeout"])
def test_retired_request_discards_late_success_and_preserves_lease(tmp_path, status):
    release = threading.Event()
    entered = threading.Event()

    def handle(query, context):
        entered.set()
        assert release.wait(3)
        return [{"type": "text", "content": "late forbidden result"}]

    bridge, responses, states, _, workers = make_bridge(tmp_path, handle)
    try:
        bridge.sendQuery(json.dumps(request()))
        until(entered.is_set)
        bridge._query.cancel("one", status)
        assert bridge._query.busy
        assert states[-1]["state"] == "RETIRED_PENDING_EXIT"
        bridge.sendQuery(json.dumps(request("two")))
        assert len(workers) == 1
        release.set()
        until(lambda: not bridge._query.busy)
        assert [r["status"] for r in responses] == [status, "rejected"]
        assert "late forbidden result" not in json.dumps(responses)
    finally:
        release.set()
        if not sip.isdeleted(workers[0]):
            workers[0].wait(3000)


class ControlledWorker(QObject):
    resultReady = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__(kwargs.get("parent"))
        self.joined = False
        self.deleted = 0
        self.cancels = 0
        self.waits = []

    def start(self):
        pass

    def wait(self, ms):
        self.waits.append(ms)
        return self.joined

    def cancel(self):
        self.cancels += 1

    def deleteLater(self):
        self.deleted += 1
        super().deleteLater()


def test_finished_signal_does_not_release_until_nonblocking_join(tmp_path):
    created = []

    def factory(*args, **kwargs):
        created.append(ControlledWorker(*args, **kwargs))
        return created[-1]

    controller = QueryController(worker_factory=factory)
    responses = []
    controller.responseReady.connect(lambda value: responses.append(json.loads(value)))
    controller.submit(request(), {}, tmp_path, 3000)
    worker = created[0]
    worker.finished.emit()
    until(lambda: len(responses) == 1)
    assert controller.busy and worker.deleted == 0
    assert responses[0]["status"] == "error"  # Result-less completion synthesizes one terminal.
    controller.submit(request("two"), {}, tmp_path, 3000)
    assert len(created) == 1
    assert set(worker.waits) == {0}
    worker.joined = True
    until(lambda: not controller.busy)
    assert worker.deleted == 1


@pytest.mark.parametrize("bad", [None, [], {}, {"query": []}, request(mode="bad"),
                                request(scopes=[]), request(scopes=[1]), request(scopes=None),
                                request(scopes=["unknown"]), request(scopes="local"),
                                request(request_id=3), request(view_generation=True)])
def test_invalid_request_never_constructs_worker(tmp_path, bad):
    bridge, responses, _, _, workers = make_bridge(tmp_path, lambda *_: [])
    bridge.sendQuery(json.dumps(bad))
    assert not workers
    assert responses[0]["status"] == "rejected"


@pytest.mark.parametrize("scopes", [["local"], ["shared"], ["mail"],
                                  ["local", "shared"], ["local", "mail"],
                                  ["shared", "mail"], ["local", "shared", "mail"]])
def test_bridge_and_worker_preserve_selected_search_scopes(tmp_path, scopes):
    """Bridge와 실제 질문 스레드는 세 체크 범위를 그대로 전달한다."""
    received = []
    bridge, responses, _, _, _ = make_bridge(
        tmp_path, lambda query, context: received.append((query, context["scopes"])) or [])
    bridge.sendQuery(json.dumps(request(scopes=scopes)))
    until(lambda: not bridge._query.busy)
    assert responses[0]["status"] == "success"
    assert received == [("synthetic question", scopes)]


def test_all_search_policy_writes_are_rejected_until_join(tmp_path, monkeypatch):
    created = []

    def factory(*args, **kwargs):
        created.append(ControlledWorker(*args, **kwargs))
        return created[-1]

    bridge = Bridge(query_config={"ui": {"query_timeout_seconds": 3}},
                    query_data_dir=tmp_path, query_worker_factory=factory)
    from knowmate import config
    cfg = {"collector": {"watch_folders": ["old"], "exclude_files": []}}
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    for name in ("update_settings", "update_exclude_files", "update_watch_folders"):
        monkeypatch.setattr(config, name, lambda *_: pytest.fail("policy write during query"))
    bridge.sendQuery(json.dumps(request()))
    for finished in (False, True):
        if finished:
            created[0].finished.emit()
            until(lambda: bridge._query._finished_seen)
        assert json.loads(bridge.saveSettings('{}'))["code"] == "query_busy"
        assert json.loads(bridge.excludeFile('one.pdf'))["error"] == "query_busy"
        assert json.loads(bridge.unexcludeFile('one.pdf'))["error"] == "query_busy"
        assert json.loads(bridge.excludeFiles('["one.pdf"]'))["error"] == "query_busy"
        assert json.loads(bridge.unexcludeFiles('["one.pdf"]'))["error"] == "query_busy"
        assert json.loads(bridge.addWatchFolder('new')) == ['old']
        assert json.loads(bridge.removeWatchFolder('old')) == ['old']
    created[0].joined = True
    until(lambda: not bridge._query.busy)
    saved = []
    monkeypatch.setattr(config, "update_settings", lambda patch: saved.append(patch))
    assert json.loads(bridge.saveSettings('{}'))["ok"]
    assert saved == [{}]


def test_conversation_ack_latest_read_delete_and_closing(tmp_path, monkeypatch):
    from knowmate.app import threads
    monkeypatch.setattr(threads, "THREADS_FILE", tmp_path / "threads.json")
    bridge = Bridge()
    conversation = {"id": "thread-one", "mode": "knowledge", "messages": [{"role": "user", "text": "one"}]}
    assert json.loads(bridge.saveThread("knowledge", json.dumps(conversation)))["ok"]
    conversation["messages"].append({"role": "assistant", "blocks": []})
    assert json.loads(bridge.saveThread("knowledge", json.dumps(conversation)))["ok"]
    assert json.loads(bridge.getThread("knowledge", "thread-one")) == conversation
    assert json.loads(bridge.deleteThread("knowledge", "thread-one"))["ok"]
    assert json.loads(bridge.getThread("knowledge", "thread-one")) is None
    monkeypatch.setattr(threads, "save_threads", lambda _: (_ for _ in ()).throw(OSError("SECRET")))
    assert not json.loads(bridge.saveThread("knowledge", json.dumps(conversation)))["ok"]
    assert not json.loads(bridge.deleteThread("knowledge", "thread-one"))["ok"]
    bridge.begin_query_shutdown()
    assert not json.loads(bridge.saveThread("knowledge", json.dumps(conversation)))["ok"]


def test_controller_shutdown_never_terminates_or_releases_unjoined_worker(tmp_path):
    controller = QueryController(worker_factory=ControlledWorker)
    controller.submit(request(), {}, tmp_path, 3000)
    worker = controller._worker
    controller.begin_shutdown()
    assert not controller.wait_for_shutdown(5)
    assert controller.closing and controller.busy and worker.deleted == 0
    assert worker.waits == [0, 5]
    worker.joined = True
    assert controller.wait_for_shutdown(5)
    assert controller.closing and not controller.busy


@pytest.mark.parametrize("collector_unsafe", [False, True])
@pytest.mark.parametrize("query_stage_error", ["none", "cancel", "wait"])
def test_shutdown_always_cleans_collector_and_skips_query_after_unsafe(monkeypatch, collector_unsafe, query_stage_error):
    from knowmate.app import lifecycle, main
    from knowmate.secure import office_guard
    calls = []

    def begin():
        calls.append("query_cancel")
        if query_stage_error == "cancel":
            raise RuntimeError("secret")

    def wait(ms):
        calls.append("query_wait")
        if query_stage_error == "wait":
            raise RuntimeError("secret")
        return True

    class HardExit(BaseException):
        pass

    def finalize(worker, quit_fn, force_hard_exit):
        calls.append(("finalize", force_hard_exit))
        if force_hard_exit:
            raise HardExit
        quit_fn()

    bridge = SimpleNamespace(_worker=None, begin_query_shutdown=begin, wait_query_shutdown=wait)
    window = SimpleNamespace(_shutdown_done=False, _bridge=bridge, _tray=None, _idle_scheduler=None)
    monkeypatch.setattr(lifecycle, "stop_worker", lambda *a, **kw: (calls.append("collector"), collector_unsafe)[1])
    monkeypatch.setattr(lifecycle, "finalize_shutdown", finalize)
    monkeypatch.setattr(main, "QApplication", SimpleNamespace(instance=lambda: SimpleNamespace(quit=lambda: calls.append("quit"))))
    try:
        if collector_unsafe or query_stage_error != "none":
            with pytest.raises(HardExit):
                main.MainWindow._shutdown(window)
        else:
            main.MainWindow._shutdown(window)
        assert calls[:2] == ["query_cancel", "collector"]
        assert ("query_wait" in calls) is (not collector_unsafe)
        assert calls[-1] == (("finalize", True) if collector_unsafe or query_stage_error != "none" else "quit")
        main.MainWindow._shutdown(window)
    finally:
        office_guard.clear_owned_pids()


@pytest.mark.parametrize("value", [True, False, 0, -1, float('inf'), float('nan'), '3', 10**1000])
def test_invalid_timeout_falls_back_to_bundled_single_default(value):
    from knowmate.config import query_timeout_ms
    assert query_timeout_ms({"ui": {"query_timeout_seconds": value}}) == query_timeout_ms({})


def test_non_object_ui_timeout_is_safe():
    from knowmate.config import query_timeout_ms
    assert query_timeout_ms({"ui": None}) == query_timeout_ms({})


@pytest.mark.parametrize("failed_stage", ["office_begin", "preparation", "scheduler", "tray"])
def test_shutdown_diagnostic_failure_cannot_skip_collector_or_finalization(monkeypatch, failed_stage):
    from knowmate.app import lifecycle, main
    from knowmate.secure import office_guard
    calls = []

    def fail():
        raise RuntimeError("secret")

    class Stage:
        shutdown = stop = hide = staticmethod(fail)

    bridge = SimpleNamespace(_worker=None, begin_query_shutdown=fail,
                             wait_query_shutdown=lambda ms: True)
    window = SimpleNamespace(_shutdown_done=False, _bridge=bridge,
                             _tray=Stage() if failed_stage == "tray" else None,
                             _ppt_preparation=Stage() if failed_stage == "preparation" else None,
                             _idle_scheduler=Stage() if failed_stage == "scheduler" else None)
    if failed_stage == "office_begin":
        monkeypatch.setattr(office_guard, "begin_application_shutdown", fail)
    monkeypatch.setattr(main.logger, "warning", lambda *a, **kw: fail())
    monkeypatch.setattr(lifecycle, "stop_worker", lambda *a, **kw: (calls.append("collector"), False)[1])
    monkeypatch.setattr(lifecycle, "finalize_shutdown", lambda worker, quit_fn, force_hard_exit: calls.append(("finalize", force_hard_exit)))
    monkeypatch.setattr(main, "QApplication", SimpleNamespace(instance=lambda: SimpleNamespace(quit=lambda: None)))
    try:
        main.MainWindow._shutdown(window)
        assert calls == ["collector", ("finalize", True)]
    finally:
        office_guard.clear_owned_pids()


def test_delayed_old_timeout_and_old_results_do_not_retire_new_request(tmp_path):
    controller = QueryController(worker_factory=ControlledWorker)
    responses = []
    controller.responseReady.connect(lambda s: responses.append(json.loads(s)))
    controller.submit(request(), {}, tmp_path, 3000)
    old_worker, old_timeout = controller._worker, controller._timeout
    old_worker.joined = True
    controller._finished_seen = True
    controller._poll_join()
    controller.submit(request("two"), {}, tmp_path, 3000)
    new_worker = controller._worker
    old_timeout.timeout.emit()
    old_worker.resultReady.emit(json.dumps({"status": "success", "blocks": []}))
    old_worker.finished.emit()
    controller._timeout.timeout.emit()
    assert controller.busy and controller._request["request_id"] == "two"
    assert responses[-1]["status"] == "timeout" and responses[-1]["request_id"] == "two"
    new_worker.joined = True
    controller._finished_seen = True
    controller._poll_join()


def test_start_failure_releases_only_proven_unstarted_worker(tmp_path):
    class FailedStart(ControlledWorker):
        def start(self):
            self.joined = True
            raise RuntimeError("secret")
    controller = QueryController(worker_factory=FailedStart)
    replies = []
    controller.responseReady.connect(lambda s: replies.append(json.loads(s)))
    controller.submit(request(), {}, tmp_path, 3000)
    assert not controller.busy
    assert len(replies) == 1 and replies[0]["status"] == "error"

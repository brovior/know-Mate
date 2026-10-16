"""Collect bounded Windows rename comparisons without changing the installer gate."""
from __future__ import annotations

import ntpath
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from knowmate.secure.ppt_installation import _DIAGNOSTIC_ERRORS, _DIAGNOSTIC_STAGES

_MODES = ("managed-create", "managed-replace", "native-create", "native-replace", "native-collision")
_CASES = tuple((mode, count) for mode in _MODES for count in range(3))
_STAGES = frozenset({
    "preflight", "fixture_create", "lease_acquire", "lease_protection", "managed_commit",
    "source_prepare", "native_rename_call", "verify", "lease_release", "cleanup", "complete",
    "source_open", "source_opened", "source_close", "source_closed", "native_return", "collision_verify",
    "lease_recheck", "directory_write_probe", "anchor_rename_probe", "parent_rename_probe", "child_rename_probe",
    "empty_child_delete_probe", "process_start",
    "atomic_path_check", "atomic_file_open", "atomic_writer_open", "atomic_serialize",
    "atomic_writer_flush", "atomic_disk_flush", "atomic_writer_close", "atomic_file_close",
    "atomic_acl_apply", "atomic_commit_probe", "atomic_replace_call", "atomic_move_call", "atomic_done",
})
_ERROR_CODES = frozenset({
    "exception", "source_not_single_regular_file", "directory_write_sharing_probe", "rename_basename_not_fixed",
    "rename_layout_invalid", "native_layout_invalid", "native_pending_unproven", "native_io_status_not_success",
    "atomic_source_not_unique", "namespace_mutation_allowed", "child_create", "source_identity_changed",
    "collision_status_not_name_collision", "collision_source_changed", "native_rename_failed", "source_name_remains",
    "target_content_changed", "target_identity_changed", "target_security_changed", "cwd_decoy_changed",
    "directory_security_changed", "argument_count", "mode_invalid", "lease_count_invalid", "fixture_path_invalid",
    "programdata_fixture_forbidden",
})


@dataclass(frozen=True)
class _Outcome:
    result: str
    stage: str = "unavailable"
    started_ms: str = "none"
    bits: str = "unavailable"
    hresult: str = "none"
    winerror: str = "none"
    ntstatus: str = "none"
    io_status: str = "none"
    io_information: str = "none"
    directory_winerror: str = "none"
    error_code: str = "none"
    errno: str = "none"
    setup_stage: str = "none"
    setup_error_type: str = "none"
    setup_winerror: str = "none"
    events: tuple[str, ...] = ()


def _outcome(output: str | bytes | None, code: int | None, *, timed_out: bool = False) -> _Outcome:
    """Only expose fixed markers, never arbitrary subprocess output or paths."""
    if isinstance(output, bytes):
        output = output.decode("ascii", errors="replace")
    stage, started, bits = "unavailable", "none", "unavailable"
    hresult, winerror, ntstatus, io_status = "none", "none", "none", "none"
    io_information, directory_winerror, error_code, error_stage = "none", "none", "none", None
    setup_stage, setup_error_type, setup_winerror = "none", "none", "none"
    starts = {}
    events = []
    result = "TIMEOUT" if timed_out else "FAIL"
    terminal = []
    lines = (output or "").splitlines()
    for line in lines:
        match = re.fullmatch(r"PPT_RENAME_STAGE ([a-z_]+) elapsed_ms=([0-9]{1,12})", line)
        if match and match[1] in _STAGES:
            stage, started = match[1], match[2]
            starts[stage] = started
            events.append(line)
        if re.fullmatch(r"PPT_RENAME_SECURITY owner_equal=[01] dacl_equal=[01] protected_before=[01] protected_after=[01] canonical_before=[01] canonical_after=[01] control_before=[0-9]{1,6} control_after=[0-9]{1,6}", line):
            events.append(line)
        match = re.fullmatch(r"PPT_RENAME_ARCH bits=(32|64)", line)
        if match:
            bits = match[1]
            events.append(line)
        match = re.fullmatch(r"PPT_RENAME_STATUS ntstatus=([0-9A-F]{8}) io_status=(none|[0-9A-F]{8}) io_information=([0-9]{1,20})", line)
        if match:
            ntstatus, io_status, io_information = match[1], match[2], match[3]
            events.append(line)
        if line == "PPT_RENAME_PENDING ntstatus=00000103 io_status=unavailable io_information=unavailable":
            ntstatus, io_status, io_information = "00000103", "unavailable", "unavailable"
            events.append(line)
        match = re.fullmatch(r"PPT_RENAME_SHARE result=(denied|opened) winerror=([0-9]{1,10}) leased=[01]", line)
        if match:
            directory_winerror = match[2]
            events.append(line)
        match = re.fullmatch(r"PPT_RENAME_SETUP stage=([a-z_]+) error_type=([A-Za-z0-9]+) winerror=(none|[0-9]{1,10})", line)
        if match and match[1] in _DIAGNOSTIC_STAGES and match[2] in _DIAGNOSTIC_ERRORS:
            setup_stage, setup_error_type, setup_winerror = match[1], match[2], match[3]
            events.append(line)
        match = re.fullmatch(r"PPT_RENAME_ERROR kind=(win32|io|access|contract) code=([a-z_]+) stage=([a-z_]+) hresult=([0-9A-F]{8}) winerror=(none|[0-9]{1,10})", line)
        if match:
            error_code = match[2] if match[2] in _ERROR_CODES else "unavailable"
            error_stage = match[3] if match[3] in _STAGES else "unavailable"
            hresult, winerror = match[4], match[5]
            if error_code != "unavailable" and error_stage != "unavailable":
                events.append(line)
        if line in ("PPT_RENAME_RESULT PASS", "PPT_RENAME_RESULT FAIL",
                    "PPT_RENAME_RESULT SKIP reason=elevated", "PPT_RENAME_RESULT SKIP reason=nonlocal_ntfs"):
            terminal.append(line)
            events.append(line)
    if not timed_out and len(terminal) == 1 and lines and lines[-1] == terminal[0] and bits != "unavailable":
        if terminal[0] == "PPT_RENAME_RESULT PASS" and code == 0 and hresult == "none" and stage == "complete":
            result = "PASS"
        elif terminal[0].startswith("PPT_RENAME_RESULT SKIP") and code == 77:
            result = "SKIP"
    if error_stage is not None and not timed_out:
        stage, started = error_stage, starts.get(error_stage, "none")
    return _Outcome(result=result, stage=stage, started_ms=started, bits=bits, hresult=hresult, winerror=winerror,
                    ntstatus=ntstatus, io_status=io_status, io_information=io_information,
                    directory_winerror=directory_winerror, error_code=error_code, setup_stage=setup_stage,
                    setup_error_type=setup_error_type, setup_winerror=setup_winerror, events=tuple(events[:256]))


def _os_code(error: OSError, name: str) -> str:
    code = getattr(error, name, None)
    return str(code) if isinstance(code, int) and 0 <= code <= 0xFFFFFFFF else "none"


def _run(command: list[str]) -> _Outcome:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True,
                                timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        return _outcome(error.stdout, None, timed_out=True)
    except OSError as error:
        return _Outcome("FAIL", "process_start", winerror=_os_code(error, "winerror"),
                        errno=_os_code(error, "errno"), error_code="process_start_failed")
    return _outcome(result.stdout, result.returncode)


def _collect(executable: Path, tmp_path: Path) -> list[_Outcome]:
    outcomes = []
    for mode, count in _CASES:
        anchor = tmp_path / ("ppt-rename-probe-" + uuid.uuid4().hex)
        print(f"PPT_RENAME_BEGIN mode={mode} leases={count}", flush=True)
        outcome = _run([str(executable), str(anchor), mode, str(count)])
        outcomes.append(outcome)
        for event in outcome.events:
            print(event, flush=True)
        if mode == "managed-replace" and outcome.error_code == "target_security_changed":
            for event in outcome.events:
                if event.startswith("PPT_RENAME_SECURITY "):
                    print(f"PPT_RENAME_SECURITY_SUMMARY mode={mode} leases={count} " + event[len("PPT_RENAME_SECURITY "):], flush=True)
        print(f"PPT_RENAME_CASE mode={mode} leases={count} result={outcome.result} bits={outcome.bits} "
              f"last_stage={outcome.stage} stage_started_ms={outcome.started_ms} "
              f"hresult={outcome.hresult} winerror={outcome.winerror} "
              f"ntstatus={outcome.ntstatus} io_status={outcome.io_status} io_information={outcome.io_information} "
              f"directory_winerror={outcome.directory_winerror} error_code={outcome.error_code} errno={outcome.errno} "
              f"setup_stage={outcome.setup_stage} setup_error_type={outcome.setup_error_type} "
              f"setup_winerror={outcome.setup_winerror}", flush=True)
    return outcomes


def _compiler_diagnostics(output: str | bytes | None, sources: list[Path]) -> tuple[str, ...]:
    if isinstance(output, bytes):
        output = output.decode("ascii", errors="replace")
    allowed = {path.name for path in sources}
    diagnostics = []
    for line in (output or "").splitlines():
        match = re.match(r"^(.*?)\(([0-9]{1,6}),([0-9]{1,6})\):\s*(error|warning)\s+(CS[0-9]{4})\b", line)
        if match and ntpath.basename(match[1]) in allowed:
            diagnostics.append(f"PPT_RENAME_COMPILER source={ntpath.basename(match[1])} line={match[2]} "
                               f"column={match[3]} level={match[4]} code={match[5]}")
        else:
            match = re.match(r"^(error|warning)\s+(CS[0-9]{4})\b", line)
            if match:
                diagnostics.append(f"PPT_RENAME_COMPILER source=compiler line=0 column=0 level={match[1]} code={match[2]}")
    return tuple(diagnostics[:64])


def _compile(command: list[str], sources: list[Path]) -> bool:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True,
                                timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        for event in _compiler_diagnostics(error.stdout, sources):
            print(event, flush=True)
        print("PPT_RENAME_COMPILE result=TIMEOUT limit_sec=30", flush=True)
        return False
    except OSError as error:
        print(f"PPT_RENAME_COMPILE result=FAIL stage=process_start winerror={_os_code(error, 'winerror')} "
              f"errno={_os_code(error, 'errno')}", flush=True)
        return False
    for event in _compiler_diagnostics(result.stdout + result.stderr, sources):
        print(event, flush=True)
    print(f"PPT_RENAME_COMPILE result={'PASS' if result.returncode == 0 else 'FAIL'} exit_code={result.returncode}", flush=True)
    return result.returncode == 0


def test_ppt_native_rename_comparison(tmp_path: Path) -> None:
    """Diagnostic matrix only; the original mandatory native lease test still applies."""
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Native rename comparison unexecuted: Windows .NET compiler required")
    root = Path(__file__).resolve().parents[2]
    policy = tmp_path / "PptBuildPolicy.cs"
    policy.write_text('namespace AegisDesk.PptSetup { internal static class BuildPolicy { '
                      'public const string AutoInstallMode = "candidate"; } }', encoding="utf-8")
    sources = [root / "scripts/ppt_reader/PptReaderSetup.cs",
               *sorted((root / "scripts/ppt_reader").glob("PptSetup*.cs")),
               root / "knowmate/tests/ppt_rename_probe_native.cs",
               root / "knowmate/tests/ppt_rename_probe_harness.cs", policy]
    executable = tmp_path / "RenameProbeHarness.exe"
    assert _compile(
        [str(compiler), "/nologo", "/main:RenameProbeHarness", f"/out:{executable}",
         "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll", "/reference:System.Security.dll",
         *(str(path) for path in sources)], sources,
    ), "PPT rename probe compile failed or unavailable: inspect PPT_RENAME_COMPILE/COMPILER lines"
    outcomes = _collect(executable, tmp_path)
    if all(outcome.result == "SKIP" for outcome in outcomes):
        pytest.skip("Native rename comparison unexecuted: ordinary user on local NTFS required")
    baseline = [(mode, outcome) for (mode, _), outcome in zip(_CASES, outcomes) if mode.startswith("managed-")]
    native = [outcome for (mode, _), outcome in zip(_CASES, outcomes) if mode.startswith("native-")]
    print(f"PPT_RENAME_SUMMARY category=baseline comparison_failures={sum(outcome.result != 'PASS' for _, outcome in baseline)} "
          f"native_failures={sum(outcome.result != 'PASS' for outcome in native)} required=false product_approval=false", flush=True)
    # Known managed failures remain visible baseline measurements; only native
    # comparison regressions fail this diagnostic test. Product gates are separate.
    assert all(outcome.result == "PASS" for outcome in native), (
        "Native comparison incomplete; inspect PPT_RENAME_CASE. This does not substitute for production writer gates.")


@pytest.mark.parametrize("output", [None, "PPT_RENAME_STAGE native_rename_call elapsed_ms=1900\n",
    b"PPT_RENAME_STAGE native_rename_call elapsed_ms=1900\n"])
def test_rename_probe_timeout_is_fixed_and_bounded(monkeypatch, output):
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(command, 30, output=output)
    monkeypatch.setattr(subprocess, "run", timeout)
    outcome = _run(["fixture"])
    assert outcome.result == "TIMEOUT"
    assert outcome.stage == ("unavailable" if output is None else "native_rename_call")
    assert outcome.started_ms == ("none" if output is None else "1900")


def test_rename_probe_output_is_fixed_and_requires_terminal_success():
    prefix = ("confidential-marker\nPPT_RENAME_ARCH bits=64\n"
              "PPT_RENAME_STAGE confidential_marker elapsed_ms=1\n"
              "PPT_RENAME_STAGE complete elapsed_ms=2\n")
    assert _outcome(prefix + "PPT_RENAME_RESULT PASS\n", 0).result == "PASS"
    assert "confidential" not in repr(_outcome(prefix, 1))
    for suffix, code in [("PPT_RENAME_RESULT PASS\nextra\n", 0), ("PPT_RENAME_RESULT PASS\n", 1),
                         ("PPT_RENAME_RESULT PASS\nPPT_RENAME_RESULT PASS\n", 0),
                         ("PPT_RENAME_RESULT SKIP reason=arbitrary\n", 77)]:
        assert _outcome(prefix + suffix, code).result == "FAIL"
    assert _outcome(prefix + "PPT_RENAME_RESULT PASS\n", None, timed_out=True).result == "TIMEOUT"


def test_rename_probe_preserves_hresult_and_ntstatus_without_guessing_winerror():
    output = ("PPT_RENAME_STAGE native_rename_call elapsed_ms=1900\n"
              "PPT_RENAME_STATUS ntstatus=C0000043 io_status=C0000043 io_information=0\n"
              "PPT_RENAME_ERROR kind=io code=exception stage=native_rename_call hresult=80070020 winerror=32\n"
              "PPT_RENAME_STAGE cleanup elapsed_ms=1901\nPPT_RENAME_RESULT FAIL\n")
    outcome = _outcome(output, 1)
    assert (outcome.hresult, outcome.winerror, outcome.ntstatus, outcome.io_status) == (
        "80070020", "32", "C0000043", "C0000043")
    assert (outcome.stage, outcome.started_ms, outcome.error_code, outcome.io_information) == (
        "native_rename_call", "1900", "exception", "0")


def test_rename_probe_collects_every_case_after_timeouts_and_failures(monkeypatch, tmp_path, capsys):
    calls = []
    def run(command):
        calls.append(command)
        return _Outcome("TIMEOUT" if len(calls) == 1 else "FAIL")
    monkeypatch.setattr(sys.modules[__name__], "_run", run)
    outcomes = _collect(tmp_path / "fixture.exe", tmp_path)
    assert len(outcomes) == len(calls) == len(_CASES) == 15
    assert len({command[1] for command in calls}) == 15
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 30
    assert sum(line.startswith("PPT_RENAME_CASE mode=") for line in lines) == 15
    assert sum(line.startswith("PPT_RENAME_BEGIN mode=") for line in lines) == 15
    assert [(command[2], int(command[3])) for command in calls] == list(_CASES)


def test_rename_probe_records_process_block_codes(monkeypatch):
    error = OSError(13, "confidential-marker")
    error.winerror = 1260
    def blocked(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, "run", blocked)
    outcome = _run(["fixture"])
    assert (outcome.result, outcome.stage, outcome.winerror, outcome.errno) == ("FAIL", "process_start", "1260", "13")
    assert "confidential" not in repr(outcome)


def test_rename_probe_retains_only_fixed_events_and_pending_failure():
    output = ("PPT_RENAME_ARCH bits=32\nPPT_RENAME_STAGE native_rename_call elapsed_ms=22\n"
              "PPT_RENAME_SHARE result=denied winerror=32 leased=1\n"
              "PPT_RENAME_PENDING ntstatus=00000103 io_status=unavailable io_information=unavailable\n"
              "PPT_RENAME_ERROR kind=contract code=native_pending_unproven stage=native_rename_call hresult=80004005 winerror=none\n"
              "confidential-marker\nPPT_RENAME_RESULT FAIL\n")
    outcome = _outcome(output, 1)
    assert (outcome.result, outcome.bits, outcome.ntstatus, outcome.io_information) == ("FAIL", "32", "00000103", "unavailable")
    assert outcome.error_code == "native_pending_unproven" and outcome.directory_winerror == "32"
    assert "confidential" not in "\n".join(outcome.events)
    assert len(outcome.events) == 6


def test_rename_probe_compiler_diagnostics_strip_paths_and_message():
    output = ("C:\\confidential\\PptSetupState.cs(12,34): warning CS0429: confidential-message\n"
              "C:\\confidential\\Unknown.cs(1,2): error CS0001: confidential-message\n"
              "error CS2001: confidential-message\n")
    events = _compiler_diagnostics(output, [Path("PptSetupState.cs")])
    assert events == (
        "PPT_RENAME_COMPILER source=PptSetupState.cs line=12 column=34 level=warning code=CS0429",
        "PPT_RENAME_COMPILER source=compiler line=0 column=0 level=error code=CS2001",
    )
    assert "confidential" not in repr(events)


def test_rename_probe_keeps_wrapped_setup_stage_and_actual_winerror():
    output = ("PPT_RENAME_SETUP stage=parent_open error_type=Win32Exception winerror=32\n"
              "PPT_RENAME_SETUP stage=confidential_marker error_type=IOException winerror=none\n")
    outcome = _outcome(output, 1)
    assert (outcome.setup_stage, outcome.setup_error_type, outcome.setup_winerror) == ("parent_open", "Win32Exception", "32")
    assert len(outcome.events) == 1 and "confidential" not in repr(outcome)


def test_rename_probe_timeout_keeps_cleanup_stage_after_operation_failure():
    output = ("PPT_RENAME_STAGE native_rename_call elapsed_ms=20\n"
              "PPT_RENAME_ERROR kind=io code=exception stage=native_rename_call hresult=80070020 winerror=32\n"
              "PPT_RENAME_STAGE cleanup elapsed_ms=21\n")
    outcome = _outcome(output, None, timed_out=True)
    assert (outcome.result, outcome.stage, outcome.started_ms, outcome.winerror) == ("TIMEOUT", "cleanup", "21", "32")


@pytest.mark.parametrize("failure", ["timeout", "blocked", "compile_error"])
def test_rename_probe_compile_failures_keep_fixed_diagnostics(monkeypatch, capsys, failure):
    def run(command, **kwargs):
        assert kwargs["timeout"] == 30
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30, output=b"error CS2001: confidential-marker\n")
        if failure == "blocked":
            error = OSError(13, "confidential-marker")
            error.winerror = 5
            raise error
        return subprocess.CompletedProcess(command, 1, "error CS2001: confidential-marker\n", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert not _compile(["fixture"], [])
    output = capsys.readouterr().out
    assert "confidential" not in output
    assert "PPT_RENAME_COMPILE result=" in output
    if failure == "blocked":
        assert "winerror=5 errno=13" in output
    else:
        assert "code=CS2001" in output

"""구버전 부모 권한 복구의 실제 NTFS 비전파 조건을 검사한다."""
from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("architecture", ["x86", "x64"])
def test_native_parent_only_security_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, architecture: str) -> None:
    """네이티브 두 단계 저장이 기존 하위 항목을 보존하는지 확인한다."""
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Windows NTFS 및 .NET Framework compiler 필요")
    root = Path(__file__).resolve().parents[2]
    policy = tmp_path / "Policy.cs"
    policy.write_text('namespace AegisDesk.PptSetup { internal static class BuildPolicy { '
                      'public const string AutoInstallMode = "candidate"; } }', encoding="utf-8")
    source = root / "scripts/ppt_reader"
    executable = tmp_path / "ParentNativeHarness.exe"
    command = [str(compiler), "/nologo", f"/platform:{architecture}", "/main:ParentNativeHarness", f"/out:{executable}",
               "/reference:System.Core.dll", "/reference:System.Security.dll", "/reference:System.Web.Extensions.dll",
               str(source / "PptReaderSetup.cs"), *(str(path) for path in sorted(source.glob("PptSetup*.cs"))),
               str(root / "knowmate/tests/ppt_parent_native_harness.cs"), str(policy)]
    compiled = subprocess.run(command, capture_output=True, timeout=45, check=False)
    assert compiled.returncode == 0, (compiled.stdout + compiled.stderr).decode("ascii", errors="replace")
    result = subprocess.run([str(executable), str(tmp_path / "native")], capture_output=True, timeout=45, check=False)
    output = (result.stdout + result.stderr).decode("ascii", errors="replace")
    assert result.returncode == 0, output
    assert "PASS native parent-only gate" in output
    assert "PASS native special lease reservation" in output
    assert "PASS native exact policy and rejection" in output
    from knowmate.secure import ppt_installation

    reservation_anchor = tmp_path / "native/reservation"
    monkeypatch.setattr(ppt_installation, "_known_folder", lambda _folder: reservation_anchor)
    assert not ppt_installation._machine_records_safe()


@pytest.mark.parametrize("state", ["reserved", "elevated"])
@pytest.mark.parametrize("revision", ["70fe63f", "c5c638a"])
def test_historical_checker_blocks_existing_schema_reservation(tmp_path: Path, state: str, revision: str) -> None:
    """실제 1.0.0.0 checker를 컴파일해 새 요구버전 예약도 보류하는지 확인한다."""
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Windows .NET Framework compiler 필요")
    root = Path(__file__).resolve().parents[2]
    sources: list[str] = []
    for name in ("PptReaderSetup.cs", "PptSetupMachine.cs", "PptSetupMutation.cs", "PptSetupReservation.cs", "PptSetupState.cs"):
        historical = subprocess.run(["git", "show", f"{revision}:scripts/ppt_reader/{name}"], cwd=root,
                                    check=False, capture_output=True, timeout=15)
        if historical.returncode != 0:
            pytest.skip(f"역사적 {revision} 소스가 필요한 호환 gate 미실행")
        text = historical.stdout.decode("utf-8")
        if name == "PptSetupMachine.cs":
            original = 'Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "AegisDesk", "PptReaderRequests")'
            assert text.count(original) == 1
            text = text.replace(original, 'System.Environment.GetCommandLineArgs()[1]')
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        sources.append(str(path))
    harness = tmp_path / "HistoricalHarness.cs"
    harness.write_text('internal static class HistoricalHarness { public static int Main(string[] args) { '
                       'return AegisDesk.PptSetup.Machine.ReservationsSafe() ? 1 : 0; } }', encoding="utf-8")
    executable = tmp_path / "Historical.exe"
    compiled = subprocess.run([str(compiler), "/nologo", "/main:HistoricalHarness", f"/out:{executable}",
                               "/reference:System.Core.dll", "/reference:System.Security.dll", "/reference:System.Web.Extensions.dll",
                               *sources, str(harness)], check=False, capture_output=True, timeout=45)
    assert compiled.returncode == 0, (compiled.stdout + compiled.stderr).decode("ascii", errors="replace")
    requests = tmp_path / "requests"
    requests.mkdir()
    request_id = "33e979c6-c5c4-40af-845a-64c80517d1ae"
    value = dict(schema=1, request_id=request_id, required_version="1.0.5.13", helper_sid="S-1-5-21-1-2-3-1001",
                 helper_pid=123, helper_creation_ticks="638901000000000000", child_pid=456 if state == "elevated" else None,
                 child_creation_ticks="638901000000000001" if state == "elevated" else None, state=state)
    (requests / f"{request_id}.json").write_text(json.dumps(value), encoding="utf-8")
    observed = subprocess.run([str(executable), str(requests)], check=False, capture_output=True, timeout=20)
    assert observed.returncode == 0, (observed.stdout + observed.stderr).decode("ascii", errors="replace")


def test_lifecycle_fixture_binding_compiles_without_changing_repair_core(tmp_path: Path) -> None:
    """OS 경계만 치환한 fixture에서 실제 복구 핵심 소스를 컴파일한다."""
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Windows .NET Framework compiler 필요")
    from knowmate.tests.ppt_parent_lifecycle_build import build_fixture

    executable = build_fixture(tmp_path / "fixture")
    assert executable.is_file()
    provenance = json.loads((executable.parent / "provenance.json").read_text(encoding="utf-8"))
    core = provenance["sources"]["PptSetupParentRepair.cs"]
    assert core["original_sha256"] == core["bound_sha256"]


def test_actual_main_nonadmin_error_json_is_accepted_by_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """실제 Main의 초기 예외가 null 결과로 크래시하지 않고 제한 JSON을 반환한다."""
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Windows .NET Framework compiler 필요")
    from knowmate.tests.ppt_parent_lifecycle_build import build_fixture
    from knowmate.secure import ppt_installation

    executable = build_fixture(tmp_path / "endpoint", nonadmin_endpoint=True)
    result = subprocess.run([str(executable), "/recover-parent", "invalid"], capture_output=True, timeout=20, check=False)
    monkeypatch.setattr(ppt_installation, "auto_install_mode", lambda: "candidate")
    parsed = ppt_installation.parse_setup_result(result.stdout.decode("utf-8"), result.returncode, "recover-parent")
    assert result.returncode == 12
    assert parsed.status == "failed" and parsed.reason == "acl_unavailable"
    assert parsed.stage == "parent_repair_recover" and parsed.error_type == "policy"
    assert not parsed.machine_safe
    assert not (executable.parent / "native").exists()

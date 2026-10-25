"""Exercise the real local elevation-diagnostic transport without Office or installation."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from knowmate.tests.ppt_parent_lifecycle_build import _body, _once


@pytest.fixture(scope="module", params=["x86", "x64"])
def diagnostic_harness(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> Path:
    """Compile unchanged control flow with fixed private OS folder/admin/bitness boundaries."""
    if os.name != "nt":
        pytest.skip("Windows native pipe/.NET Framework required")
    output = tmp_path_factory.mktemp("child-diagnostic-" + request.param)
    output.rmdir()
    return build_diagnostic_harness(output, request.param)


def build_diagnostic_harness(output: Path, architecture: str = "x64") -> Path:
    """Build a fixed-root component fixture; never perform UAC or installation here."""
    root = Path(__file__).resolve().parents[2]
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if not compiler.is_file():
        pytest.skip(".NET Framework compiler required")
    output.mkdir(parents=True)
    sources = []
    provenance = {}
    for original in [root / "scripts/ppt_reader/PptReaderSetup.cs", *sorted((root / "scripts/ppt_reader").glob("PptSetup*.cs"))]:
        source = original.read_text(encoding="utf-8")
        if original.name == "PptSetupMachine.cs":
            source = _once(source, 'Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles)', 'ChildFixture.Install')
            source = _once(source, 'Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData)', 'ChildFixture.Data')
            source = _body(source, 'public static int OfficeBitness()', 'return ChildFixture.Bitness();')
            source = _once(source, 'public static bool Admin { get { using (var identity = WindowsIdentity.GetCurrent()) return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator); } }', 'public static bool Admin { get { return ChildFixture.Mode != "main_admin"; } }')
        target = output / original.name
        target.write_text(source, encoding="utf-8")
        sources.append(str(target))
        provenance[original.name] = {"source_sha256": hashlib.sha256(original.read_bytes()).hexdigest(), "bound_sha256": hashlib.sha256(source.encode()).hexdigest()}
    escaped = str(output).replace('"', '""')
    policy = output / "FixturePaths.cs"
    policy.write_text(f'internal static class FixturePaths {{ internal const string Root = @"{escaped}"; }} namespace AegisDesk.PptSetup {{ internal static class BuildPolicy {{ public const string AutoInstallMode = "candidate"; }} }}', encoding="utf-8")
    executable = output / "PptReaderSetup.exe"
    result = subprocess.run([str(compiler), "/nologo", "/utf8output", "/target:exe", "/main:ChildFixture", f"/platform:{architecture}", f"/out:{executable}", "/reference:System.Core.dll", "/reference:System.Security.dll", "/reference:System.Web.Extensions.dll", *sources, str(policy), str(root / "knowmate/tests/ppt_child_diagnostics_harness.cs")], capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return executable


@pytest.mark.parametrize("mode", ["success", "impersonation", "first_instance", "peer_mismatch", "stall", "cancel_accept", "cancel_read", "handled", "hresult_one", "hresult_two", "root_owner", "native_denied", "native_wrapped", "native_direct", "native_repeated", "native_refined", "machine_check", "rollback", "main_payload", "main_admin", "never_connect", "server_mismatch", "wrong_code", "wrong_nonce", "trailing", "partial", "oversize"])
def test_real_child_diagnostic_transport(diagnostic_harness: Path, mode: str) -> None:
    """Preserve actual child/error identity and reject malformed frames without installation."""
    result = subprocess.run([str(diagnostic_harness), mode], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    line = result.stdout.strip().splitlines()[-1]
    value = json.loads(line)
    assert value["phase"] == "install"
    assert all(marker not in line for marker in ("PRIVATE", "secret body", "S-1-5-", str(diagnostic_harness.parent)))

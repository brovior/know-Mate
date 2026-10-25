"""Build an admin-only, private-root production Main/elevation/mutation flow fixture."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from knowmate.tests.ppt_parent_lifecycle_build import _body, _once, build_fixture


def build_flow_fixture(output: Path) -> Path:
    """Bind only private OS/test fault boundaries and preserve production control flow."""
    executable = build_fixture(output)
    root = Path(__file__).resolve().parents[2]
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    modifications = {}
    for name in ("PptSetupMachine.cs", "PptSetupReservation.cs", "PptReaderSetup.cs", "PptSetupMutation.cs"):
        path = output / name
        source = path.read_text(encoding="utf-8")
        if name == "PptSetupMachine.cs":
            source = _body(source, "public static int OfficeBitness()", "return DiagnosticFlow.OfficeBitness();")
            source = _body(source, "public static bool OfficeClosed()", "return !DiagnosticFlow.Worker || DiagnosticFlow.Mode != \"office_busy\";")
        elif name == "PptSetupReservation.cs":
            source = _once(source, "private struct ExecuteInfo", "internal struct ExecuteInfo")
            source = _once(source, 'return RepairLifecycleFixture.Elevate(request);', 'return RunOperation("/elevated-repair-parent ", request);')
            source = _body(source, "public static bool ProcessGone(int pid, string ticks)", "return DiagnosticFlow.ProcessGone(pid,ticks);")
            source = _once(source, "if (!ShellExecuteEx(ref info))", "if (!DiagnosticFlow.ShellExecute(ref info))")
            source = _once(source, '(diagnostics==null?"":diagnostics.Arguments)', '(DiagnosticFlow.SuppressDiagnostics||diagnostics==null?"":diagnostics.Arguments)')
        elif name == "PptReaderSetup.cs":
            source = _once(source, "using (var lease = RequestDirectoryLease.AcquireExisting())", "using (var lease = DiagnosticFlow.AcquireExisting())")
            source = _once(source, "Reservation request = Reservation.Load(args[1], lease);", "Reservation request = DiagnosticFlow.Load(args[1], lease);")
            source = _once(source, "request.EnterChild();", "DiagnosticFlow.Enter(request);")
            source = _once(source, 'Mutation.Apply(operation == "elevated-remove", request.request_id, lease);', 'DiagnosticFlow.BeforeMutation(); Mutation.Apply(operation == "elevated-remove", request.request_id, lease);')
        else:
            source = _once(source, 'RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, Machine.View(state.office_bitness.Value))', 'RepairLifecycleFixture.Registry(false)')
            source = _once(source, 'RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, Machine.View(bitness))', 'RepairLifecycleFixture.Registry(false)')
        path.write_text(source, encoding="utf-8")
        modifications[name] = hashlib.sha256(source.encode("utf-8")).hexdigest()
    harness = (root / "knowmate/tests/ppt_parent_lifecycle_harness.cs").read_text(encoding="utf-8")
    for name in ("Reset", "HistoricalProtect"):
        harness = _once(harness, f"private static void {name}(", f"internal static void {name}(")
    harness = _once(harness, "private static FileSecurity ProtectedFile()", "internal static FileSecurity ProtectedFile()")
    harness = _body(harness, "public static int Main(string[] args)", "return DiagnosticFlow.Run(args);")
    copied_harness = output / "LifecycleHarness.cs"
    copied_harness.write_text(harness, encoding="utf-8")
    sources = [output / "PptReaderSetup.cs", *sorted(output.glob("PptSetup*.cs"))]
    result = subprocess.run([str(compiler), "/nologo", "/utf8output", "/main:RepairLifecycleFixture", f"/out:{executable}", "/reference:System.Core.dll", "/reference:System.Security.dll", "/reference:System.Web.Extensions.dll", *(str(path) for path in sources), str(output / "FixturePaths.cs"), str(copied_harness), str(root / "knowmate/tests/ppt_parent_native_harness.cs"), str(root / "knowmate/tests/ppt_child_flow_harness.cs")], capture_output=True, text=True, encoding="utf-8", timeout=45)
    (output / "flow-compile.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise ValueError("flow_fixture_compile_failed")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["PptReaderSetup.exe"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    provenance_path = output / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    for name, digest in modifications.items():
        provenance["sources"][name]["bound_sha256"] = digest
    provenance["flow_boundaries"] = ["fixed private folders and GUID-scoped HKCU only", "actual admin token required; Machine.Admin is logical per original fixture", "ShellExecuteEx replaced by real inherited-token child process plus duplicated native handle; Wait/ExitCode/pipe unchanged", "ProcessGone restored to actual process identity", "Office presence/bitness and API faults controlled; actual Main and Mutation decision flow retained", "rollback_1175 injects equal File.Replace failure at atomic_replace_call for both diagnostic variants; un-injected OS failures still fail strict outcome comparison", "diagnostic on/off legacy CLI pairs; no direct Capture/Gate calls", "repair_install_error injects current-user ownership into private outer only after first repair succeeds, at install BeforeMutation"]
    provenance["flow_harness_sha256"] = hashlib.sha256((root / "knowmate/tests/ppt_child_flow_harness.cs").read_bytes()).hexdigest()
    provenance_path.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return executable

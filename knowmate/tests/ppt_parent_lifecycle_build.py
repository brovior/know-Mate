"""제품 코드를 복사해 OS 경계만 격리한 수동 승격 시험 EXE를 만든다."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path


def _once(source: str, old: str, new: str) -> str:
    """예상한 경계 한 곳만 치환하며 소스 변화 때 조용히 계속하지 않는다."""
    if source.count(old) != 1:
        raise ValueError(f"fixture_boundary_count:{old[:80]}")
    return source.replace(old, new)


def _body(source: str, signature: str, replacement: str) -> str:
    """중괄호로 구분한 단일 OS 함수 본문만 시험 binding으로 바꾼다."""
    if source.count(signature) != 1:
        raise ValueError("fixture_signature_count")
    start = source.index("{", source.index(signature))
    depth = 1
    end = start + 1
    while depth:
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
        end += 1
    return source[:start] + "{ " + replacement + " }" + source[end:]


def build_fixture(output: Path, *, nonadmin_endpoint: bool = False) -> Path:
    """실제 시스템을 대상으로 실행할 수 없는 고정 경로 시험 산출물을 만든다."""
    if output.exists():
        raise ValueError("fixture_output_must_be_new")
    output.mkdir(parents=True)
    root = Path(__file__).resolve().parents[2]
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    fixture_id = uuid.uuid4().hex
    source_paths: list[Path] = []
    provenance: dict[str, object] = {"schema": 1, "fixture_id": fixture_id, "sources": {}, "boundaries": [
        "known folder paths -> compile-time fixture root", "HKLM/HKCU -> HKCU isolated fixture subkeys",
        "Machine.Admin logical transition on actual elevated fixture token", "Office presence/bitness -> closed/64",
        "RunRepair -> same-process logical elevated child", "ProcessGone -> controlled logical completion",
        "backup/write fault hooks -> deterministic interruption", "managed metadata commit failure -> fixture-only handle/ACL diagnostics",
    ]}
    for path in [root / "scripts/ppt_reader/PptReaderSetup.cs", *sorted((root / "scripts/ppt_reader").glob("PptSetup*.cs"))]:
        source = path.read_bytes().decode("utf-8")
        original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.name == "PptSetupMachine.cs":
            source = _once(source, 'Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "AegisDesk", "PptReaderRequests")', 'RepairLifecycleFixture.Requests')
            source = _once(source, 'Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "AegisDesk", "PptReader")', 'RepairLifecycleFixture.Root')
            source = _once(source, 'using (var identity = WindowsIdentity.GetCurrent()) return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);', 'return RepairLifecycleFixture.Admin;')
            source = _body(source, "public static bool OfficeClosed()", "return true;")
            source = _body(source, "public static int OfficeBitness()", "return 64;")
            source = _once(source, 'RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, View(result.office_bitness.Value))', 'RepairLifecycleFixture.Registry(false)')
            source = _once(source, 'RegistryKey.OpenBaseKey(RegistryHive.CurrentUser, View(result.office_bitness.Value))', 'RepairLifecycleFixture.Registry(true)')
        if path.name == "PptSetupLegacyInstallation.cs":
            source = _once(source, 'Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles)', 'RepairLifecycleFixture.Install')
            source = _once(source, 'RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, Machine.View(lease.Bitness))', 'RepairLifecycleFixture.Registry(false)')
        if path.name in {"PptSetupDirectoryLease.cs", "PptSetupRepairLease.cs"}:
            expected = 2 if path.name == "PptSetupDirectoryLease.cs" else 1
            boundary = 'Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData)'
            if source.count(boundary) != expected:
                raise ValueError("fixture_lease_path_count")
            source = source.replace(boundary, 'RepairLifecycleFixture.Data')
        if path.name == "PptSetupState.cs":
            source = _once(source, '@"Global\\AegisDesk.PptReader.Machine.v1"', f'@"Local\\AegisDesk.PptReader.RepairFixture.{fixture_id}"')
            source = _once(source, 'catch (Exception error) { if (request) throw SetupFailure.From("request_commit", error); throw; }',
                           'catch (Exception error) { if (!request) RepairLifecycleFixture.AtomicFailure(path, temp, error); if (request) throw SetupFailure.From("request_commit", error); throw; }')
        if path.name == "PptSetupReservation.cs":
            source = _body(source, 'public static bool ProcessGone(int pid, string ticks)', 'return RepairLifecycleFixture.ProcessGone(pid, ticks);')
            source = _once(source, 'return RunOperation("/elevated-repair-parent ", request);', 'return RepairLifecycleFixture.Elevate(request);')
        if path.name == "PptSetupRepairBackup.cs":
            source = _once(source, 'Save(data, true); return data;', 'RepairLifecycleFixture.BeforeBackup(); Save(data, true); RepairLifecycleFixture.AfterBackup(); return data;')
        if path.name == "PptSetupParentSecurity.cs":
            source = _once(source, 'int status = NtSetSecurityObject(handle, 4, expected.GetSecurityDescriptorBinaryForm());',
                           'int status = NtSetSecurityObject(handle, 4, expected.GetSecurityDescriptorBinaryForm()); RepairLifecycleFixture.AfterWrite();')
        if path.name == "PptSetupDiagnostics.cs":
            source = _once(source, 'Stage = stage; ErrorType = type; WinError = winerror;',
                           'Stage = stage; ErrorType = type; WinError = winerror; System.IO.File.AppendAllText(FixturePaths.Result + ".policytrace", stage + "\\n" + System.Environment.StackTrace + "\\n");')
        target = output / path.name
        target.write_bytes(source.encode("utf-8"))
        source_paths.append(target)
        provenance["sources"][path.name] = {"original_sha256": original_hash, "bound_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    paths = output / "FixturePaths.cs"
    def escaped(path: Path) -> str:
        """컴파일 시 고정할 Windows 경로의 문자열 따옴표만 이스케이프한다."""
        return str(path).replace('"', '""')
    paths.write_text('internal static class FixturePaths { '
                     f'internal const string Root = @"{escaped(output / "native")}"; '
                     f'internal const string Result = @"{escaped(output / "result.txt")}"; '
                     f'internal const string LegacyDll = @"{escaped(output / "legacy/AegisDesk.PptReader.dll")}"; '
                     f'internal const string Id = "{fixture_id}"; '
                     '} namespace AegisDesk.PptSetup { internal static class BuildPolicy { '
                     'public const string AutoInstallMode = "candidate"; } }', encoding="utf-8")
    def compile_sources(arguments: list[str]) -> None:
        """로컬 .NET compiler 오류를 고정 시험 디렉터리에 보존한다."""
        compiled = subprocess.run([str(compiler), "/nologo", *arguments], capture_output=True, check=False, timeout=45)
        if compiled.returncode:
            (output / "compile-error.txt").write_bytes(compiled.stdout + compiled.stderr)
            raise ValueError("fixture_compile_failed")
    for version, directory in [("1.0.5.14", output), ("1.0.0.0", output / "legacy")]:
        directory.mkdir(exist_ok=True)
        payload_source = directory / "Payload.cs"
        payload_source.write_text(f'using System.Reflection; [assembly: AssemblyVersion("{version}")] internal class FixturePayload {{ }}', encoding="utf-8")
        names = ["AegisDesk.PptReader.dll", "AegisDesk.PptReader.Events.dll"] if version == "1.0.5.14" else ["AegisDesk.PptReader.dll"]
        for name in names:
            compile_sources(["/target:library", f"/out:{directory / name}", str(payload_source)])
    executable = output / "PptReaderSetup.exe"
    if nonadmin_endpoint:
        endpoint = output / "MainEndpoint.cs"
        endpoint.write_text('internal static class MainErrorFixture { public static int Main(string[] args) { '
                            'return AegisDesk.PptSetup.Program.Main(args); } }', encoding="utf-8")
        source_paths.append(endpoint)
    entrypoint = "MainErrorFixture" if nonadmin_endpoint else "RepairLifecycleFixture"
    compile_sources([f"/main:{entrypoint}", f"/out:{executable}", "/reference:System.Core.dll",
                     "/reference:System.Security.dll", "/reference:System.Web.Extensions.dll", *(str(path) for path in source_paths),
                     str(paths), str(root / "knowmate/tests/ppt_parent_native_harness.cs"),
                     str(root / "knowmate/tests/ppt_parent_lifecycle_harness.cs")])
    names = ("AegisDesk.PptReader.dll", "AegisDesk.PptReader.Events.dll", "PptReaderSetup.exe")
    manifest = dict(schema=1, reader_version="1.0.5.14", protocol=3, payload_schema=2,
                    prog_id="AegisDesk.PptReader", reader_class="AegisDesk.PptReader.Connect",
                    reader_clsid="287A80D2-804A-46D3-A0E9-CA31C6FC3D09", reader_iid="9A20F4A6-6364-4BA7-930B-3E8857BEAF60",
                    auto_install_mode="candidate", auto_install_enabled=True, auto_install_validated=False, auto_shutdown_enabled=True,
                    assembly_versions={name: "1.0.5.14" for name in names},
                    files={name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in names})
    (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return executable

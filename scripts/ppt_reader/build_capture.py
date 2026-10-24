"""Build a separately versioned, default-locked diagnostic package without Office."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VERSION = "1.0.6.0"
PREVIOUS_VERSION = "1.0.5.10"
PAYLOADS = ("AegisDesk.PptReader.dll", "AegisDesk.PptReader.Events.dll", "PptReaderSetup.exe")
COLLECTOR = "PptCaptureCollector.exe"
DEFINES = "/define:AEGIS_PPT_DEFERRED_DIAGNOSTIC;AEGIS_PPT_EXTERNAL_DIAGNOSTIC;AEGIS_PPT_CAPTURE_DIAGNOSTIC"
CORE = ("PptReader.cs", "PptCallerQuit.cs", "PptShutdownEvents.cs", "PptOwnedMetadata.cs", "PptShutdownGuard.cs",
        "PptRawCount.cs", "PptShutdownState.cs", "PptSinkLoader.cs", "PptPackageIdentity.cs",
        "PptDeferredGate.cs", "PptDeferredDiagnostic.cs", "PptEventSignatures.cs",
        "PptEventValidator.cs", "PptCaptureBuffer.cs")


def digest(path: Path) -> str:
    """Hash a bounded build/source file without interpreting its contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_paths(root: Path = ROOT) -> list[Path]:
    """Enumerate the implementation inputs pinned by this diagnostic package."""
    paths = set((root / "scripts/ppt_reader").glob("*.cs"))
    for pattern in ("scripts/*.py", "scripts/diagnostics/*.py", "knowmate/*.py",
                    "scripts/ppt_reader/*.py", "scripts/ppt_events/*.cs", "scripts/ppt_shared/*.cs",
                    "scripts/diagnostics/ppt_capture/*.py", "knowmate/secure/*.py",
                    "knowmate/tests/ppt_capture_*harness.cs"):
        paths.update(root.glob(pattern))
    paths.update(root / name for name in ("scripts/ppt_reader/build_capture.py",
        "scripts/ppt_reader/build.ps1", "scripts/build_guard.py", "scripts/ppt_reader/manifest.py",
        "knowmate/build_info.py", "knowmate/version.py",
        "knowmate/tests/ppt_capture_buffer_harness.cs", "knowmate/tests/test_ppt_capture_buffer_native.py"))
    return sorted(paths)


def verify_metadata(info_dir: Path, root: Path = ROOT) -> dict[str, Any]:
    """Reject stale or released build metadata and an altered generated policy."""
    info = runpy.run_path(str(info_dir / "aegisdesk_build_info.py"))["BUILD_INFO"]
    current = runpy.run_path(str(root / "scripts/build_guard.py"))["_build_info"]()
    if any(info.get(key) != current[key] for key in ("commit", "source_fingerprint", "dirty")):
        raise ValueError("ppt_build_source_changed")
    if info.get("ppt_auto_install_mode") != "candidate":
        raise ValueError("ppt_capture_candidate_required")
    expected = ('namespace AegisDesk.PptSetup { internal static class BuildPolicy { '
                'public const string AutoInstallMode = "candidate"; } }\n'
                'namespace AegisDesk.PptReader { internal static class ShutdownBuildPolicy { '
                'public const bool Enabled = true; } }\n')
    if (info_dir / "PptBuildPolicy.cs").read_text(encoding="utf-8") != expected:
        raise ValueError("ppt_build_policy_changed")
    return info


def copy_sources(directory: Path, info_dir: Path, root: Path = ROOT) -> dict[str, str]:
    """Transform versions in generated copies while preserving normal product sources."""
    hashes: dict[str, str] = {}
    for source in source_paths(root):
        if source.suffix != ".cs":
            continue
        relative = source.relative_to(root)
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8-sig").replace(PREVIOUS_VERSION, VERSION), encoding="utf-8")
        hashes[relative.as_posix()] = digest(target)
    policy = directory / "PptBuildPolicy.cs"
    policy.write_bytes((info_dir / "PptBuildPolicy.cs").read_bytes())
    hashes[policy.name] = digest(policy)
    version = directory / "CaptureAssemblyVersion.cs"
    version.write_text('using System.Reflection; [assembly: AssemblyVersion("' + VERSION + '")]\n', encoding="utf-8")
    hashes[version.name] = digest(version)
    return hashes


def create_manifests(output: Path, info: dict[str, Any], generated: dict[str, str], root: Path = ROOT) -> dict[str, Any]:
    """Write helper-compatible metadata and separately locked capture metadata."""
    build = {key: info[key] for key in ("commit", "source_fingerprint", "dirty")}
    hashes = {name: digest(output / name) for name in (*PAYLOADS, COLLECTOR)}
    helper = dict(schema=1, reader_version=VERSION, protocol=3, payload_schema=2,
        prog_id="AegisDesk.PptReader", reader_class="AegisDesk.PptReader.Connect",
        reader_clsid="287A80D2-804A-46D3-A0E9-CA31C6FC3D09",
        reader_iid="9A20F4A6-6364-4BA7-930B-3E8857BEAF60",
        auto_install_mode="candidate", auto_install_enabled=True, auto_install_validated=False,
        auto_shutdown_enabled=True, build=build, assembly_versions={name: VERSION for name in PAYLOADS},
        files={name: hashes[name] for name in PAYLOADS})
    capture = dict(schema=3, capture_schema=3, capture_layout=1, reader_version=VERSION,
        protocol=3, mode="candidate", default_enabled=False, build=build, files=hashes,
        source_files={path.relative_to(root).as_posix(): digest(path) for path in source_paths(root)},
        source_transform=dict(reader_version_from=PREVIOUS_VERSION, reader_version_to=VERSION, generated_files=generated),
        native_validation=dict(x86="UNEXECUTED", x64="UNEXECUTED", live_enabled=False))
    for name, value in (("manifest.json", helper), ("capture-manifest.json", capture)):
        with (output / name).open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
    return capture


INSPECTION = r'''param([string]$Directory,[string]$Version)
$ErrorActionPreference='Stop'
foreach($name in @('AegisDesk.PptReader.dll','AegisDesk.PptReader.Events.dll','PptReaderSetup.exe','PptCaptureCollector.exe')) {
 $file=Join-Path $Directory $name
 $identity=[Reflection.AssemblyName]::GetAssemblyName($file)
 if($identity.Version.ToString() -cne $Version -or $identity.Name -cne [IO.Path]::GetFileNameWithoutExtension($name)){throw 'capture_assembly_identity'}
}
$reader=[Reflection.Assembly]::ReflectionOnlyLoadFrom((Join-Path $Directory 'AegisDesk.PptReader.dll'))
foreach($reference in $reader.GetReferencedAssemblies()) {
 if($reference.Name -in @('Microsoft.Office.Interop.PowerPoint','office','AegisDesk.PptReader.Events')){throw 'capture_optional_dependency'}
}
if($reader.GetType('AegisDesk.PptReader.ShutdownBuildPolicy').GetField('Enabled').GetRawConstantValue() -ne $true){throw 'capture_shutdown_policy'}
if($reader.GetType('AegisDesk.PptReader.IPptReaderLifecycle').GUID.ToString().ToUpperInvariant() -cne '9A20F4A6-6364-4BA7-930B-3E8857BEAF60'){throw 'capture_reader_iid'}
$setup=[Reflection.Assembly]::ReflectionOnlyLoadFrom((Join-Path $Directory 'PptReaderSetup.exe'))
if($setup.GetType('AegisDesk.PptSetup.BuildPolicy').GetField('AutoInstallMode').GetRawConstantValue() -cne 'candidate'){throw 'capture_setup_policy'}
'''


def build(info_dir: Path) -> Path:
    """Compile and inspect a fresh diagnostic directory; never install or start Office."""
    if sys.platform != "win32":
        raise ValueError("ppt_capture_windows_build_required")
    info_dir = info_dir.resolve()
    info = verify_metadata(info_dir)
    output = info_dir / "ppt_capture"
    output.mkdir(exist_ok=False)
    generated = output / "generated"
    generated_hashes = copy_sources(generated, info_dir)
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    reader_dir = generated / "scripts/ppt_reader"
    policy = generated / "PptBuildPolicy.cs"

    def compile_one(name: str, target: str, sources: list[Path], references: list[str], capture: bool = False) -> None:
        command = [str(compiler), "/nologo", "/optimize+", "/platform:anycpu", f"/target:{target}", f"/out:{output / name}"]
        if capture:
            command.extend([DEFINES, "/unsafe"])
        command.extend(f"/reference:{reference}" for reference in references)
        subprocess.run(command + [str(path) for path in sources], check=True)

    core = [reader_dir / name for name in CORE] + sorted(reader_dir.glob("PptExternal*.cs"))
    core += [policy, generated / "scripts/ppt_shared/PptExtractionCore.cs"]
    compile_one(PAYLOADS[0], "library", core,
                ["System.Windows.Forms.dll", "Microsoft.CSharp.dll", "System.Core.dll", "System.Web.Extensions.dll"], True)
    pia_root = Path(os.environ["WINDIR"]) / "assembly/GAC_MSIL"
    pia = pia_root / "Microsoft.Office.Interop.PowerPoint/15.0.0.0__71e9bce111e9429c/Microsoft.Office.Interop.PowerPoint.dll"
    office = pia_root / "office/15.0.0.0__71e9bce111e9429c/office.dll"
    # The event assembly keeps the same complete PIA ABI as the normal build.
    pia_check = generated / "CheckPia.ps1"
    pia_check.write_text("param([string]$Ppt,[string]$Office)\n$ErrorActionPreference='Stop'\nforeach($file in @($Ppt,$Office)){ $name=[IO.Path]::GetFileNameWithoutExtension($file); if([Reflection.AssemblyName]::GetAssemblyName($file).FullName -cne \"$name, Version=15.0.0.0, Culture=neutral, PublicKeyToken=71e9bce111e9429c\"){throw 'capture_pia_identity'} }\n", encoding="utf-8")
    subprocess.run(["powershell.exe", "-NoProfile", "-File", str(pia_check), str(pia), str(office)], check=True)
    compile_one(PAYLOADS[1], "library", [generated / "scripts/ppt_events/PptEventSink.cs"],
                [str(output / PAYLOADS[0]), str(pia), str(office)])
    compile_one(PAYLOADS[2], "exe", [reader_dir / "PptReaderSetup.cs", policy] + sorted(reader_dir.glob("PptSetup*.cs")),
                ["System.Core.dll", "System.Web.Extensions.dll", "System.Security.dll"])
    compile_one(COLLECTOR, "exe", [reader_dir / "PptCaptureBuffer.cs", reader_dir / "PptCaptureCollector.cs", generated / "CaptureAssemblyVersion.cs"], [], True)
    inspect = generated / "InspectCapture.ps1"
    inspect.write_text(INSPECTION, encoding="utf-8")
    subprocess.run(["powershell.exe", "-NoProfile", "-File", str(inspect), str(output), VERSION], check=True)
    if verify_metadata(info_dir) != info:
        raise ValueError("ppt_build_source_changed")
    create_manifests(output, info, generated_hashes)
    return output


def main() -> int:
    """Expose only compilation; live/native approval is a separate pinned gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-info-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        build(args.build_info_dir)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print("ppt_capture_build_failed:" + type(error).__name__)
        return 1
    print("PPT capture package compiled; native/live validation remains locked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

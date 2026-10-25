"""Build only the separately pinned, live-locked Reader lifetime diagnostic DLL."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
VERSION = "1.0.7.0"
PREVIOUS_VERSION = "1.0.5.11"
PROTOCOL = 3
IID = "F71674D8-CB02-405A-87D3-59177E1A1487"
CLSID = "287A80D2-804A-46D3-A0E9-CA31C6FC3D09"
PROG_ID = "AegisDesk.PptReader"
DEFINE = "AEGIS_PPT_LIFETIME_DIAGNOSTIC"
DLL = "AegisDesk.PptReader.dll"
MANIFEST = "lifetime-manifest.json"
CORE = ("scripts/ppt_reader/PptReader.cs", "scripts/ppt_reader/PptLifetimeDiagnostic.cs",
        "scripts/ppt_shared/PptExtractionCore.cs")


def digest(path: Path) -> str:
    """Hash one bounded source or diagnostic payload without executing it."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("lifetime_build_file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_paths(root: Path = ROOT) -> list[Path]:
    """Enumerate the exact source set used by the isolated diagnostic candidate."""
    paths = {root / name for name in (*CORE, "scripts/ppt_reader/build_lifetime.py")}
    for pattern in ("scripts/*.py", "scripts/diagnostics/*.py", "scripts/diagnostics/ppt_lifetime/*.py",
                    "scripts/diagnostics/ppt_capture/*.py", "knowmate/*.py", "knowmate/secure/*.py"):
        paths.update(root.glob(pattern))
    return sorted(paths)


def source_hashes(root: Path = ROOT) -> dict[str, str]:
    """Pin every executable source input by its repository-relative path."""
    return {path.relative_to(root).as_posix(): digest(path) for path in source_paths(root)}


def copy_sources(directory: Path, root: Path = ROOT) -> dict[str, str]:
    """Create versioned generated copies while preserving all product source bytes."""
    result: dict[str, str] = {}
    for relative in CORE:
        source = root / relative
        digest(source)
        content = source.read_text(encoding="utf-8-sig")
        if relative.endswith("/PptReader.cs"):
            if (content.count('AssemblyVersion("' + PREVIOUS_VERSION + '")') != 1
                    or content.count('AssemblyFileVersion("' + PREVIOUS_VERSION + '")') != 1):
                raise ValueError("lifetime_source_version")
            content = content.replace('AssemblyVersion("' + PREVIOUS_VERSION + '")', 'AssemblyVersion("' + VERSION + '")')
            content = content.replace('AssemblyFileVersion("' + PREVIOUS_VERSION + '")', 'AssemblyFileVersion("' + VERSION + '")')
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        result[relative] = digest(target)
    return result


def _fixture_hashes() -> dict[str, str]:
    from scripts.diagnostics.ppt_lifetime.fixture import generator_sha256, template_sha256
    return dict(fixture_generator_sha256=generator_sha256(), fixture_template_sha256=template_sha256())


def create_manifest(output: Path, generated: dict[str, str], root: Path = ROOT) -> dict[str, Any]:
    """Write truthful diagnostic metadata without any installer-compatible manifest."""
    value = dict(schema=1, variant="lifetime_diagnostic", reader_version=VERSION, protocol=PROTOCOL,
        reader_iid=IID, reader_clsid=CLSID, prog_id=PROG_ID,
        effective_internal_quit=False, live_enabled=False, files={DLL: digest(output / DLL)},
        source_files=source_hashes(root),
        source_transform=dict(reader_version_from=PREVIOUS_VERSION, reader_version_to=VERSION,
                              define=DEFINE, generated_files=generated),
        native_validation=dict(x86="UNEXECUTED", x64="UNEXECUTED"), provisioning="UNIMPLEMENTED",
        **_fixture_hashes())
    with (output / MANIFEST).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return value


INSPECTION = r'''param([string]$Directory)
$ErrorActionPreference='Stop'
$file=Join-Path $Directory 'AegisDesk.PptReader.dll'
$name=[Reflection.AssemblyName]::GetAssemblyName($file)
if($name.Version.ToString() -cne '1.0.7.0' -or $name.Name -cne 'AegisDesk.PptReader'){throw 'lifetime_assembly_identity'}
$reader=[Reflection.Assembly]::ReflectionOnlyLoadFrom($file)
foreach($reference in $reader.GetReferencedAssemblies()) {
 if($reference.Name -in @('Microsoft.Office.Interop.PowerPoint','office','AegisDesk.PptReader.Events')){throw 'lifetime_optional_dependency'}
}
$service=$reader.GetType('AegisDesk.PptReader.IPptReaderLifetimeDiagnostic')
if($service.GUID.ToString().ToUpperInvariant() -cne 'F71674D8-CB02-405A-87D3-59177E1A1487'){throw 'lifetime_iid'}
foreach($forbidden in @('PptShutdownGuard','PptSinkLoader','PptEventSink','PptDeferredDiagnostic','PptLifecycleTrace','ShutdownBuildPolicy','IPptExternalBootstrap')) {
 if($reader.GetType('AegisDesk.PptReader.'+$forbidden) -ne $null){throw 'lifetime_shutdown_type'}
}
'''


def build(output: Path, root: Path = ROOT) -> Path:
    """Compile and inspect one DLL; never install, register COM, or start Office."""
    if sys.platform != "win32":
        raise ValueError("lifetime_windows_build_required")
    before, fixture_before = source_hashes(root), _fixture_hashes()
    output = output.resolve()
    output.mkdir(exist_ok=False)
    generated = output / "generated"
    copied = copy_sources(generated, root)
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:library", "/platform:anycpu", "/optimize+",
                    "/define:" + DEFINE, "/out:" + str(output / DLL),
                    "/reference:Microsoft.CSharp.dll", "/reference:System.Core.dll",
                    "/reference:System.Web.Extensions.dll", *[str(generated / name) for name in CORE]], check=True)
    inspection = generated / "InspectLifetime.ps1"
    inspection.write_text(INSPECTION, encoding="utf-8")
    subprocess.run(["powershell.exe", "-NoProfile", "-File", str(inspection), str(output)], check=True)
    if source_hashes(root) != before or _fixture_hashes() != fixture_before:
        raise ValueError("lifetime_build_source_changed")
    create_manifest(output, copied, root)
    return output


def main(arguments: list[str] | None = None) -> int:
    """Expose build-only packaging with live/provisioning explicitly unexecuted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(arguments)
    try:
        build(args.output)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print("ppt_lifetime_build_failed:" + type(error).__name__)
        return 1
    print("Lifetime DLL compiled; native, provisioning and live checks remain UNEXECUTED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

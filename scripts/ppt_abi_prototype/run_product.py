"""Exercise actual core/Events binaries with synthetic hosts; never activate Office."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def main() -> int:
    """Build independent callers for both CLR architectures and retain all logs."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    results = []
    for arch in ("x86", "x64"):
        directory = output / arch
        directory.mkdir()
        names = ("AegisDesk.PptReader.dll", "AegisDesk.PptReader.Events.dll")
        for name in names:
            shutil.copyfile(args.payload / name, directory / name)
        hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names}
        record = dict(schema=2, version="1.0.5.15", prog_id="AegisDesk.PptReader",
                      class_id="{287A80D2-804A-46D3-A0E9-CA31C6FC3D09}", scope="Machine",
                      bitness=32 if arch == "x86" else 64, path=str(directory / names[0]),
                      sha256=hashes[names[0]], files=hashes)
        (directory / "installation.json").write_text(json.dumps(record), encoding="utf-8")
        exe = directory / "ProductHarness.exe"
        command = [str(compiler), "/nologo", "/target:exe", f"/platform:{arch}", f"/out:{exe}",
                   "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll",
                   f"/reference:{directory / names[0]}",
                   *[str(source / name) for name in ("ProductHarness.cs", "ProductDispatch.cs",
                      "ProductContractFixtures.cs", "ProductGuardFixtures.cs", "NativeMock.cs")]]
        build = subprocess.run(command, capture_output=True, timeout=30)
        (directory / "compile.log").write_bytes(build.stdout + build.stderr)
        if build.returncode:
            results.append(dict(arch=arch, status="COMPILE_FAIL"))
            continue
        try:
            run = subprocess.run([str(exe), str(source / "host_contract.json")], capture_output=True, timeout=60)
            (directory / "run.log").write_bytes(run.stdout + run.stderr)
            results.append(dict(arch=arch, status="PASS" if run.returncode == 0 else "FAIL",
                                summary=run.stdout.decode("ascii", errors="replace").splitlines()[-1:]))
        except subprocess.TimeoutExpired as error:
            (directory / "run.log").write_bytes((error.stdout or b"") + (error.stderr or b""))
            results.append(dict(arch=arch, status="TIMEOUT"))
        for case in ("events-missing", "hash-mismatch", "partial-record", "foreign-file", "old-schema",
                     "wrong-class", "wrong-bitness", "wrong-identity", "powerpoint-unavailable", "office-unavailable",
                     "callback-fault", "callback-fault-quit", "events-cached-build", "core-cached-build"):
            case_dir = directory / case
            case_dir.mkdir()
            case_exe = case_dir / exe.name
            shutil.copyfile(exe, case_exe)
            shutil.copyfile(directory / names[0], case_dir / names[0])
            if case != "events-missing":
                shutil.copyfile(directory / names[1], case_dir / names[1])
            case_record = copy.deepcopy(record)
            case_record["path"] = str(case_dir / names[0])
            extra = []
            if case.endswith("cached-build"):
                variant = case_dir / "variant"
                variant.mkdir()
                marker = variant / "Marker.cs"
                marker.write_text('[assembly:System.Reflection.AssemblyDescription("independent fixture build")]')
                core_case = case.startswith("core")
                target = variant / names[0] if core_case else case_dir / names[1]
                command = [str(compiler), "/nologo", "/target:library", f"/out:{target}",
                           "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll",
                           "/reference:Microsoft.CSharp.dll", str(marker)]
                if core_case:
                    reader = source.parent / "ppt_reader"
                    command += [str(reader / name) for name in (
                        "PptReader.cs", "PptCallerQuit.cs", "PptShutdownEvents.cs", "PptOwnedMetadata.cs", "PptShutdownGuard.cs", "PptShutdownState.cs",
                        "PptSinkLoader.cs", "PptPackageIdentity.cs", "PptEventSignatures.cs", "PptEventValidator.cs")]
                    command += [str(source.parent / "ppt_shared/PptExtractionCore.cs"),
                                str(args.payload.resolve().parent / "PptBuildPolicy.cs")]
                else:
                    gac = Path(os.environ["WINDIR"]) / "assembly/GAC_MSIL"
                    command += [f"/reference:{case_dir / names[0]}",
                                *[f"/reference:{gac / name / '15.0.0.0__71e9bce111e9429c' / (name + '.dll')}"
                                  for name in ("Microsoft.Office.Interop.PowerPoint", "office")],
                                str(source.parent / "ppt_events/PptEventSink.cs")]
                built = subprocess.run(command, capture_output=True, timeout=30)
                (case_dir / "compile.log").write_bytes(built.stdout + built.stderr)
                if built.returncode:
                    results.append(dict(arch=arch, case=case, status="COMPILE_FAIL"))
                    continue
                variant_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                extra = [str(target), variant_hash]
                if not core_case:
                    case_record["files"][names[1]] = variant_hash
                    extra.insert(0, str(directory / names[1]))
            if case == "hash-mismatch":
                case_record["files"][names[1]] = "0" * 64
            elif case == "partial-record":
                del case_record["files"][names[1]]
            elif case == "foreign-file":
                case_record["files"]["foreign.dll"] = "0" * 64
            elif case == "old-schema":
                case_record["schema"] = 1
            elif case == "wrong-class":
                case_record["class_id"] = "{00000000-0000-0000-0000-000000000000}"
            elif case == "wrong-bitness":
                case_record["bitness"] = 64 if arch == "x86" else 32
            elif case == "wrong-identity":
                shutil.copyfile(directory / names[0], case_dir / names[1])
                case_record["files"][names[1]] = hashes[names[0]]
            elif case in {"powerpoint-unavailable", "office-unavailable"}:
                dependency = "office" if case == "office-unavailable" else "Microsoft.Office.Interop.PowerPoint"
                case_exe.with_suffix(".exe.config").write_text(
                    '<configuration><runtime><assemblyBinding xmlns="urn:schemas-microsoft-com:asm.v1">'
                    '<dependentAssembly>'
                    f'<assemblyIdentity name="{dependency}" publicKeyToken="71e9bce111e9429c" culture="neutral" />'
                    '<bindingRedirect oldVersion="0.0.0.0-15.0.0.0" newVersion="99.0.0.0" />'
                    '<publisherPolicy apply="no" /></dependentAssembly></assemblyBinding></runtime></configuration>')
            elif case.startswith("callback-fault"):
                fault = case_dir / "Fault.cs"
                fault.write_text('namespace AegisDesk.PptEvents { public sealed partial class PptEventSink { '
                                 'partial void CancelCheckpoint() { throw new System.InvalidOperationException("synthetic"); } } }')
                gac = Path(os.environ["WINDIR"]) / "assembly/GAC_MSIL"
                command = [str(compiler), "/nologo", "/target:library", f"/out:{case_dir / names[1]}",
                           f"/reference:{case_dir / names[0]}",
                           *[f"/reference:{gac / name / '15.0.0.0__71e9bce111e9429c' / (name + '.dll')}"
                             for name in ("Microsoft.Office.Interop.PowerPoint", "office")],
                           str(source.parent / "ppt_events/PptEventSink.cs"), str(fault)]
                build = subprocess.run(command, capture_output=True, timeout=30)
                (case_dir / "compile.log").write_bytes(build.stdout + build.stderr)
                if build.returncode:
                    results.append(dict(arch=arch, case=case, status="COMPILE_FAIL"))
                    continue
                case_record["files"][names[1]] = hashlib.sha256((case_dir / names[1]).read_bytes()).hexdigest()
            (case_dir / "installation.json").write_text(json.dumps(case_record), encoding="utf-8")
            run = subprocess.run([str(case_exe), str(source / "host_contract.json"), case, *extra], capture_output=True, timeout=30)
            (case_dir / "run.log").write_bytes(run.stdout + run.stderr)
            results.append(dict(arch=arch, case=case, status="PASS" if run.returncode == 0 else "FAIL",
                                summary=run.stdout.decode("ascii", errors="replace").splitlines()[-1:]))
    (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results))
    return 0 if all(row["status"] == "PASS" for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Probe optional PIA loading with local executable binding policy; never edit GAC."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main() -> int:
    """Build an unchanged core plus separate sink and run isolated dependency failures."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent
    reader = source.parent / "ppt_reader"
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    subprocess.run(["powershell.exe", "-NoProfile", "-File", str(source / "generate_sink.ps1"),
                    "-OutputDirectory", str(output)], check=True, timeout=30)
    references = json.loads((output / "references.json").read_text(encoding="utf-8-sig"))
    policy = output / "ProbePolicy.cs"
    policy.write_text("namespace AegisDesk.PptReader { internal static class ShutdownBuildPolicy { public const bool Enabled = true; } }\n")
    core = output / "AegisDesk.PptReader.CoreProbe.dll"
    commands = [
        [str(compiler), "/nologo", "/target:library", "/platform:anycpu", f"/out:{core}",
         "/reference:Microsoft.CSharp.dll", "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll",
         *[str(reader / name) for name in ("PptReader.cs", "PptShutdownGuard.cs", "PptShutdownEvents.cs", "PptOwnedMetadata.cs", "PptShutdownState.cs", "PptSinkLoader.cs", "PptPackageIdentity.cs", "PptEventSignatures.cs", "PptEventValidator.cs")],
         str(reader.parent / "ppt_shared/PptExtractionCore.cs"), str(policy)],
        [str(compiler), "/nologo", "/target:library", "/platform:anycpu",
         f"/out:{output / 'OptionalSink.Probe.dll'}", f"/reference:{references['pia']}",
         f"/reference:{references['office']}", str(output / "PrototypeSink.cs")],
    ]
    for index, command in enumerate(commands):
        run = subprocess.run(command, capture_output=True, timeout=30)
        (output / f"compile-{index}.log").write_bytes(run.stdout + run.stderr)
        if run.returncode:
            raise RuntimeError(f"compile_failed:{index}")
    results = []
    for architecture in ("x64", "x86"):
        base = output / f"LoaderProbe.{architecture}.exe"
        command = [str(compiler), "/nologo", "/target:exe", f"/platform:{architecture}",
                   f"/out:{base}", "/reference:System.Core.dll", f"/reference:{core}", str(source / "LoaderProbe.cs")]
        run = subprocess.run(command, capture_output=True, timeout=30)
        (output / f"compile-{architecture}.log").write_bytes(run.stdout + run.stderr)
        if run.returncode:
            raise RuntimeError(f"compile_failed:{architecture}")
        for case in ("ready", "powerpoint-unavailable", "office-unavailable", "adapter-missing"):
            executable = output / f"LoaderProbe.{architecture}.{case}.exe"
            shutil.copyfile(base, executable)
            if case in {"powerpoint-unavailable", "office-unavailable"}:
                name = "office" if case == "office-unavailable" else "Microsoft.Office.Interop.PowerPoint"
                # Only this synthetic executable is redirected to a nonexistent version.
                executable.with_suffix(".exe.config").write_text(
                    '<configuration><runtime><assemblyBinding xmlns="urn:schemas-microsoft-com:asm.v1">'
                    '<dependentAssembly>'
                    f'<assemblyIdentity name="{name}" publicKeyToken="71e9bce111e9429c" culture="neutral" />'
                    '<bindingRedirect oldVersion="0.0.0.0-15.0.0.0" newVersion="99.0.0.0" />'
                    '<publisherPolicy apply="no" />'
                    '</dependentAssembly></assemblyBinding></runtime></configuration>', encoding="utf-8",
                )
            adapter = output / ("Absent.Optional.dll" if case == "adapter-missing" else "OptionalSink.Probe.dll")
            run = subprocess.run([str(executable), str(adapter), "ready" if case == "ready" else "unavailable"],
                                 capture_output=True, timeout=30)
            (output / f"{architecture}-{case}.log").write_bytes(run.stdout + run.stderr)
            row = {"arch": architecture, "case": case, "exit": run.returncode,
                   "summary": run.stdout.decode("ascii", errors="replace").splitlines()[-1:]}
            results.append(row)
            print(json.dumps(row), flush=True)
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    return 0 if all(row["exit"] == 0 for row in results) else 1


if __name__ == "__main__":
    sys.exit(main())

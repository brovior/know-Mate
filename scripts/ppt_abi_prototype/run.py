"""Build/run isolated Framework COM ABI probes without starting Office."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    """Compile x86/x64 embedded/reference probes and preserve bounded output."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    source = Path(__file__).resolve().parent
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(source / "generate_sink.ps1"),
         "-OutputDirectory", str(output)], check=True, timeout=30,
    )
    references = json.loads((output / "references.json").read_text(encoding="utf-8-sig"))
    results = []
    for architecture in ("x64", "x86"):
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET" / (
            "Framework64" if architecture == "x64" else "Framework"
        ) / "v4.0.30319" / "csc.exe"
        for linkage in ("embed", "reference"):
            prefix = f"{architecture}-{linkage}"
            executable = output / f"AbiHarness.{prefix}.exe"
            flag = "/link:" if linkage == "embed" else "/reference:"
            command = [str(compiler), "/nologo", "/target:exe", f"/platform:{architecture}",
                       f"/out:{executable}", "/reference:System.Core.dll",
                       "/reference:System.Web.Extensions.dll",
                       flag + references["pia"], flag + references["office"],
                       str(output / "PrototypeSink.cs"), str(source / "NativeMock.cs"),
                       str(source / "AbiHarness.cs"), str(source / "DispatchChecks.cs"),
                       str(source / "ContractValidator.cs"), str(source / "ContractFixtures.cs")]
            compiled = subprocess.run(command, capture_output=True, timeout=30)
            (output / f"{prefix}-compile.log").write_bytes(compiled.stdout + compiled.stderr)
            if compiled.returncode:
                result = {"case": prefix, "status": "COMPILE_FAIL", "exit": compiled.returncode}
            else:
                try:
                    run = subprocess.run([str(executable), str(source / "host_contract.json"), linkage],
                                         capture_output=True, timeout=60)
                    (output / f"{prefix}.log").write_bytes(run.stdout + run.stderr)
                    status = {0: "PASS", 2: "PARTIAL"}.get(run.returncode, "FAIL")
                    result = {"case": prefix, "status": status, "exit": run.returncode,
                              "summary": run.stdout.decode("ascii", errors="replace").splitlines()[-1:]}
                    if linkage == "reference" and run.returncode == 0:
                        text = run.stdout.decode("ascii", errors="replace")
                        result["bindings_match_build"] = all(
                            f"identity={references[key + 'Identity']} path={references[key]} sha256={references[key + 'Sha256']}".casefold() in text.casefold()
                            for key in ("pia", "office")
                        )
                        if not result["bindings_match_build"]:
                            result["status"] = "FAIL"
                except subprocess.TimeoutExpired as exc:
                    # subprocess.run kills/waits only this directly-created probe child.
                    (output / f"{prefix}.log").write_bytes((exc.stdout or b"") + (exc.stderr or b""))
                    result = {"case": prefix, "status": "TIMEOUT"}
            results.append(result)
            print(json.dumps(result), flush=True)
        reader = source.parent / "ppt_reader"
        # Combined product guard coverage is provided by run_product.py.
        executable = output / f"RollbackFixtures.{architecture}.exe"
        compiled = subprocess.run(
            [str(compiler), "/nologo", "/target:exe", f"/platform:{architecture}",
             f"/out:{executable}", "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll",
             str(source / "ProductSetupMemory.cs"), str(source / "ProductSetupFixtures.cs"),
             str(reader / "PptSetupMutation.cs"), str(reader / "PptSetupInstallRootPolicy.cs"),
             str(reader / "PptSetupOwnership.cs"), str(reader / "PptSetupMachine.cs")], capture_output=True, timeout=30,
        )
        (output / f"{architecture}-rollback-compile.log").write_bytes(compiled.stdout + compiled.stderr)
        if compiled.returncode:
            result = {"case": architecture + "-rollback", "status": "COMPILE_FAIL"}
        else:
            try:
                run = subprocess.run([str(executable)], capture_output=True, timeout=30)
                (output / f"{architecture}-rollback.log").write_bytes(run.stdout + run.stderr)
                result = {"case": architecture + "-rollback", "status": "PASS" if run.returncode == 0 else "FAIL",
                          "summary": run.stdout.decode("ascii", errors="replace").splitlines()[-1:]}
            except subprocess.TimeoutExpired as exc:
                (output / f"{architecture}-rollback.log").write_bytes((exc.stdout or b"") + (exc.stderr or b""))
                result = {"case": architecture + "-rollback", "status": "TIMEOUT"}
        results.append(result)
        print(json.dumps(result), flush=True)
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return 0 if all(row["status"] == "PASS" for row in results) else 1


if __name__ == "__main__":
    sys.exit(main())

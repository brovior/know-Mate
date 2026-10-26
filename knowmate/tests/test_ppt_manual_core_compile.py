"""Compile actual manual Core source lists without loading DLLs or Office."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def framework_compiler() -> list[str]:
    """Use Windows Framework or an explicitly configured portable reference compiler."""
    if os.name == "nt":
        csc = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
        if csc.is_file():
            return [str(csc), "/nologo", "/reference:Microsoft.CSharp.dll",
                    "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll"]
    dotnet = os.environ.get("AEGIS_TEST_DOTNET") or shutil.which("dotnet")
    references = Path(os.environ.get("AEGIS_TEST_NETFRAMEWORK_REFS", ""))
    if dotnet and (references / "mscorlib.dll").is_file():
        compilers = sorted(Path(dotnet).resolve().parent.glob("sdk/*/Roslyn/bincore/csc.dll"))
        if compilers:
            return [dotnet, str(compilers[-1]), "/nologo", "/nostdlib+", "/langversion:5",
                    *("/reference:" + str(path) for path in sorted(references.glob("*.dll"))
                      if not path.name.endswith((".Wrapper.dll", ".Thunk.dll")))]
    pytest.skip("Manual Core compile UNEXECUTED: Framework compiler/references unavailable")


@pytest.mark.parametrize("script", ["run_loader.py", "run_product.py"])
def test_actual_manual_core_source_list_compiles(tmp_path: Path, script: str) -> None:
    """Fail on missing production types rather than just checking one filename."""
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "scripts/ppt_abi_prototype" / script).read_text())
    lists = [[item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
             for node in ast.walk(tree) if isinstance(node, (ast.Tuple, ast.List))]
    core_lists = [names for names in lists if "PptSinkLoader.cs" in names]
    assert len(core_lists) == 1
    assert {"PptRawCount.cs", "PptSetupInstallRootPolicy.cs"} <= set(core_lists[0])
    policy = tmp_path / "Policy.cs"
    policy.write_text('namespace AegisDesk.PptReader { internal static class ShutdownBuildPolicy { '
                      'public const bool Enabled=true; } }', encoding="utf-8")
    sources = [root / "scripts/ppt_reader" / name for name in core_lists[0]]
    sources += [root / "scripts/ppt_shared/PptExtractionCore.cs", policy]
    compiled = subprocess.run([*framework_compiler(), "/target:library", "/out:" + str(tmp_path / "Core.dll"),
                               *map(str, sources)], capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr

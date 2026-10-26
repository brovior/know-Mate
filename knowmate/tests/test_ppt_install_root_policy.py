"""Execute the production C# writer predicate without changing any real ACL."""
from __future__ import annotations

import os
import ast
from pathlib import Path
import shutil
import subprocess

import pytest

TI = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
FULL = 0x1F01FF


@pytest.fixture(scope="module")
def policy_caller(tmp_path_factory: pytest.TempPathFactory):
    """Compile the exact portable policy source; no installer or Office is executed."""
    dotnet = os.environ.get("AEGIS_TEST_DOTNET") or shutil.which("dotnet")
    if not dotnet:
        pytest.skip("C# policy execution UNEXECUTED: .NET SDK unavailable")
    temporary = tmp_path_factory.mktemp("install-root-policy")
    source = Path(__file__).resolve().parents[2] / "scripts/ppt_reader/PptSetupInstallRootPolicy.cs"
    shutil.copyfile(source, temporary / source.name)
    (temporary / "Policy.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
        '<TargetFramework>net8.0</TargetFramework><EnableNETAnalyzers>false</EnableNETAnalyzers>'
        '</PropertyGroup></Project>', encoding="utf-8")
    (temporary / "NuGet.Config").write_text(
        '<configuration><packageSources><clear /></packageSources></configuration>', encoding="utf-8")
    (temporary / "Program.cs").write_text(
        'using System; using System.Security.AccessControl; using AegisDesk.PptSetup; '
        'class Program { static void Main(string[] a) { '
        'bool allowed = InstallRootPolicy.TrustedWriter(a[0], a[1], a[2], '
        '(InheritanceFlags)int.Parse(a[3]), (PropagationFlags)int.Parse(a[4]), '
        '(FileSystemRights)int.Parse(a[5]), bool.Parse(a[6])); Console.WriteLine(allowed ? "ALLOW" : "DENY"); } }',
        encoding="utf-8")
    environment = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1")
    built = subprocess.run([dotnet, "build", "Policy.csproj", "--configuration", "Release", "--nologo",
                            "--verbosity", "quiet"], cwd=temporary, env=environment,
                           capture_output=True, text=True, timeout=90)
    assert built.returncode == 0, built.stdout + built.stderr
    parent = temporary / "Program Files" / "AegisDesk"
    reader = parent / "PptReader"

    def run(sid: str, location: str = "parent", inheritance: int = 0,
            propagation: int = 0, rights: int = FULL, is_directory: bool = True) -> bool:
        locations = {"parent": str(parent), "reader": str(reader), "version": str(reader / "1.0.5.16"),
                     "file": str(reader / "AegisDesk.PptReader.dll"), "foreign": str(parent) + "Extra",
                     "case": str(parent).upper(), "empty": ""}
        result = subprocess.run([dotnet, str(temporary / "bin/Release/net8.0/Policy.dll"), locations[location],
                                 str(reader), sid, str(inheritance), str(propagation), str(rights), str(is_directory)],
                                env=environment, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() in {"ALLOW", "DENY"}
        return result.stdout.strip() == "ALLOW"

    return run


@pytest.mark.parametrize("location", ["parent", "case"])
def test_exact_trustedinstaller_folder_only_write_is_accepted(policy_caller, location):
    assert policy_caller(TI, location)


@pytest.mark.parametrize("location", ["reader", "version", "file", "foreign", "empty"])
def test_trustedinstaller_exception_does_not_extend_to_reader_or_other_paths(policy_caller, location):
    assert not policy_caller(TI, location)


@pytest.mark.parametrize("sid", ["NT SERVICE\\TrustedInstaller", TI + "-1", TI[:-1] + "5",
                                  "S-1-5-80-1-2-3-4-5", "S-1-5-32-545", "S-1-5-11", "S-1-1-0"])
def test_display_name_lookalike_service_and_nonadmin_writers_remain_rejected(policy_caller, sid):
    assert not policy_caller(sid)


@pytest.mark.parametrize("inheritance,propagation", [(1, 0), (2, 0), (3, 0), (1, 2), (0, 1), (0, 2)])
def test_trustedinstaller_child_scope_and_propagation_are_not_whitelisted(policy_caller, inheritance, propagation):
    assert not policy_caller(TI, inheritance=inheritance, propagation=propagation)


def test_trustedinstaller_unknown_access_bits_remain_rejected(policy_caller):
    assert not policy_caller(TI, rights=FULL | 0x01000000)


@pytest.mark.parametrize("sid", ["S-1-5-32-544", "S-1-5-18"])
@pytest.mark.parametrize("location", ["parent", "reader", "version"])
@pytest.mark.parametrize("is_directory", [True, False])
def test_original_admin_system_writer_contract_is_preserved(policy_caller, sid, location, is_directory):
    assert policy_caller(sid, location, inheritance=3, is_directory=is_directory)


@pytest.mark.parametrize("location", ["parent", "case", "reader", "file"])
def test_file_acl_never_receives_trustedinstaller_directory_exception(policy_caller, location):
    assert not policy_caller(TI, location, is_directory=False)


def test_setup_and_runtime_loader_share_the_same_writer_predicate():
    """Prevent the independent ACL allowlists that caused the runtime-only rejection."""
    root = Path(__file__).resolve().parents[2]
    setup = (root / "scripts/ppt_reader/PptSetupMutation.cs").read_text()
    loader = (root / "scripts/ppt_reader/PptSinkLoader.cs").read_text()
    gate = loader[loader.index("private static void Protected"):loader.index("internal static object Create")]
    assert "InstallRootPolicy.TrustedWriter(" in setup
    assert "InstallRootPolicy.TrustedWriter(" in gate
    assert "!sid.IsWellKnown" not in gate
    assert "Protected(path,false,root)" in loader


def test_every_reader_compile_path_includes_the_shared_policy_source():
    """Catch source-list omissions on Linux before a Windows C# compile is attempted."""
    from scripts.ppt_reader import build_capture
    root = Path(__file__).resolve().parents[2]
    policy = "PptSetupInstallRootPolicy.cs"
    assert policy in build_capture.CORE
    script = (root / "scripts/ppt_reader/build.ps1").read_text()
    compile_reader = script[script.index("& $compiler /nologo /target:library"):
                            script.index("throw 'ppt_reader_build_failed'")]
    assert policy in compile_reader
    for relative in ("scripts/ppt_abi_prototype/run_product.py", "scripts/ppt_abi_prototype/run_loader.py",
                     "knowmate/tests/test_ppt_owned_connection.py", "knowmate/tests/test_ppt_bundle.py",
                     "knowmate/tests/test_ppt_raw_count.py"):
        tree = ast.parse((root / relative).read_text())
        lists = [[item.value for item in node.elts if isinstance(item, ast.Constant)] for node in ast.walk(tree)
                 if isinstance(node, (ast.Tuple, ast.List))]
        consumers = [names for names in lists if "PptSinkLoader.cs" in names]
        assert consumers and all(policy in names for names in consumers), relative

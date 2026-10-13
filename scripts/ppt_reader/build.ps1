<# Precompile only. This script never starts Office or registers COM. #>
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$BuildInfoDir)
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
if (!(Test-Path $compiler)) { throw 'ppt_compiler_missing' }
$out = Join-Path $PSScriptRoot 'bin'
New-Item -ItemType Directory -Path $out -Force | Out-Null
$reader = Join-Path $out 'AegisDesk.PptReader.dll'
$setup = Join-Path $out 'PptReaderSetup.exe'
# Overwrite compile outputs on every build; a failed compile cannot reuse stale payloads.
Remove-Item (Join-Path $out 'manifest.json') -ErrorAction SilentlyContinue
& $compiler /nologo /target:library /platform:anycpu /optimize+ "/out:$reader" `
    /reference:Microsoft.CSharp.dll /reference:System.Core.dll /reference:System.Web.Extensions.dll `
    (Join-Path $PSScriptRoot 'PptReader.cs') (Join-Path $PSScriptRoot '..\ppt_shared\PptExtractionCore.cs')
if ($LASTEXITCODE -ne 0) { throw 'ppt_reader_build_failed' }
$setupSources = @(Get-ChildItem (Join-Path $PSScriptRoot 'PptSetup*.cs') | ForEach-Object { $_.FullName })
& $compiler /nologo /target:exe /platform:anycpu /optimize+ "/out:$setup" `
    /reference:System.Core.dll /reference:System.Web.Extensions.dll /reference:System.Security.dll `
    (Join-Path $PSScriptRoot 'PptReaderSetup.cs') @setupSources
if ($LASTEXITCODE -ne 0) { throw 'ppt_setup_build_failed' }
foreach ($payload in @($reader, $setup)) {
    if ([Reflection.AssemblyName]::GetAssemblyName($payload).Version.ToString() -ne '1.0.0.0') {
        throw 'ppt_payload_assembly_version_mismatch'
    }
}
$python = Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'
& $python (Join-Path $PSScriptRoot 'manifest.py') --payload-dir $out --build-info-dir $BuildInfoDir
if ($LASTEXITCODE -ne 0) { throw 'ppt_payload_manifest_failed' }
Write-Output 'PPT payload compiled and verified (no registration, auto-install prototype gate closed).'

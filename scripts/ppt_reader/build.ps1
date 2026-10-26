<# Precompile only. This script never starts Office or registers COM. #>
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$BuildInfoDir, [switch]$DeferredDiagnostic, [switch]$ExternalDiagnostic, [switch]$CaptureDiagnostic)
$ErrorActionPreference = 'Stop'
if ($CaptureDiagnostic) {
    $capturePython = Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'
    & $capturePython (Join-Path $PSScriptRoot 'build_capture.py') --build-info-dir $BuildInfoDir
    if ($LASTEXITCODE -ne 0) { throw 'ppt_capture_build_failed' }
    return
}
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
if (!(Test-Path $compiler)) { throw 'ppt_compiler_missing' }
$infoModule = Join-Path $BuildInfoDir 'aegisdesk_build_info.py'
$policySource = Join-Path $BuildInfoDir 'PptBuildPolicy.cs'
if (!(Test-Path $infoModule) -or !(Test-Path $policySource)) { throw 'ppt_build_policy_missing' }
# Metadata, generated policy and payloads share one mode-specific build directory.
$out = Join-Path $BuildInfoDir 'ppt_reader'

New-Item -ItemType Directory -Path $out -Force | Out-Null
$reader = Join-Path $out 'AegisDesk.PptReader.dll'
$events = Join-Path $out 'AegisDesk.PptReader.Events.dll'
$setup = Join-Path $out 'PptReaderSetup.exe'
# Overwrite compile outputs on every build; a failed compile cannot reuse stale payloads.
Remove-Item (Join-Path $out 'manifest.json') -ErrorAction SilentlyContinue
$diagnosticArgs = @()
if ($DeferredDiagnostic -or $ExternalDiagnostic) {
    # Explicit diagnostic build only; normal payloads do not contain the new IID.
    $diagnosticArgs = @('/define:AEGIS_PPT_DEFERRED_DIAGNOSTIC', '/reference:System.Windows.Forms.dll')
}
if ($ExternalDiagnostic) {
    $diagnosticArgs = @('/define:AEGIS_PPT_DEFERRED_DIAGNOSTIC;AEGIS_PPT_EXTERNAL_DIAGNOSTIC', '/reference:System.Windows.Forms.dll')
}
$externalSources = @(Get-ChildItem (Join-Path $PSScriptRoot 'PptExternal*.cs') | ForEach-Object { $_.FullName })
& $compiler /nologo /target:library /platform:anycpu /optimize+ "/out:$reader" `
    @diagnosticArgs @externalSources `
    /reference:Microsoft.CSharp.dll /reference:System.Core.dll /reference:System.Web.Extensions.dll `
    (Join-Path $PSScriptRoot 'PptReader.cs') (Join-Path $PSScriptRoot 'PptCallerQuit.cs') (Join-Path $PSScriptRoot 'PptShutdownEvents.cs') (Join-Path $PSScriptRoot 'PptOwnedMetadata.cs') `
    (Join-Path $PSScriptRoot 'PptShutdownGuard.cs') (Join-Path $PSScriptRoot 'PptRawCount.cs') $policySource `
    (Join-Path $PSScriptRoot 'PptShutdownState.cs') (Join-Path $PSScriptRoot 'PptSinkLoader.cs') `
    (Join-Path $PSScriptRoot 'PptPackageIdentity.cs') `
    (Join-Path $PSScriptRoot 'PptDeferredGate.cs') (Join-Path $PSScriptRoot 'PptDeferredDiagnostic.cs') `
    (Join-Path $PSScriptRoot 'PptEventSignatures.cs') (Join-Path $PSScriptRoot 'PptEventValidator.cs') `
    (Join-Path $PSScriptRoot '..\ppt_shared\PptExtractionCore.cs')
if ($LASTEXITCODE -ne 0) { throw 'ppt_reader_build_failed' }
$piaRoot = Join-Path $env:WINDIR 'assembly\GAC_MSIL'
$pia = Join-Path $piaRoot 'Microsoft.Office.Interop.PowerPoint\15.0.0.0__71e9bce111e9429c\Microsoft.Office.Interop.PowerPoint.dll'
$office = Join-Path $piaRoot 'office\15.0.0.0__71e9bce111e9429c\office.dll'
foreach ($reference in @($pia, $office)) {
    $name = [IO.Path]::GetFileNameWithoutExtension($reference)
    if ([Reflection.AssemblyName]::GetAssemblyName($reference).FullName -cne "$name, Version=15.0.0.0, Culture=neutral, PublicKeyToken=71e9bce111e9429c") { throw 'ppt_pia_identity' }
}
& $compiler /nologo /target:library /platform:anycpu /optimize+ "/out:$events" `
    "/reference:$reader" "/reference:$pia" "/reference:$office" `
    (Join-Path $PSScriptRoot '..\ppt_events\PptEventSink.cs')
if ($LASTEXITCODE -ne 0) { throw 'ppt_events_build_failed' }
$setupSources = @(Get-ChildItem (Join-Path $PSScriptRoot 'PptSetup*.cs') | ForEach-Object { $_.FullName })
& $compiler /nologo /target:exe /platform:anycpu /optimize+ "/out:$setup" `
    /reference:System.Core.dll /reference:System.Web.Extensions.dll /reference:System.Security.dll `
    (Join-Path $PSScriptRoot 'PptReaderSetup.cs') $policySource @setupSources
if ($LASTEXITCODE -ne 0) { throw 'ppt_setup_build_failed' }
foreach ($payload in @($reader, $events, $setup)) {
    if ([Reflection.AssemblyName]::GetAssemblyName($payload).Version.ToString() -ne '1.0.5.15') {
        throw 'ppt_payload_assembly_version_mismatch'
    }
}
$python = Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'
$expectedMode = & $python -c "import runpy,sys; print(runpy.run_path(sys.argv[1])['BUILD_INFO']['ppt_auto_install_mode'])" $infoModule
if ($LASTEXITCODE -ne 0) { throw 'ppt_build_policy_missing' }
# Reflection-only inspection reads a constant; it never invokes setup or Office.
$assembly = [Reflection.Assembly]::ReflectionOnlyLoadFrom($setup)
$embeddedMode = $assembly.GetType('AegisDesk.PptSetup.BuildPolicy').GetField('AutoInstallMode').GetRawConstantValue()
if ($embeddedMode -ne $expectedMode) { throw 'ppt_setup_policy_mismatch' }
$readerAssembly = [Reflection.Assembly]::ReflectionOnlyLoadFrom($reader)
foreach ($reference in $readerAssembly.GetReferencedAssemblies()) {
    if ($reference.Name -in @('Microsoft.Office.Interop.PowerPoint', 'office', 'AegisDesk.PptReader.Events')) { throw 'ppt_core_optional_dependency' }
}
$shutdownEnabled = $readerAssembly.GetType('AegisDesk.PptReader.ShutdownBuildPolicy').GetField('Enabled').GetRawConstantValue()
if ($shutdownEnabled -ne ($expectedMode -eq 'candidate')) { throw 'ppt_shutdown_policy_mismatch' }
$service = $readerAssembly.GetType('AegisDesk.PptReader.IPptReaderLifecycle')
if ($service.GUID.ToString().ToUpperInvariant() -ne '9A20F4A6-6364-4BA7-930B-3E8857BEAF60') {
    throw 'ppt_reader_iid_mismatch'
}
& $python (Join-Path $PSScriptRoot 'manifest.py') --payload-dir $out --build-info-dir $BuildInfoDir
if ($LASTEXITCODE -ne 0) { throw 'ppt_payload_manifest_failed' }
Write-Output 'PPT payload compiled and verified (no registration performed).'

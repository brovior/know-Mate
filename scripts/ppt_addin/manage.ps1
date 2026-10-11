<#
Build는 컴파일만 한다. Install/Remove는 사내 허용 후 사용자가 직접 실행한다.
실험용 고유 CLSID·ProgID의 현재 사용자 레지스트리만 변경한다.
#>
[CmdletBinding()]
param(
    [ValidateSet('Build', 'Install', 'Remove')][string]$Action = 'Build',
    [ValidateSet('32', '64')][string]$OfficeBitness
)
$ErrorActionPreference = 'Stop'
$probeClassId = '{02BE8BA9-C02B-48AD-ADBB-9B5686D72B1E}'
$probeProgId = 'AegisDesk.PptReadProbe'
$probeDll = Join-Path $PSScriptRoot 'bin\PptReadProbe.dll'

if ($Action -eq 'Build') {
    $probeCompiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
    New-Item -ItemType Directory -Path (Split-Path -Parent $probeDll) -Force | Out-Null
    & $probeCompiler /nologo /target:library /platform:anycpu /optimize+ "/out:$probeDll" `
        /reference:Microsoft.CSharp.dll /reference:System.Core.dll /reference:System.Web.Extensions.dll `
        (Join-Path $PSScriptRoot 'PptReadProbe.cs')
    if ($LASTEXITCODE -ne 0) { throw 'probe_build_failed' }
    Write-Output "Build complete: $probeDll (registration not performed)"
    return
}

if (!$OfficeBitness) { throw 'Install/Remove에는 OfficeBitness 32 또는 64가 필요합니다.' }
if (Get-Process -Name POWERPNT -ErrorAction SilentlyContinue) {
    throw 'PowerPoint를 사용자가 정상 종료한 뒤 다시 실행하세요. 프로세스를 강제 종료하지 않습니다.'
}
$probeView = if ($OfficeBitness -eq '32') {
    [Microsoft.Win32.RegistryView]::Registry32
} else { [Microsoft.Win32.RegistryView]::Registry64 }
$probeRoot = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
    [Microsoft.Win32.RegistryHive]::CurrentUser, $probeView)
$probeClassKey = "Software\Classes\CLSID\$probeClassId"
$probeProgKey = "Software\Classes\$probeProgId"
$probeOfficeKey = "Software\Microsoft\Office\PowerPoint\Addins\$probeProgId"
try {
    if ($Action -eq 'Remove') {
        foreach ($probeKey in @($probeOfficeKey, $probeProgKey, $probeClassKey)) {
            $probeRoot.DeleteSubKeyTree($probeKey, $false)
        }
        Write-Output 'Probe registration removed; other add-ins unchanged.'
        return
    }
    if (!(Test-Path -LiteralPath $probeDll -PathType Leaf)) { throw 'Build를 먼저 실행하세요.' }
    $probeAssembly = [Reflection.AssemblyName]::GetAssemblyName($probeDll).FullName
    $probeValues = @{
        $probeClassKey = @{ '' = 'Aegis Desk PPT read probe' }
        "$probeClassKey\InprocServer32" = @{
            '' = 'mscoree.dll'; ThreadingModel = 'Both'; Class = 'AegisDesk.PptProbe.Connect'
            Assembly = $probeAssembly; RuntimeVersion = 'v4.0.30319'
            CodeBase = ([Uri]$probeDll).AbsoluteUri
        }
        "$probeClassKey\ProgId" = @{ '' = $probeProgId }
        $probeProgKey = @{ '' = 'Aegis Desk PPT read probe' }
        "$probeProgKey\CLSID" = @{ '' = $probeClassId }
        $probeOfficeKey = @{
            FriendlyName = 'Aegis Desk PPT read probe'; Description = 'Experimental in-process reader'
            LoadBehavior = 3
        }
    }
    foreach ($probePath in $probeValues.Keys) {
        $probeRegistry = $probeRoot.CreateSubKey($probePath)
        try {
            foreach ($probeName in $probeValues[$probePath].Keys) {
                $probeValue = $probeValues[$probePath][$probeName]
                $probeKind = if ($probeValue -is [int]) {
                    [Microsoft.Win32.RegistryValueKind]::DWord
                } else { [Microsoft.Win32.RegistryValueKind]::String }
                $probeRegistry.SetValue($probeName, $probeValue, $probeKind)
            }
        } finally { $probeRegistry.Dispose() }
    }
    Write-Output 'Probe registered for current user; restart PowerPoint. Trust policy is not changed.'
} finally { $probeRoot.Dispose() }

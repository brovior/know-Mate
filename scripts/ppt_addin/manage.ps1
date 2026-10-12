<#
Build는 컴파일만 한다. Install/Remove는 사내 허용 후 사용자가 직접 실행한다.
선택한 범위의 실험용 고유 CLSID·ProgID·PowerPoint Addins 키만 변경한다.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('Build', 'Install', 'Remove')][string]$Action = 'Build',
    [ValidateSet('32', '64')][string]$OfficeBitness,
    [ValidateSet('User', 'Machine')][string]$Scope = 'User'
)
$ErrorActionPreference = 'Stop'
$probeClassId = '{02BE8BA9-C02B-48AD-ADBB-9B5686D72B1E}'
$probeProgId = 'AegisDesk.PptReadProbe'
$probeDll = Join-Path $PSScriptRoot 'bin\PptReadProbe.dll'

if ($Action -eq 'Build') {
    $probeCompiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
    if (!$PSCmdlet.ShouldProcess($probeDll, 'Compile probe DLL (no registration)')) { return }
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
$probeHive = if ($Scope -eq 'Machine') {
    [Microsoft.Win32.RegistryHive]::LocalMachine
} else { [Microsoft.Win32.RegistryHive]::CurrentUser }
$probeClassKey = "Software\Classes\CLSID\$probeClassId"
$probeProgKey = "Software\Classes\$probeProgId"
$probeOfficeKey = "Software\Microsoft\Office\PowerPoint\Addins\$probeProgId"
$probeTarget = "$probeHive ($OfficeBitness-bit): $probeClassKey; $probeProgKey; $probeOfficeKey"
if (!$PSCmdlet.ShouldProcess($probeTarget, "$Action probe registration")) { return }
if ($Scope -eq 'Machine') {
    $probeIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $probePrincipal = [Security.Principal.WindowsPrincipal]::new($probeIdentity)
        if (!$probePrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'Machine 범위 등록·해제는 관리자 권한이 필요합니다. 사용자 범위로 자동 전환하지 않습니다.'
        }
    } finally { $probeIdentity.Dispose() }
}
$probeRoot = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
    $probeHive, $probeView)
try {
    if ($Action -eq 'Remove') {
        foreach ($probeKey in @($probeOfficeKey, $probeProgKey, $probeClassKey)) {
            $probeRoot.DeleteSubKeyTree($probeKey, $false)
        }
        Write-Output "Probe registration removed: Scope=$Scope OfficeBitness=$OfficeBitness; other scopes/add-ins unchanged."
        return
    }
    if (!(Test-Path -LiteralPath $probeDll -PathType Leaf)) { throw 'Build를 먼저 실행하세요.' }
    $probeAssemblyName = [Reflection.AssemblyName]::GetAssemblyName($probeDll)
    $probeAssembly = $probeAssemblyName.FullName
    # RegAsm과 같은 버전별 CLR 등록 정보. 사용자·컴퓨터 범위에 동일하게 쓴다.
    $probeManagedValues = @{
        Class = 'AegisDesk.PptProbe.Connect'; Assembly = $probeAssembly
        RuntimeVersion = 'v4.0.30319'; CodeBase = ([Uri]$probeDll).AbsoluteUri
    }
    # Office 목록에는 CLR·ProgID 등록을 마친 뒤 마지막으로 노출한다.
    $probeValues = [ordered]@{
        $probeClassKey = @{ '' = 'Aegis Desk PPT read probe' }
        "$probeClassKey\InprocServer32" = @{
            '' = 'mscoree.dll'; ThreadingModel = 'Both'; Class = 'AegisDesk.PptProbe.Connect'
            Assembly = $probeAssembly; RuntimeVersion = 'v4.0.30319'
            CodeBase = ([Uri]$probeDll).AbsoluteUri
        }
        "$probeClassKey\InprocServer32\$($probeAssemblyName.Version)" = $probeManagedValues
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
    Write-Output "Probe registered: Scope=$Scope OfficeBitness=$OfficeBitness; restart PowerPoint. Trust policy is not changed."
} finally { $probeRoot.Dispose() }

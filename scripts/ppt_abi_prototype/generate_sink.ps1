<# Metadata-only prototype generation. Does not activate Office or register COM. #>
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$piaPath = Get-Item 'C:\Windows\assembly\GAC_MSIL\Microsoft.Office.Interop.PowerPoint\15.0.0.0__71e9bce111e9429c\Microsoft.Office.Interop.PowerPoint.dll' -ErrorAction Stop
$pia = [Reflection.Assembly]::ReflectionOnlyLoadFrom($piaPath.FullName)
if ($pia.FullName -ne 'Microsoft.Office.Interop.PowerPoint, Version=15.0.0.0, Culture=neutral, PublicKeyToken=71e9bce111e9429c') { throw 'prototype_pia_identity' }
foreach ($reference in $pia.GetReferencedAssemblies()) {
    [Reflection.Assembly]::ReflectionOnlyLoad($reference.FullName) | Out-Null
}
$events = $pia.GetType('Microsoft.Office.Interop.PowerPoint.EApplication', $true)
$methods = @($events.GetMethods() | Sort-Object MetadataToken)
if ($methods.Count -ne 33) { throw 'prototype_pia_member_count' }
$lines = [Collections.Generic.List[string]]::new()
$lines.Add('using System; using System.Runtime.InteropServices;')
$lines.Add('[ComVisible(true), ClassInterface(ClassInterfaceType.None)]')
$lines.Add('public sealed class PrototypeSink : Microsoft.Office.Interop.PowerPoint.EApplication {')
$lines.Add('public bool Active, Cancelled, Broken; public int ThrowIndex = -1; public Action<int,object[]> Observe;')
$lines.Add('private void Seen(int i, object[] args) { if (Observe != null) Observe(i,args); if (ThrowIndex == i) throw new COMException("synthetic", unchecked((int)0x80040201)); }')
for ($i = 0; $i -lt $methods.Count; $i++) {
    $method = $methods[$i]
    $parameters = @($method.GetParameters())
    $declarations = @()
    $arguments = @()
    $cancel = $null
    for ($j = 0; $j -lt $parameters.Count; $j++) {
        $p = $parameters[$j]
        if ($p.ParameterType.IsByRef) {
            if ($p.ParameterType.GetElementType().FullName -ne 'System.Boolean') { throw 'prototype_unexpected_byref' }
            $declarations += "ref bool p$j"
            $cancel = "p$j"
        } else { $declarations += ($p.ParameterType.FullName + " p$j") }
        $arguments += "p$j"
    }
    $lines.Add(('public void {0}({1}) {{' -f $method.Name, ($declarations -join ', ')))
    if ($method.Name -eq 'PresentationBeforeClose' -or $method.Name -eq 'ProtectedViewWindowBeforeClose') {
        if (!$cancel) { throw 'prototype_cancel_missing' }
        $lines.Add("bool incoming = $cancel;")
        $lines.Add("if (Active) { $cancel = true; Cancelled = true; }")
        $observed = @($arguments | ForEach-Object { if ($_ -eq $cancel) { 'incoming' } else { $_ } })
        $lines.Add(('try {{ Seen({0}, new object[] {{ {1} }}); }} catch {{ Broken = true; }}' -f $i, ($observed -join ', ')))
    } else { $lines.Add(('Seen({0}, new object[] {{ {1} }});' -f $i, ($arguments -join ', '))) }
    $lines.Add('}')
}
$lines.Add('}')
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
[IO.File]::WriteAllLines((Join-Path $OutputDirectory 'PrototypeSink.cs'), $lines)
$office = $pia.GetReferencedAssemblies() | Where-Object Name -eq 'office' | Select-Object -First 1
$officeAssembly = [Reflection.Assembly]::ReflectionOnlyLoad($office.FullName)
if ($officeAssembly.FullName -ne 'office, Version=15.0.0.0, Culture=neutral, PublicKeyToken=71e9bce111e9429c') { throw 'prototype_office_identity' }
function Read-Sha256([string]$path) {
    $stream=[IO.File]::OpenRead($path); $sha=[Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','') }
    finally { $sha.Dispose(); $stream.Dispose() }
}
@{ pia=$piaPath.FullName; office=$officeAssembly.Location; piaIdentity=$pia.FullName; officeIdentity=$officeAssembly.FullName; iid=$events.GUID.ToString(); members=$methods.Count; piaSha256=(Read-Sha256 $piaPath.FullName); officeSha256=(Read-Sha256 $officeAssembly.Location) } | ConvertTo-Json | Set-Content (Join-Path $OutputDirectory 'references.json') -Encoding UTF8

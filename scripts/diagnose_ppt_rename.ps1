<# Test-only diagnostics. Does not build the app, register COM, or change ProgramData. #>
[CmdletBinding()]
param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
if (!$PythonPath) { $PythonPath = Join-Path $repo '.venv\Scripts\python.exe' }
$log = Join-Path ([IO.Path]::GetTempPath()) ('AegisDesk-PptRename-' + [Guid]::NewGuid().ToString('N') + '.txt')
$exitCode = 1

function Write-Diagnostic([string]$Line) {
    $Line | Tee-Object -FilePath $log -Append
}

try {
    Write-Diagnostic 'PPT_SHORT_REPORT status=collecting required=actual_writer_and_acl product_approval=false'
    Write-Diagnostic 'PPT_RENAME_REPORT schema=1 purpose=baseline_and_actual_writer'
    Write-Diagnostic ('PPT_RENAME_ENV os_version=' + [Environment]::OSVersion.Version.ToString() +
        ' os_bits=' + $(if ([Environment]::Is64BitOperatingSystem) { 64 } else { 32 }) +
        ' powershell_bits=' + ([IntPtr]::Size * 8) + ' powershell_version=' + $PSVersionTable.PSVersion.ToString())
    Write-Diagnostic ('PPT_RENAME_POLICY directory_share=READ request_writer=native metadata_writer=managed')
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
    Write-Diagnostic ('PPT_RENAME_PREFLIGHT compiler_present=' + (Test-Path -LiteralPath $compiler -PathType Leaf))
    if (!(Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        Write-Diagnostic 'PPT_RENAME_PREFLIGHT python_present=false'
        throw 'python_missing'
    }
    $PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
    Push-Location $repo
    try {
        $git = Get-Command git -ErrorAction SilentlyContinue
        if ($git) {
            $revision = & git rev-parse --verify HEAD 2>$null
            if ($LASTEXITCODE -eq 0 -and $revision -match '^[0-9a-f]{40}$') {
                Write-Diagnostic ('PPT_RENAME_SOURCE commit=' + $revision)
                $changes = & git status --porcelain 2>$null
                if ($LASTEXITCODE -eq 0) {
                    Write-Diagnostic ('PPT_RENAME_SOURCE working_tree_dirty=' + [bool]$changes)
                }
            }
        }
        # PS 5.1 turns native stderr into ErrorRecord objects; keep collecting it.
        $ErrorActionPreference = 'Continue'
        $LASTEXITCODE = 1
        & $PythonPath -c "import sys,struct,pytest; print('PPT_RENAME_PYTHON version='+sys.version.split()[0]+' bits='+str(struct.calcsize('P')*8)+' pytest='+pytest.__version__)" 2>&1 |
            Tee-Object -FilePath $log -Append
        $preflightCode = $LASTEXITCODE
        if ($preflightCode -eq 0) {
            $LASTEXITCODE = 1
            & $PythonPath -m pytest knowmate/tests/test_ppt_rename_probe.py knowmate/tests/test_ppt_request_writer.py knowmate/tests/test_ppt_acl_native.py knowmate/tests/test_ppt_installation.py::test_windows_helper_stdout_survives_observer_close_and_deletes_on_last_close -s -v -o addopts= 2>&1 |
                Tee-Object -FilePath $log -Append
            $exitCode = $LASTEXITCODE
        }
        else { Write-Diagnostic 'PPT_RENAME_PREFLIGHT python_dependencies_failed=true' }
    }
    finally { $ErrorActionPreference = 'Stop'; Pop-Location }
}
catch {
    $exitCode = 1
    # Exception text can contain user paths; diagnostics use a fixed code.
    Write-Diagnostic 'PPT_RENAME_REPORT collection_error=true'
}
finally {
    Write-Diagnostic 'PPT_REQUIRED_EXTERNAL distinct_accounts=UNEXECUTED uac=UNEXECUTED office=UNEXECUTED x86_x64=inspect_bits'
    $lines = @(Get-Content -LiteralPath $log)
    $summary = @($lines | Where-Object { $_ -match '^PPT_(WRITER_SUMMARY|RENAME_SUMMARY|RENAME_SECURITY_SUMMARY|ACL_SUMMARY|STDOUT_SUMMARY) ' })
    if (!($summary | Where-Object { $_ -match '^PPT_WRITER_SUMMARY ' })) {
        $summary += 'PPT_WRITER_SUMMARY category=actual_writer result=INCOMPLETE product_approval=false'
    }
    if (!($summary | Where-Object { $_ -match '^PPT_ACL_SUMMARY result=PASS ' })) {
        $summary += 'PPT_ACL_SUMMARY result=INCOMPLETE actual_request_writer=true'
    }
    if (!($summary | Where-Object { $_ -match '^PPT_STDOUT_SUMMARY result=PASS$' })) {
        $summary += 'PPT_STDOUT_SUMMARY result=INCOMPLETE'
    }
    $failed = @($lines | Where-Object { $_ -match '^PPT_WRITER_CASE .* result=(FAIL|TIMEOUT|SKIP|UNEXECUTED) ' })
    $short = @('PPT_SHORT_REPORT status=complete product_approval=false') + $summary + $failed +
        @('PPT_REQUIRED_EXTERNAL distinct_accounts=UNEXECUTED uac=UNEXECUTED office=UNEXECUTED x86_x64=inspect_bits')
    # Put the same short block first and last. Corporate users can transcribe
    # only this block and retain the detailed one-run evidence underneath it.
    Set-Content -LiteralPath $log -Value ($short + @('PPT_SHORT_END') + $lines) -Encoding Unicode
    foreach ($line in $short) { Write-Diagnostic $line }
    Write-Diagnostic ('PPT_RENAME_REPORT pytest_exit_code=' + $exitCode + ' product_approval=false')
    Write-Output ('Diagnostic log: ' + $log)
}
exit $exitCode

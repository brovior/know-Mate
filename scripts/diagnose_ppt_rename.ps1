<# Test-only diagnostics. Does not build the app, register COM, or change ProgramData. #>
[CmdletBinding()]
param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
if (!$PythonPath) { $PythonPath = Join-Path $repo '.venv\Scripts\python.exe' }
$runId = [Guid]::NewGuid().ToString('N')
$log = Join-Path ([IO.Path]::GetTempPath()) ('AegisDesk-PptRename-' + $runId + '.txt')
$xml = Join-Path ([IO.Path]::GetTempPath()) ('AegisDesk-PptRename-' + $runId + '.xml')
# CreateNew prevents accidental reuse even in the unlikely event of a GUID collision.
$created = [IO.File]::Open($log, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
$created.Dispose()
if (Test-Path -LiteralPath $xml) { throw 'run_file_collision' }
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
            & $PythonPath -m pytest knowmate/tests/test_ppt_rename_probe.py knowmate/tests/test_ppt_request_writer.py knowmate/tests/test_ppt_acl_native.py knowmate/tests/test_ppt_installation.py::test_windows_helper_stdout_survives_observer_close_and_deletes_on_last_close -s -v --color=no -o addopts= "--junitxml=$xml" 2>&1 |
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
    $reportCode = 1
    if (Test-Path -LiteralPath $PythonPath -PathType Leaf) {
        $ErrorActionPreference = 'Continue'
        $LASTEXITCODE = 1
        & $PythonPath (Join-Path $PSScriptRoot 'ppt_diagnostic_report.py') --log $log --xml $xml --run-id $runId "--pytest-exit=$exitCode"
        $reportCode = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
    }
    if ($reportCode -ne 0) {
        # Never overwrite original evidence when collection/replacement fails.
        Write-Diagnostic 'PPT_SHORT_REPORT status=INCOMPLETE required_gate=INCOMPLETE product_approval=false'
        Write-Diagnostic 'PPT_COLLECTION_ERROR code=report_collection_failed'
        Write-Diagnostic 'PPT_SHORT_END'
        if ($exitCode -eq 0) { $exitCode = 1 }
    }
    # The helper removes this run's XML only after the txt summary is committed.
    # On collection/write failure retain both raw log and XML for investigation.
    Write-Output ('Diagnostic log: ' + $log)
}
exit $exitCode

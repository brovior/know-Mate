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
    Write-Diagnostic 'PPT_RENAME_REPORT schema=1 purpose=comparison_only'
    Write-Diagnostic ('PPT_RENAME_ENV os_version=' + [Environment]::OSVersion.Version.ToString() +
        ' os_bits=' + $(if ([Environment]::Is64BitOperatingSystem) { 64 } else { 32 }) +
        ' powershell_bits=' + ([IntPtr]::Size * 8) + ' powershell_version=' + $PSVersionTable.PSVersion.ToString())
    Write-Diagnostic ('PPT_RENAME_POLICY directory_share=READ product_changed=false')
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
            & $PythonPath -m pytest knowmate/tests/test_ppt_rename_probe.py -s -v -o addopts= 2>&1 |
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
    Write-Diagnostic ('PPT_RENAME_REPORT pytest_exit_code=' + $exitCode + ' product_approval=false')
    Write-Output ('Diagnostic log: ' + $log)
}
exit $exitCode

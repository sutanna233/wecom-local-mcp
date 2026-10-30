[CmdletBinding()]
param(
    [string]$ConfigPath = $(if ($env:WECOM_CONFIG) { $env:WECOM_CONFIG } else { "" }),
    [string]$OfflineDb = "",
    [string]$PythonPath = "",
    [switch]$StartWatcher,
    [switch]$NoWatcher
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Vendor = Join-Path $Root "third_party\wecom-decrypt"
$DataDir = Join-Path $Root "data"
$DbPath = Join-Path $DataDir "wecom_messages.sqlite"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# Keep this script ASCII-only. Windows PowerShell 5.1 otherwise decodes a
# no-BOM script with the active ANSI code page and corrupts Chinese literals.
function ConvertFrom-CodePoints([int[]]$CodePoints) {
    return (-join ($CodePoints | ForEach-Object { [char]$_ }))
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Write-LinesUtf8NoBom([string]$Path, [string[]]$Lines) {
    Write-Utf8NoBom $Path (($Lines -join "`r`n") + "`r`n")
}

function Invoke-Python([string[]]$Arguments, [string]$WorkDir = $Root) {
    $Output = @()
    $ExitCode = 0
    Push-Location -LiteralPath $WorkDir
    try {
        # Capture diagnostics so the caller can distinguish key failures from
        # unrelated collector errors. Continue prevents PS5.1 stderr handling
        # from hiding the native process exit code and diagnostic text.
        $ErrorActionPreference = "Continue"
        $Output = @(& $script:Python @Arguments 2>&1)
        $ExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    foreach ($Line in $Output) { Write-Host $Line }
    if ($ExitCode -ne 0) {
        $Detail = ($Output | Out-String).Trim()
        $Message = "Python command failed ($ExitCode) in ${WorkDir}: $($Arguments -join ' ')"
        if ($Detail) { $Message += [Environment]::NewLine + $Detail }
        throw $Message
    }
}

function Test-KeyRelatedFailure([object]$Failure) {
    if ($Failure -is [System.Management.Automation.ErrorRecord]) {
        $Text = $Failure.Exception.Message
    } else {
        $Text = [string]$Failure
    }
    $Patterns = @(
        '(?i)\bno (verified )?key\b',
        '(?i)\bkey (validation|verification|extraction|decryption) (failed|failure)\b',
        '(?i)\b(invalid|bad|missing|wrong|stale) (encryption |database |raw )?key\b',
        '(?i)\bkey\b[^\r\n]*\b(invalid|failed|failure|missing|mismatch|expired|changed)\b',
        '(?i)\b(decrypt|decryption|encrypted database)\b[^\r\n]*\bkey\b',
        '\u5bc6\u94a5'
    )
    foreach ($Pattern in $Patterns) {
        if ($Text -match $Pattern) { return $true }
    }
    return $false
}

function Get-RunningCollectorPid([string]$StatePath) {
    if (-not (Test-Path $StatePath -PathType Leaf)) { return $null }
    try {
        $State = Get-Content -Raw -Encoding UTF8 $StatePath | ConvertFrom-Json
        $ProcessId = [int]$State.pid
        if ($ProcessId -le 0) { return $null }
        $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if ($Process -and -not $Process.HasExited) { return $ProcessId }
    } catch {
        # A partial or stale state file is recovered by the collector lock.
    }
    return $null
}

# Chinese launcher names are constructed at runtime so PS5.1 never has to
# decode non-ASCII source text.
$CollectorLauncherName = "02_" + (ConvertFrom-CodePoints @(0x542F, 0x52A8, 0x6D88, 0x606F, 0x91C7, 0x96C6)) + ".cmd"
$McpLauncherName = "03_" + (ConvertFrom-CodePoints @(0x542F, 0x52A8, 0x53EA, 0x8BFB)) + "MCP.cmd"
$CollectorLauncher = Join-Path $Root $CollectorLauncherName
$McpLauncher = Join-Path $Root $McpLauncherName

try {
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if ($PythonPath) {
        $Python = (Resolve-Path $PythonPath -ErrorAction Stop).Path
    } elseif (Test-Path $VenvPython -PathType Leaf) {
        $Python = $VenvPython
    } else {
        $Python = (Get-Command python -ErrorAction Stop).Source
    }
    $script:Python = $Python
    Write-Host "[1/5] Python: $Python"
    & $Python -c "import Crypto" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "pycryptodome is missing from $Python. Run the numbered deployment CMD to install the packaged offline wheel."
    }

    if ($OfflineDb) {
        $OfflineSource = (Resolve-Path $OfflineDb -ErrorAction Stop).Path
        $DbPath = Join-Path $DataDir "offline_messages.sqlite"
        Write-Host "[2/5] Preparing checked offline copy from: $OfflineSource"
        Invoke-Python @("prepare_offline_db.py", $OfflineSource, $DbPath) $Root
    } else {
        if (-not $ConfigPath) {
            $ConfigPath = Join-Path $Vendor "config.json"
            Write-Host "[2/5] Detecting the local WXWork Data directory"
            Invoke-Python @("configure_wecom.py", "--config", $ConfigPath) $Root
        }
        $ConfigPath = (Resolve-Path $ConfigPath -ErrorAction Stop).Path
        $env:WECOM_CONFIG = $ConfigPath
        $Cfg = Get-Content -Raw -Encoding UTF8 $ConfigPath | ConvertFrom-Json
        if (-not (Test-Path $Cfg.wxwork_db_dir -PathType Container)) {
            throw "wxwork_db_dir does not exist: $($Cfg.wxwork_db_dir). Edit the deployment config first."
        }
        $KeyPath = [string]$Cfg.wxwork_keys_file
        if (-not [IO.Path]::IsPathRooted($KeyPath)) {
            $KeyPath = Join-Path (Split-Path -Parent $ConfigPath) $KeyPath
        }
        Write-Host "[2/5] WeCom data: $($Cfg.wxwork_db_dir)"
        if (-not (Test-Path $KeyPath -PathType Leaf)) {
            Write-Host "[3/5] No local key file; extracting and validating keys..."
            Invoke-Python @("find_wxwork_keys.py") $Vendor
        } else {
            Write-Host "[3/5] Existing key file found; validating during sync"
        }
        try {
            Invoke-Python @("wecom_collector.py", "--once") $Root
        } catch {
            if (-not (Test-KeyRelatedFailure $_)) { throw }
            Write-Host "Initial sync reported a key-related failure; refreshing the local key once..."
            Invoke-Python @("find_wxwork_keys.py") $Vendor
            Invoke-Python @("wecom_collector.py", "--once") $Root
        }
    }

    Write-Host "[4/5] Generating local MCP launchers"
    $McpEnv = [ordered]@{
        WECOM_MCP_DB = $DbPath
        WECOM_MCP_READ_ONLY = "1"
        PYTHONUTF8 = "1"
        PYTHONIOENCODING = "utf-8"
    }
    if (-not $OfflineDb -and $ConfigPath) { $McpEnv.WECOM_CONFIG = $ConfigPath }
    $McpConfig = [ordered]@{
        mcpServers = [ordered]@{
            "wecom-local-messages" = [ordered]@{
                command = $Python
                args = @((Join-Path $Root "wecom_mcp.py"))
                env = $McpEnv
            }
        }
    }
    $McpJson = $McpConfig | ConvertTo-Json -Depth 6
    Write-Utf8NoBom (Join-Path $Root "mcp_config.generated.json") ($McpJson + "`r`n")

    # Use %~dp0 instead of embedding Root so launchers remain valid after the
    # package is moved. chcp precedes any potentially non-ASCII path value.
    $CmdLines = @(
        "@echo off",
        "chcp 65001 >nul",
        "cd /d `"%~dp0`"",
        "set `"PYTHONUTF8=1`"",
        "set `"PYTHONIOENCODING=utf-8`"",
        "set `"WECOM_MCP_DB=$DbPath`"",
        "set `"WECOM_MCP_READ_ONLY=1`""
    )
    if (-not $OfflineDb -and $ConfigPath) { $CmdLines += "set `"WECOM_CONFIG=$ConfigPath`"" }
    $CmdLines += "`"$Python`" `"%~dp0wecom_mcp.py`" %*"
    Write-LinesUtf8NoBom $McpLauncher $CmdLines

    $WatcherLines = @(
        "@echo off",
        "chcp 65001 >nul",
        "cd /d `"%~dp0`"",
        "set `"PYTHONUTF8=1`"",
        "set `"PYTHONIOENCODING=utf-8`""
    )
    if ($ConfigPath) { $WatcherLines += "set `"WECOM_CONFIG=$ConfigPath`"" }
    $WatcherLines += "`"$Python`" `"%~dp0wecom_collector.py`" --watch --poll 15"
    Write-LinesUtf8NoBom $CollectorLauncher $WatcherLines

    # StartWatcher remains accepted for compatibility; online deployment now
    # starts the watcher by default. NoWatcher always wins, and OfflineDb is static.
    $WatcherRequested = -not $OfflineDb -and -not $NoWatcher
    $CollectorPidPath = Join-Path $Root "logs\collector.pid"
    if ($WatcherRequested) {
        $CollectorOutLog = Join-Path $DataDir "collector.stdout.log"
        $CollectorErrorLog = Join-Path $DataDir "collector.stderr.log"
        $ExistingCollectorPid = Get-RunningCollectorPid $CollectorPidPath
        if ($null -ne $ExistingCollectorPid) {
            Write-Host "Collector watcher already running; PID=$ExistingCollectorPid"
        } else {
            $WatcherProcess = Start-Process -FilePath $Python `
                -ArgumentList @("wecom_collector.py", "--watch", "--poll", "15") `
                -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput $CollectorOutLog `
                -RedirectStandardError $CollectorErrorLog
            Write-Host "Collector watcher started in the current user session; PID=$($WatcherProcess.Id)"
        }
        Write-Host "Collector PID file: $CollectorPidPath"
        Write-Host "Collector stdout log: $CollectorOutLog"
        Write-Host "Collector stderr log: $CollectorErrorLog"
    } elseif ($OfflineDb) {
        Write-Host "Collector watcher not started for OfflineDb."
    } else {
        Write-Host "Collector watcher disabled by -NoWatcher."
    }

    Write-Host "[5/5] DONE"
    Write-Host "MCP config: $(Join-Path $Root 'mcp_config.generated.json')"
    Write-Host "MCP launcher: $McpLauncher"
    Write-Host "Collector launcher: $CollectorLauncher"
    Write-Host "Message DB: $DbPath"
    Write-Host "Import the generated MCP config into your Agent manually; no Agent settings were changed."
} catch {
    Write-Error $_
    exit 1
}

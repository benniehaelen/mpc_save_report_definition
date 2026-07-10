<#
.SYNOPSIS
    List (or stop) running hin-poc MCP server processes.

.DESCRIPTION
    Servers no longer contend for a database lock: the warehouse is opened
    read-only (a shared lock) and writes go to the SQLite metadata store, which
    allows concurrent writers. Several servers can now run side by side without
    breaking each other's tool calls.

    Orphaned servers left over from previous sessions are still worth clearing
    out -- they hold connections and file handles, and it is rarely what you
    meant to have running. Use this to see what is up and, with -Kill, stop them.

.EXAMPLE
    powershell -File scripts/servers.ps1
        List the running hin-poc servers.

.EXAMPLE
    powershell -File scripts/servers.ps1 -Kill
        Stop every running hin-poc server.
#>
[CmdletBinding()]
param(
    [switch]$Kill
)

$servers = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'mpc_save_report_definition' -and $_.CommandLine -match 'main\.py' })

if (-not $servers) {
    Write-Host "No hin-poc servers running." -ForegroundColor Green
    exit 0
}

# One launch of "python server/main.py" shows up as two processes: the venv
# python.exe shim and the real interpreter it spawns as its child. Count
# *logical* servers -- a matched process whose parent is not itself a matched
# server -- so a single normal launch reads as 1, not 2.
$pids = $servers | ForEach-Object { $_.ProcessId }
$logical = @($servers | Where-Object { $pids -notcontains $_.ParentProcessId })
$n = $logical.Count

$servers | Select-Object ProcessId, ParentProcessId, CommandLine | Format-List

if ($Kill) {
    foreach ($s in $servers) {
        Write-Host ("Stopping PID {0}" -f $s.ProcessId) -ForegroundColor Yellow
        Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Done. Reconnect the hin-poc server in your client to start a clean instance." -ForegroundColor Green
} elseif ($n -gt 1) {
    Write-Host ("{0} servers running. They no longer lock each other out, but this is probably orphans -- use -Kill to clear them." -f $n) -ForegroundColor Yellow
} else {
    Write-Host "1 server running (expected)." -ForegroundColor Cyan
}

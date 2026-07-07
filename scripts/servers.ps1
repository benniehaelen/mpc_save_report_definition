<#
.SYNOPSIS
    List (or stop) running hin-poc MCP server processes.

.DESCRIPTION
    Only one hin-poc server can run at a time: it holds an exclusive read-write
    lock on data/poc.duckdb. Orphaned servers left over from previous sessions
    keep that lock and make every tool call (nl_query, execute_sql, ...) fail,
    which looks like the tools hanging. Use this to see what is running and, with
    -Kill, stop them so a single clean server can take the lock.

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
    Write-Host "No hin-poc servers running. The database lock is free." -ForegroundColor Green
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
    Write-Host "Done. Reconnect the hin-poc server in your client to start one clean instance." -ForegroundColor Green
} elseif ($n -gt 1) {
    Write-Host ("{0} servers running -- that's the problem. Re-run with -Kill to stop them all." -f $n) -ForegroundColor Red
} else {
    Write-Host "1 server running (expected). Re-run with -Kill only if the tools are stuck." -ForegroundColor Cyan
}

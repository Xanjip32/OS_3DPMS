# Stops OS_3DPMS. Only kills a process whose command line is this program,
# so a stale PID file can never stop an unrelated process.
$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root 'dashboard.pid'

if (Test-Path $pidFile) {
    $id = [int](Get-Content $pidFile -TotalCount 1)
    Get-CimInstance Win32_Process -Filter "ProcessId=$id" |
        Where-Object { $_.CommandLine -like '*run.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force; 'Stopped OS_3DPMS.' }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

# Older "printer-dashboard" prototype
Get-CimInstance Win32_Process -Filter "Name like 'py%.exe'" |
    Where-Object { $_.CommandLine -like '*printer-dashboard*server.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; 'Stopped old printer-dashboard.' }

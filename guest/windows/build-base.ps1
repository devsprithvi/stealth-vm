# Runs once, in Audit mode as the built-in Administrator, while the Windows 10
# base is being built (see autounattend.xml). It leaves the machine with no
# user activity, stages the files every future session needs, and schedules
# `sysprep /generalize /oobe /shutdown`. Nothing here registers an account or a
# device with any online service.
$ErrorActionPreference = 'Stop'
$log = 'C:\build-base.log'
function Write-Log([string]$Message) {
    ('{0} {1}' -f (Get-Date -Format s), $Message) | Add-Content -Path $log
}

try {
    Write-Log "build-base start; media=$PSScriptRoot"

    # 1. Files used by every session on its first logon.
    $scripts = 'C:\Windows\Setup\Scripts'
    New-Item -ItemType Directory -Path $scripts -Force | Out-Null
    Copy-Item (Join-Path $PSScriptRoot 'first-logon.ps1') $scripts -Force
    Copy-Item (Join-Path $PSScriptRoot 'warp.msi') $scripts -Force
    Copy-Item (Join-Path $PSScriptRoot 'session-unattend.xml') `
        'C:\Windows\System32\Sysprep\session-unattend.xml' -Force

    # 2. Power: no hibernation file (a hibernated base cannot be shared), no
    #    fast startup, no display or sleep timeouts.
    powercfg /hibernate off | Out-Null
    powercfg /change monitor-timeout-ac 0 | Out-Null
    powercfg /change standby-timeout-ac 0 | Out-Null
    New-Item -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Force | Out-Null
    Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
        -Name HiberbootEnabled -Value 0 -Type DWord

    # 3. Stop the Store from updating inbox apps for one user while the image
    #    is prepared; a per-user app update is the usual cause of a failed
    #    generalize (0x80073cf2). first-logon.ps1 removes the policy again.
    $store = 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore'
    New-Item -Path $store -Force | Out-Null
    Set-ItemProperty $store -Name AutoDownload -Value 2 -Type DWord

    # 4. Remove traces of the build: logs, temp files, event logs.
    Remove-Item "$env:TEMP\*" -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item 'C:\Windows\Temp\*' -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item 'C:\Windows\INF\setupapi*.log' -Force -ErrorAction SilentlyContinue
    # Some analytic/debug channels cannot be cleared; that is expected. Run
    # through cmd so native stderr never becomes a terminating error here.
    cmd.exe /c 'for /f "tokens=*" %L in (''wevtutil el'') do @wevtutil cl "%L" >nul 2>&1'
    $global:LASTEXITCODE = 0
    ipconfig /flushdns | Out-Null

    # 5. sysprep runs from a detached process a short time after the auditUser
    #    pass ends. It first closes the Sysprep dialog that Audit mode opens by
    #    itself (two sysprep instances refuse to run together). The process is
    #    started through WMI so it is not a child of this pass and is not
    #    killed when the pass ends; no scheduled task is left in the image.
    $waiter = Join-Path $scripts 'run-sysprep.ps1'
    @'
$log = 'C:\build-base.log'
function Write-Log([string]$Message) {
    ('{0} {1}' -f (Get-Date -Format s), $Message) | Add-Content -Path $log
}
Start-Sleep -Seconds 45
Get-Process sysprep -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 5
Write-Log 'starting sysprep'
$p = Start-Process -FilePath 'C:\Windows\System32\Sysprep\sysprep.exe' -Wait -PassThru -ArgumentList @(
    '/generalize', '/oobe', '/shutdown', '/quiet',
    '/unattend:C:\Windows\System32\Sysprep\session-unattend.xml')
Write-Log ('sysprep exit code {0}' -f $p.ExitCode)
$err = 'C:\Windows\System32\Sysprep\Panther\setuperr.log'
if (Test-Path $err) { Get-Content $err -Tail 40 | Add-Content -Path $log }
'@ | Set-Content -Path $waiter -Encoding ASCII
    $started = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File $waiter" }
    if ($started.ReturnValue -ne 0) { throw "could not start the sysprep waiter: $($started.ReturnValue)" }
    Write-Log 'sysprep scheduled'
} catch {
    Write-Log ('FAILED: {0}' -f $_)
    throw
}

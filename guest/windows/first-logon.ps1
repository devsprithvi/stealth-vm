# Runs on the first logon of every Windows session (FirstLogonCommands in
# session-unattend.xml). The base contains the WARP installer but no installed
# client and no registration, so everything that identifies this machine to
# Cloudflare is created here, fresh, for this session only.
$ErrorActionPreference = 'Continue'
$log = 'C:\Windows\Temp\first-logon.log'
function Write-Log([string]$Message) {
    ('{0} {1}' -f (Get-Date -Format s), $Message) | Add-Content -Path $log
}
$failed = $false

# Identifiers that survive sysprep and would be the same in every session:
# the SQM/telemetry machine id, the hardware-profile GUID and the network
# adapter's interface GUID. Give this session its own. The adapter is removed
# and re-detected, so Windows installs it again under a new GUID.
try {
    $sqm = 'HKLM:\SOFTWARE\Microsoft\SQMClient'
    New-Item -Path $sqm -Force | Out-Null
    Set-ItemProperty -Path $sqm -Name MachineId -Value ('{' + [guid]::NewGuid().ToString().ToUpper() + '}')
    Write-Log 'SQM machine id replaced'
} catch { Write-Log "SQM: $_"; $failed = $true }

try {
    Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\IDConfigDB\Hardware Profiles\0001' `
        -Name HwProfileGuid -Value ('{' + [guid]::NewGuid().ToString() + '}')
    Write-Log 'hardware profile GUID replaced'
} catch { Write-Log "HwProfileGuid: $_"; $failed = $true }

try {
    $before = @(Get-NetAdapter -Physical | ForEach-Object { $_.InterfaceGuid })
    Get-PnpDevice -Class Net -PresentOnly | Where-Object { $_.InstanceId -like 'PCI\*' } | ForEach-Object {
        Write-Log "removing $($_.InstanceId)"
        pnputil.exe /remove-device $_.InstanceId | ForEach-Object { Write-Log "pnputil: $_" }
    }
    pnputil.exe /scan-devices | Out-Null
    $new = $null
    for ($i = 0; $i -lt 90; $i++) {
        $a = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' } | Select-Object -First 1
        if ($a -and (Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -notlike '169.254.*' })) { $new = $a.InterfaceGuid; break }
        Start-Sleep -Seconds 2
    }
    if (-not $new) { throw 'network did not come back' }
    if ($before -contains $new) { throw "interface GUID unchanged ($new)" }
    Write-Log "interface GUID $new"
    # Drop the registry leftovers of the build-time adapter.
    foreach ($old in $before) {
        foreach ($key in @(
                "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\$old",
                "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters\Interfaces\$old",
                "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Adapters\$old",
                "HKLM:\SYSTEM\CurrentControlSet\Control\Network\{4D36E972-E325-11CE-BFC1-08002BE10318}\$old")) {
            Remove-Item -LiteralPath $key -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    # Remove interface keys that belong to no adapter (build-time leftovers).
    $live = @(Get-NetAdapter -IncludeHidden | ForEach-Object { $_.InterfaceGuid.ToLower() })
    foreach ($family in 'Tcpip', 'Tcpip6') {
        $root = "HKLM:\SYSTEM\CurrentControlSet\Services\$family\Parameters\Interfaces"
        Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue | Where-Object {
            $live -notcontains $_.PSChildName.ToLower()
        } | ForEach-Object {
            Write-Log "removing orphan $family interface $($_.PSChildName)"
            Remove-Item -LiteralPath $_.PSPath -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
} catch { Write-Log "NIC: $_"; $failed = $true }

# IPv6 off on every adapter so a WARP outage cannot leak over IPv6. This runs
# after the adapter was re-created, because a new device has default bindings.
try {
    Get-NetAdapter | Disable-NetAdapterBinding -ComponentID ms_tcpip6 -ErrorAction Stop
    Write-Log 'IPv6 disabled'
} catch { Write-Log "IPv6: $_"; $failed = $true }

# Undo the image-preparation Store policy.
Remove-Item 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore' -Recurse -Force -ErrorAction SilentlyContinue

# WARP: managed policy first (locked switch, connect on start), then install.
try {
    $dir = 'C:\ProgramData\Cloudflare'
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    @'
<dict>
  <key>switch_locked</key>
  <true/>
  <key>auto_connect</key>
  <integer>0</integer>
  <key>service_mode</key>
  <string>warp</string>
</dict>
'@ | Set-Content -Path (Join-Path $dir 'mdm.xml') -Encoding ASCII

    $msi = Join-Path $PSScriptRoot 'warp.msi'
    $install = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @('/i', "`"$msi`"", '/qn', '/norestart')
    Write-Log ('msiexec exit code {0}' -f $install.ExitCode)

    $cli = Join-Path $env:ProgramFiles 'Cloudflare\Cloudflare WARP\warp-cli.exe'
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        if ((Test-Path $cli) -and ((& $cli --accept-tos status 2>&1) -notmatch 'Unable to connect')) { $ready = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw 'warp-svc did not answer' }
    & $cli --accept-tos registration new 2>&1 | ForEach-Object { Write-Log "register: $_" }
    & $cli --accept-tos mode warp 2>&1 | ForEach-Object { Write-Log "mode: $_" }
    & $cli --accept-tos connect 2>&1 | ForEach-Object { Write-Log "connect: $_" }
    Start-Sleep -Seconds 5
    & $cli --accept-tos status 2>&1 | ForEach-Object { Write-Log "status: $_" }
} catch { Write-Log "WARP: $_"; $failed = $true }

if (-not $failed) {
    # Leave no installer or log behind when everything worked.
    Remove-Item (Join-Path $PSScriptRoot 'warp.msi') -Force -ErrorAction SilentlyContinue
    Remove-Item $log -Force -ErrorAction SilentlyContinue
    Remove-Item $PSCommandPath -Force -ErrorAction SilentlyContinue
}

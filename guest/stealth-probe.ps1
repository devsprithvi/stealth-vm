# stealth-probe.ps1 - report what user-mode software can see about the VM.
# Read-only: changes nothing. Run inside the Windows guest (PowerShell 5.1).
# Usage: powershell -ep bypass -f D:\stealth-probe.ps1 [-NoPause] [-HostPort 9999]
#
# -HostPort N  isolation test: first start a listener on the HOST, e.g.
#              python3 -m http.server N --bind 127.0.0.1
#              The probe then tries to reach it through the slirp gateway.
#              Reaching it means the guest can talk to host loopback services.
param([switch]$NoPause, [int]$HostPort = 0)
$ErrorActionPreference = 'SilentlyContinue'
$script:flags = New-Object System.Collections.ArrayList
function Section($t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }
function Flag($m) { [void]$script:flags.Add($m); Write-Host "  [TELL] $m" -ForegroundColor Red }
function Pause-Page { if (-not $NoPause) { Read-Host "-- Enter --" | Out-Null; Clear-Host } }
$pat = 'qemu|virtio|bochs|bxpc|seabios|ovmf|edk ?ii|tianocore|red ?hat|spice|vbox|virtualbox|vmware|vmw|xen|kvm|parallels|innotek|1b36|1af4|1234'

Clear-Host
Section "1. Identity (WMI)"
$cs = Get-WmiObject Win32_ComputerSystem
$bios = Get-WmiObject Win32_BIOS
$bb = Get-WmiObject Win32_BaseBoard
$enc = Get-WmiObject Win32_SystemEnclosure
$cpu = Get-WmiObject Win32_Processor | Select-Object -First 1
"System   : $($cs.Manufacturer) | $($cs.Model) | name=$($cs.Name)"
"BIOS     : $($bios.Manufacturer) | $($bios.SMBIOSBIOSVersion) | $($bios.ReleaseDate) | sn=$($bios.SerialNumber) | SMBIOS $($bios.SMBIOSMajorVersion).$($bios.SMBIOSMinorVersion)"
"Board    : $($bb.Manufacturer) | $($bb.Product) | $($bb.SerialNumber)"
"Chassis  : types=$($enc.ChassisTypes) | $($enc.Manufacturer)"
"CPU      : $($cpu.Name) | $($cpu.NumberOfCores)C/$($cpu.NumberOfLogicalProcessors)T | $($cpu.MaxClockSpeed)MHz | L2=$($cpu.L2CacheSize)K L3=$($cpu.L3CacheSize)K | socket=$($cpu.SocketDesignation)"
"HypervisorPresent : $($cs.HypervisorPresent)"
if ($cs.HypervisorPresent) { Flag "Win32_ComputerSystem.HypervisorPresent = True" }
foreach ($s in @($cs.Manufacturer,$cs.Model,$bios.Manufacturer,$bios.SMBIOSBIOSVersion,$bb.Manufacturer,$bb.Product,$cpu.Name)) { if ("$s" -match $pat) { Flag "string matches VM pattern: $s" } }
$mem = Get-WmiObject Win32_PhysicalMemory
foreach ($m in $mem) { "DIMM     : $($m.Manufacturer) | $($m.PartNumber) | $([math]::Round($m.Capacity/1GB))GB | $($m.Speed)MT/s | $($m.DeviceLocator)" }
$arr = Get-WmiObject Win32_PhysicalMemoryArray
"MemArray : slots=$($arr.MemoryDevices) maxKB=$($arr.MaxCapacity)"
$cache = Get-WmiObject Win32_CacheMemory
"Caches   : $(($cache | ForEach-Object { "$($_.DeviceID)=$($_.InstalledSize)K" }) -join ' ')"
Pause-Page

Section "1b. SMBIOS structures as Windows received them"
$rt = Get-WmiObject -Namespace root\wmi -Class MSSmBios_RawSMBiosTables
$raw = $rt.SMBiosData
"SMBIOS $($rt.SmbiosMajorVersion).$($rt.SmbiosMinorVersion), $($raw.Length) bytes; entries: type(handle,len)"
$i = 0; $list = @(); $strs = @()
while ($i + 4 -lt $raw.Length -and $raw[$i] -ne 127) {
  $t = $raw[$i]; $l = $raw[$i+1]; $h = [BitConverter]::ToUInt16($raw, $i+2)
  $j = $i + $l
  while ($j + 1 -lt $raw.Length -and -not ($raw[$j] -eq 0 -and $raw[$j+1] -eq 0)) { $j++ }
  $list += ("{0}(h{1:X},{2})" -f $t, $h, $l)
  if (($t -eq 0 -or $t -eq 1 -or $t -eq 17) -and $j -gt $i + $l) {
    $s = [Text.Encoding]::ASCII.GetString($raw[($i+$l)..($j-1)]) -replace "`0", ' | '
    $strs += "type ${t}: $s"
  }
  $i = $j + 2
}
$list -join ' '
$strs
$types = @($list | ForEach-Object { [int]($_ -replace '\(.*','') })
"Types present: $(($types | Sort-Object -Unique) -join ',')"
if ($types -notcontains 1) { Flag "SMBIOS type 1 (System) missing - table likely malformed" }
if ($types -notcontains 17) { Flag "SMBIOS type 17 (memory device) missing - table likely malformed" }
Pause-Page

Section "2. Storage and network"
Get-WmiObject Win32_DiskDrive | ForEach-Object { "Disk     : $($_.Model) | sn=$($_.SerialNumber.Trim()) | fw=$($_.FirmwareRevision) | $([math]::Round($_.Size/1GB,1))GB | $($_.InterfaceType) | $($_.PNPDeviceID)" }
Get-PhysicalDisk | ForEach-Object { "PhysDisk : $($_.FriendlyName) | media=$($_.MediaType) | bus=$($_.BusType)" }
Get-WmiObject Win32_CDROMDrive | ForEach-Object { "CD       : $($_.Caption) | $($_.PNPDeviceID)" }
Get-WmiObject Win32_NetworkAdapter | Where-Object { $_.PhysicalAdapter } | ForEach-Object {
  "NIC      : $($_.Name) | $($_.MACAddress) | $($_.PNPDeviceID)"
}
Get-WmiObject Win32_VideoController | ForEach-Object { "GPU      : $($_.Name) | $($_.PNPDeviceID) | $($_.CurrentHorizontalResolution)x$($_.CurrentVerticalResolution) | VRAM=$([math]::Round($_.AdapterRAM/1MB))MB | drv=$($_.DriverVersion)" }
Get-WmiObject Win32_DesktopMonitor | ForEach-Object { "Monitor  : $($_.Name) | $($_.PNPDeviceID)" }
Get-WmiObject -Namespace root\wmi WmiMonitorID | ForEach-Object { "EDID     : $(($_.ManufacturerName | Where-Object {$_}) | ForEach-Object {[char]$_}) $(($_.UserFriendlyName | Where-Object {$_}) | ForEach-Object {[char]$_})" }
$mac = (Get-WmiObject Win32_NetworkAdapter | Where-Object { $_.PhysicalAdapter -and $_.MACAddress } | Select-Object -First 1).MACAddress
if ($mac -match '^(52:54:00|00:0C:29|00:50:56|08:00:27|00:1C:42|00:15:5D|0A:00:27)') { Flag "hypervisor MAC OUI: $mac" }
Pause-Page

Section "3. PCI / PnP devices (present)"
$pnp = Get-PnpDevice -PresentOnly
$pci = $pnp | Where-Object { $_.InstanceId -like 'PCI\*' }
$pci | ForEach-Object { $id = ($_.InstanceId -split '\\')[1]; "{0,-42} {1}" -f $id, $_.FriendlyName } | Sort-Object -Unique
$ven = $pci | ForEach-Object { if ($_.InstanceId -match 'VEN_([0-9A-F]{4})') { $Matches[1] } } | Sort-Object -Unique
"Vendors  : $($ven -join ' ')"
foreach ($v in @('1B36','1AF4','1234','15AD','80EE')) { if ($ven -contains $v) { Flag "PCI vendor $v present (QEMU/virtio/VMware/VBox)" } }
$bad = $pnp | Where-Object { "$($_.FriendlyName) $($_.InstanceId) $($_.Manufacturer)" -match $pat }
foreach ($b in $bad) { Flag "PnP: $($b.FriendlyName) [$($b.InstanceId)]" }
Pause-Page

Section "4. Non-PCI PnP, ACPI devices, services, processes"
($pnp | Where-Object { $_.InstanceId -like 'ACPI\*' } | ForEach-Object { ($_.InstanceId -split '\\')[1] } | Sort-Object -Unique) -join ' '
# Hyper-V guest drivers/services ship (stopped) in stock Windows 10; only a running one is a tell.
$hv = 'vmbus|vmic|hyperv|hv_|storflt|vpci|vmgid|gencounter|bttflt|hvservice|hvsocket'
$svc = Get-WmiObject Win32_SystemDriver | Where-Object { ("$($_.Name) $($_.DisplayName) $($_.PathName)" -match $pat) -or ($_.State -eq 'Running' -and $_.Name -match $hv) }
foreach ($s in $svc) { Flag "driver: $($s.Name) ($($s.DisplayName)) state=$($s.State)" }
$svc2 = Get-Service | Where-Object { ("$($_.Name) $($_.DisplayName)" -match $pat) -or ($_.Status -eq 'Running' -and $_.Name -match $hv) }
foreach ($s in $svc2) { Flag "service: $($s.Name)" }
$proc = Get-Process | Where-Object { $_.Name -match 'vdagent|vdservice|qemu|vbox|vmtools|vmware|spice|virtio' }
foreach ($p in $proc) { Flag "process: $($p.Name)" }
Pause-Page

Section "5. Firmware tables in registry (ACPI OEM IDs, BIOS)"
$acpi = 'HKLM:\HARDWARE\ACPI'
foreach ($t in 'DSDT','FADT','RSDT','XSDT') {
  $k = Get-ChildItem "$acpi\$t" -ErrorAction SilentlyContinue
  foreach ($oem in $k) { "{0,-5} OEM id: {1}" -f $t, $oem.PSChildName; if ($oem.PSChildName -match 'BOCHS|BXPC|QEMU|VBOX|VMWARE|XEN|PTLTD|_ASUS_|ALASKA') { if ($oem.PSChildName -match 'BOCHS|BXPC|QEMU|VBOX|VMWARE|XEN') { Flag "$t OEM id $($oem.PSChildName)" } } }
}
$sysbios = Get-ItemProperty 'HKLM:\HARDWARE\DESCRIPTION\System\BIOS'
"BIOS reg : $($sysbios.SystemManufacturer) | $($sysbios.SystemProductName) | $($sysbios.BIOSVendor) $($sysbios.BIOSVersion) | $($sysbios.BaseBoardProduct)"
$sys = Get-ItemProperty 'HKLM:\HARDWARE\DESCRIPTION\System'
"SysBios  : $($sys.SystemBiosVersion -join ' / ')"
"VideoBios: $($sys.VideoBiosVersion -join ' / ')"
$tz = Get-ItemProperty 'HKLM:\HARDWARE\DESCRIPTION\System\CentralProcessor\0'
"CPU reg  : $($tz.ProcessorNameString) | $($tz.Identifier) | MHz=$($tz.'~MHz') | ucode=$(($tz.'Update Revision' | ForEach-Object {$_.ToString('X2')}) -join '')"
$fw = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control' -Name FirmwareBootDevice).FirmwareBootDevice
"FirmwareType: $env:firmware_type"
Pause-Page

Section "6. OS-level virtualization hints"
$si = (systeminfo 2>$null) -join "`n"
if ($si -match 'A hypervisor has been detected') { Flag "systeminfo: A hypervisor has been detected" } else { "systeminfo: no hypervisor message" }
$vp = (Get-WmiObject Win32_Processor | Select-Object -First 1).VirtualizationFirmwareEnabled
"VirtualizationFirmwareEnabled = $vp  (real CPUs usually True/False, not null)"
"Hyper-V feature/Device Guard: $((Get-WmiObject -Namespace root\Microsoft\Windows\DeviceGuard -Class Win32_DeviceGuard).VirtualizationBasedSecurityStatus)"
$tp = Get-WmiObject -Namespace root\cimv2\Security\MicrosoftTpm -Class Win32_Tpm
"TPM      : $(if ($tp) { 'present' } else { 'absent' })"
$sb = $null; try { $sb = Confirm-SecureBootUEFI } catch {}
"SecureBoot: $sb"
$tt = Get-WmiObject -Namespace root\wmi MSAcpi_ThermalZoneTemperature
"ThermalZone (MSAcpi): $(if ($tt) { 'present' } else { 'absent' })"
$bat = Get-WmiObject Win32_Battery
"Battery  : $(if ($bat) { 'present' } else { 'none (desktop OK)' })"
$up = (Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
"Uptime   : $([int]$up.TotalMinutes) min"
"Cores/Threads from env: $env:NUMBER_OF_PROCESSORS  RAM: $([math]::Round($cs.TotalPhysicalMemory/1GB,2)) GB"
Pause-Page

if ($HostPort -gt 0) {
  Section "7. Host isolation (loopback via slirp gateway)"
  $c = New-Object Net.Sockets.TcpClient
  $ok = $false
  try { $ar = $c.BeginConnect('192.168.1.1', $HostPort, $null, $null); $ok = $ar.AsyncWaitHandle.WaitOne(4000) -and $c.Connected } catch {}
  $c.Close()
  if ($ok) { Flag "guest reached HOST loopback port $HostPort via 192.168.1.1" }
  else { Write-Host "  host loopback port $HostPort is NOT reachable from the guest (good)" -ForegroundColor Green }
  Pause-Page
}

Section "SUMMARY"
if ($script:flags.Count -eq 0) { Write-Host "No tells found by these checks." -ForegroundColor Green }
else { Write-Host "$($script:flags.Count) tell(s):" -ForegroundColor Red; $script:flags | ForEach-Object { "  - $_" } }

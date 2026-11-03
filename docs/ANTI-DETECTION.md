# Anti-detection design

This document explains why the project exists, what the upstream
`zhaodice/qemu-anti-detection` patch does, what this project adds on top of
it, how the result is measured, and what is still detectable.

## Why a sandbox must look like a real PC

Evasive malware asks "am I in a virtual machine or sandbox?" before it does
anything interesting. If the answer is yes it sleeps, exits, or runs a harmless
decoy path, and the analyst records a clean-looking sample. To observe the real
behaviour, the guest has to pass the checks the sample performs.

Those checks fall into a small number of families. Each row says where this
project stands. "Probe" means the check is covered by `guest/stealth-probe.*`
(see [Validation](#validation)).

| Family | What a sample looks at | How it is handled | State |
|---|---|---|---|
| Firmware strings | SMBIOS system/board/BIOS/chassis/memory, ACPI OEM IDs and device names | Upstream patch renames QEMU strings; local patches and generated SMBIOS tables fill in a coherent ASUS/AMI desktop | Pass (probe) |
| CPUID | hypervisor bit, KVM signature, vendor, brand string, family/model/stepping, cache sizes, topology | `-cpu host,hypervisor=off,kvm=off`, identity matched to the host CPU, `host-cache-info=on`, `topoext=on` | Pass (probe) |
| Device identity | PCI vendor/device IDs, device names, disk/CD model, NIC driver, USB and input devices | Upstream patch renames IDs and strings; emulated ICH9 chipset, Intel e1000e, AHCI SSD, PS/2 input | Pass except the GPU |
| Guest artifacts | VM tools, services, drivers, registry keys, processes | No guest additions, no agent, no clipboard or folder sharing | Pass (probe) |
| Hardware realism | core count, RAM, disk size, SSD vs HDD, screen size, optical drive, uptime | 12 vCPU / 8 GiB / 500 GB SSD, 1920x1080, real drive models | Pass; user-activity artifacts are not simulated |
| Network | MAC OUI, DHCP, public IP | Verified real-vendor OUI matching the NIC vendor; Cloudflare WARP in the guest | Pass; the WARP adapter itself is visible |
| GPU | PCI ID `1b36:0100`, "Microsoft Basic Display Adapter", `qxl` module, 0 MB VRAM | Not solved | **Detectable** |
| Firmware vendor | OVMF reports "EDK II" / "Ubuntu distribution of EDK II" | Not solved | **Detectable** (Windows registry) |
| Chipset | Intel ICH9/Q35 PCI IDs on an AMD-board identity | Not solved | Detectable by deep checks |
| Timing | RDTSC / CPUID exit latency, clock jitter | Not solved (optional host-kernel patch in `HARDENING.md`) | **Detectable** by timing-aware samples |

## What `zhaodice/qemu-anti-detection` does

The upstream project is a single source patch against a pinned QEMU release.
For QEMU 10.2.2 it touches about sixty files. It does not change how the
virtual machine is built, only what the virtual machine says about itself.
`scripts/build-qemu.sh` downloads commit
`2750c86d2d045243ba6617951487e41b25c05557` and refuses to continue unless the
file hash matches the one pinned in the script.

Its changes group into:

- **Name strings.** Every user-visible `QEMU`, `Red Hat`, `Bochs` and `SeaBIOS`
  string in device models becomes `ASUS`: ATAPI and SCSI vendor/product
  (`ASUS DVD-ROM`, `ASUS HARDDISK`), NVMe model, USB device names, PS/2 and HID
  device names (`ASUS PS/2 Mouse`), the HDA codec, the SPICE server name, and
  the default monitor name and EDID vendor (`DEL`).
- **SMBIOS defaults.** The default manufacturer/product become ASUS values, and
  the SMBIOS type 0 "virtual machine" characteristic bit is switched off.
- **CPU identity.** The default CPU model string no longer says "QEMU Virtual
  CPU", and the KVM CPUID signature (`KVMKVMKVM`) is replaced.
- **PCI identity.** The Red Hat / Qumranet vendor-ID macros are redefined to
  Intel's `0x8086`, which is what makes the e1000e, AHCI and USB devices read as
  Intel parts in the guest.
- **ACPI.** The firmware-config device ID `QEMU0002` becomes `ASUS0002`, and a
  placeholder BGRT table is appended.
- **Miscellaneous.** Strings in the vvfat, VHDX and wacom-tablet code, the
  vhost-user GPU and a few others.

We use it instead of writing our own because the work is wide and mechanical:
finding every string across sixty files and keeping the patch rebased on new
QEMU releases is exactly what a shared patch is good for.

### What it does not do

- It substitutes strings and IDs only. It does not describe a coherent
  computer: the ASUS strings it inserts (`M4A88TD-M`, `ASUS-PC`) are an old AMD
  board and do not match a Ryzen 5 4500 or a B550 chipset.
- It leaves the QXL graphics card at `1b36:0100`. The QXL device uses its own
  vendor macro, which the patch does not touch. (The project README used to
  claim otherwise; the Windows and Linux probes both show `1B36`.)
- It does not touch OVMF, timing, or the chipset.
- It is third-party code that runs inside the process that contains the guest.
  It is pinned by commit and SHA-256, applied with a dry run first, and built
  under `runtime/` as an unprivileged user. Read it before changing the pin.

## What this project adds

### QEMU patches (`patches/`, applied in order after the upstream patch)

| Patch | Why |
|---|---|
| `0001-smbios-desktop-chassis` | QEMU hard-codes chassis type "Other" and zero power cords. A desktop reports type 3 and one cord. |
| `0002-smbios-ddr4-memory` | QEMU reports generic "RAM" with unknown widths and 0 V. Report DDR4, 64-bit, 1.2 V. |
| `0003-acpi-ami-oem` | The upstream patch leaves ACPI OEM IDs as `INTEL` / `PC8086`, which contradicts an AMD CPU and an AMI BIOS. Real AMI boards use `ALASKA` / `A M I`. |
| `0004-malware-analysis-hardening` | Found while auditing from inside Windows. (a) The slirp gateway aliased the host's `127.0.0.1`; it is now blocked. (b) The WAET ACPI table exists only on hypervisors; it is no longer emitted. (c) The optical drive product read `ASUS ASUS DVD-ROM`; it now uses the configured model. |

### The `svm` layer (`src/svm/`)

- **One coherent identity per session** (`identity.py`, `config/hardware.toml`).
  Samples cross-check, so each value must agree with the others:
  CPU brand, family/model/stepping, cache sizes, SMBIOS type 4 and type 7 all
  describe the same chip; the board, BIOS vendor/version/date and ACPI OEM ID
  describe the same AMI board; the DIMM size equals installed RAM; the disk
  model, decimal capacity and non-rotational flag describe one SSD; the NIC's
  MAC OUI belongs to the NIC vendor. Every OUI in the pool was checked against
  the IEEE registry.
- **Unique every run.** UUID, serials, MAC, hostname, disk serial and NVRAM
  are regenerated, so a reused sandbox does not leave a stable fingerprint.
- **Binary SMBIOS tables** (`smbios_tables.py`). QEMU's `-smbios` options
  cannot describe cache (type 7), a four-slot memory array (type 16) or a full
  DDR4 DIMM (type 17), so these are written as wire-format structures and loaded
  with `-smbios file=`. A structure with no strings must end in two NUL bytes;
  an earlier version wrote one, which made Windows lose the BIOS, system and
  memory tables entirely.
- **Host-matched CPU.** The generator prefers the identity whose
  family/model/stepping and cache sizes match the host, because KVM passes the
  host microcode revision and cache leaves through.
- **Disposable by construction.** Sessions are qcow2 overlays on a sealed,
  read-only base; `svm destroy` or `--ephemeral` deletes everything the guest
  wrote.

## Validation

Two read-only scripts report what user-mode code in the guest can see. Both
print `[TELL]` for each giveaway and repeat them in a summary.

| Script | Guest | Covers |
|---|---|---|
| `guest/stealth-probe.ps1` | Windows (PowerShell 5.1) | WMI identity, raw SMBIOS structures, storage/NIC/GPU, PCI and PnP devices, drivers/services/processes, ACPI OEM IDs and BIOS registry values, hypervisor hints, optional host-loopback test |
| `guest/stealth-probe.sh` | Linux | DMI, CPU flags and caches, `systemd-detect-virt`, kernel log, `lspci`, disks, NICs, input devices, USB, modules, ACPI tables, WARP egress, optional host-loopback test |

Deliver a script to the guest on a read-only CD so nothing is installed or
shared:

```bash
xorriso -as mkisofs -V PROBE -J -r -o /tmp/probe.iso guest/   # contains both scripts
./bin/svm run --fresh --profile win10 --keep --cdrom /tmp/probe.iso
```

In Windows, open PowerShell and run `powershell -ep bypass -f D:\stealth-probe.ps1`
(check the drive letter; it can be `D:` or `E:`). In Linux:
`sudo mount /dev/sr0 /mnt && sudo bash /mnt/stealth-probe.sh`.

For the isolation test, start `python3 -m http.server 9999 --bind 127.0.0.1` on
the host and add `-HostPort 9999` (Windows) or `--host-port 9999` (Linux). The
probe must report the port as not reachable.

### Measured results (3 Nov 2026, patched QEMU 10.2.2 with patches 0001-0004)

| Check | Windows 10 | Ubuntu 24.04 |
|---|---|---|
| System / board / BIOS | ASUSTeK PRIME B550-PLUS, AMI 3405 | same |
| Chassis | type 3 | type 3 |
| CPU and caches | Ryzen 5 4500, 6C/12T, L2 3 MB, L3 8 MB | same, CPUID L3 8 MiB |
| Memory | one 8 GB DDR4 DIMM, 3200 MT/s | same |
| Disk | WDC WDS500G2B0A, SSD, 465.7 GB | WD/Samsung SSD, `ROTA=0` |
| NIC and OUI | Intel 82574L, Intel OUI | e1000e, Intel OUI |
| Optical drive | ASUS DRW-24D5MT | ASUS DRW-24D5MT |
| Hypervisor bit / `systeminfo` / `systemd-detect-virt` | none | none |
| Host loopback via gateway | not reachable | not reachable |
| WARP | connected | connected, Cloudflare egress |
| Remaining tells | PCI `1B36` (QXL), Basic Display Adapter | PCI `1b36` (QXL), `qxl` module |

The probes do not test timing, GPU behaviour, or checks that need a driver.
Passing them means "no tell found by these checks", not "undetectable".

## Residual gaps and options

1. **GPU (QXL, `1b36:0100`).** Software can read the vendor, the "Basic Display
   Adapter" name, and 0 MB VRAM. Strong fix: GPU passthrough. The host has one
   NVIDIA card in use for its own display, so this needs a second GPU. Weaker
   fix: add a QEMU property to spoof the IDs and ship a matching driver INF;
   the Linux `qxl` driver then no longer binds, so display modes need testing.
2. **OVMF firmware vendor.** Windows records `Ubuntu distribution of EDK II` as
   the BIOS version in the registry. It comes from the firmware image, not from
   QEMU, so it needs a custom OVMF build with a different `PcdFirmwareVendor`.
3. **Chipset.** The emulated chipset is Intel Q35/ICH9 on an AMD identity.
4. **Timing.** `RDTSC` and `CPUID` latency betray KVM. See the RDTSC section of
   `HARDENING.md`.
5. **Windows computer name and machine SID** come from the base image, not the
   session. Rename on first boot or sysprep the base.
6. **Duplicate BGRT ACPI table.** The upstream stub is added next to the
   firmware's own table; a real PC has one.
7. **Activity artifacts.** A fresh install has no browsing history, recent
   files or installed programs. Some samples look for them.
8. **Network visibility.** The WARP adapter and routes are visible, and some
   samples treat a VPN as a sandbox indicator.

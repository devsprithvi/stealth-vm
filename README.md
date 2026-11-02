# svm — stealth Linux VMs

`svm` runs an Ubuntu 24.04 XFCE desktop inside QEMU/KVM with a self-consistent
hardware identity, Cloudflare WARP inside the guest, and Firefox. Each session
is a qcow2 overlay on a sealed base image; state persists across stop/start,
and destroying the session wipes everything it wrote.

The guest gets no shared folders, clipboard sharing, USB passthrough, guest
agent, or host port forward. The host hardware identity is not handed to the
guest. This is built for **isolated software testing and malware analysis**,
not for bypassing anti-cheat in online games. There is no such thing as 100%
stealth; see [Limits](#limits) before relying on it.

## What you get

- Ubuntu 24.04.5 XFCE desktop with a normal panel, Thunar, terminal, task
  manager, screenshot tool, notifications, power manager, and Firefox 159.
- Cloudflare WARP `2026.8.2115.0`, connected before the desktop is useful,
  with a local MDM policy for Always On / locked switch.
- An AMD Ryzen 3 4100 4C/8T CPU identity, 4 GiB RAM, a 500 GB-class WD SSD,
  an ASUS/MSI B550 board, and an AMI BIOS. The generator prefers the CPU model
  matching the host CPUID family/model/stepping, so KVM's microcode revision
  stays consistent with the emulated CPU.
- PS/2 keyboard and mouse, no USB HID tablet. The guest sees no `kvm` module.
- A 500,000,000,000-byte disk (465.7 GiB visible), reported as a non-rotating
  SSD.
- Per-session SMBIOS UUID, serials, MAC, hostname, disk serial, and NVRAM.
- Persistent Firefox profile and WARP registration inside each overlay.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Hypervisor | QEMU 10.2.2 + [`zhaodice/qemu-anti-detection`](https://github.com/zhaodice/qemu-anti-detection) | SMBIOS, ACPI, PCI ID, device-name, and CPUID patches. Built into `runtime/qemu`, never installed over the distro QEMU. Local patches add a desktop chassis, DDR4 DIMM details, and AMI-style ACPI OEM IDs. |
| Firmware | OVMF (distro `ovmf`) | UEFI, Secure Boot optional. |
| Management | This Python CLI + pure QEMU | No libvirt state. One directory per session. |
| Display | SPICE + QXL over a Unix socket | No TCP listener and no host window required for headless runs. |
| Network | QEMU user-mode NAT + WARP in the guest | No bridge, no host ports; WARP hides the host public IP from destinations. |
| Base | Ubuntu 24.04.5, installed once | Sealed read-only; every session is an overlay. |

## Requirements

- x86_64 Linux host with KVM (`/dev/kvm`). Tested on Ubuntu 26.04, kernel 7.0.
- ~15 GiB host RAM recommended (the Linux guest uses 4 GiB).
- ~40 GiB free disk for the base plus a session.
- `sudo` for the one-time build and base provisioning.
- [`uv`](https://docs.astral.sh/uv/) for the Python CLI. `bin/svm` calls
  `uv run`; install it if missing:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- `virt-viewer` for the SPICE window (`remote-viewer`).
- The base image is already built on this machine. Building from scratch also
  needs the Ubuntu ISO and OVMF.

## Quick start

```bash
cd ~/stealth-vm

# Check the host, patched QEMU, OVMF, viewer, and base.
./bin/svm doctor

# Boot a new persistent Linux session. The viewer opens immediately.
./bin/svm run --fresh --profile linux

# Leave the desktop with the normal XFCE shutdown button, or close the
# viewer. The session is kept and can be resumed:
./bin/svm list
./bin/svm run 20261102-105914-c472
```

The Linux profile sets `auto_destroy = false`, so a normal `svm run` keeps the
overlay and its state. To delete one session, use `svm destroy <id>`. To make a
one-shot run that is wiped when the viewer closes, add `--ephemeral`:

```bash
./bin/svm run --fresh --profile linux --ephemeral
```

## Clickable launcher

Install the desktop entry once:

```bash
./scripts/install-launcher.sh
```

It creates `~/.local/share/applications/svm.desktop` and a copy on the desktop.
Clicking **Stealth VM**:

- attaches to the newest running session, or
- resumes the newest stopped session, or
- creates a new Linux session if none exists.

The wrapper is `bin/svm-gui`; it uses `svm list --json` and then calls
`svm attach` or `svm run`. The viewer opens as soon as the SPICE socket is
ready.

## Build the base from scratch

The base is installed once, provisioned in a chroot, then sealed read-only.

```bash
sudo ./bootstrap.sh

# 1. Install Ubuntu 24.04.5. The unattended seed is optional; an interactive
#    install works too. Use the Linux profile so the disk size matches.
./bin/svm base create linux \
  --iso images/iso/ubuntu-24.04.5-live-server-amd64.iso \
  --driver-iso images/iso/seed.iso \
  --profile linux --headless --yes

# 2. Apply the reproducible desktop/WARP/Firefox configuration, clean the
#    image, and seal it. This refuses to run while any session exists.
sudo ./scripts/provision-linux-base.sh

# 3. Boot the result.
./bin/svm run --fresh --profile linux
```

`scripts/configure-linux-base.sh` is the canonical definition of the base. It
installs the XFCE desktop, Firefox from Mozilla's APT repository, WARP from
Cloudflare's APT repository, AMD microcode, and the desktop panel packages. It
removes cloud-init, VMware/SPICE agents, snapd, Intel microcode, apport,
pollinate, sosreport, and other server/telemetry packages; configures DHCP,
IPv6 disablement, hostname-from-SMBIOS, WARP Always On, autologin, passwordless
sudo, Firefox policies, and the `desktop-<uuid6>` hostname service.

The base is sealed with mode `0444`. Session overlays point at it, so do not
modify a base while any overlay exists. `svm` refuses to create a new base over
a running session, and the provisioning script refuses to run if `vms/` has
any session directory.

## Profiles

| Profile | OS | vCPU | RAM | Disk | TPM / Secure Boot | Use |
|---|---|---:|---:|---:|---|---|
| `linux` | Ubuntu 24.04 XFCE | 8 | 4 GiB | 500 GB | no / no | Default; persistent desktop |
| `win10` | Windows 10 | 8 | 8 GiB | 500 GB | no / no | Deferred; needs a working Windows base |
| `win10-perf` | Windows 10 | 12 | 8 GiB | 500 GB | no / no | Deferred; heavy installers |
| `win11` | Windows 11 | 8 | 8 GiB | 500 GB | yes / yes | Experimental and deferred; TPM is detectable |

Disk sizes are decimal GB, matching the number printed on a real SSD label.
The Linux guest therefore sees 465.7 GiB for a 500 GB disk.

The 8-vCPU identity pool contains 4C/8T Ryzen parts. On this host the generator
selects the Ryzen 3 4100 because its family/model/stepping matches the host,
keeping the guest microcode revision consistent. On a different host vendor or
CPUID, it falls back to the configured identity that matches the vCPU count.
The 12-vCPU profile selects a 6C/12T Ryzen 5 3600.

## Commands

```text
svm doctor                     check host, patched QEMU, OVMF, viewer, bases
svm build                      rebuild patched QEMU (deps must be installed)
svm info                       show paths and installed bases
svm version                    versions + whether QEMU is patched
svm paths                      print every path used

svm new [-p PROFILE] [-b BASE] [--dry-run]
svm run [SESSION] [--fresh] [-p PROFILE] [-b BASE] [--keep|--persist|--ephemeral]
        [--headless] [--dry-run] [--viewer EXE] [--no-sandbox] [--debug]
svm list [--json]
svm attach SESSION [--viewer EXE]
svm stop SESSION
svm destroy SESSION [--force]
svm nuke [--force]
svm gc [--days N]
svm identity SESSION

svm base list
svm base create NAME --iso FILE [-p PROFILE] [--driver-iso FILE]
        [--size-gb N] [--headless] [--yes] [--force]
svm base delete NAME [--force]
```

Typical loop:

```bash
./bin/svm run --fresh --profile linux   # new persistent overlay + identity
# use the desktop, then shut down or close the viewer
./bin/svm list                          # session remains, state intact
./bin/svm run <id>                      # resume the same overlay
./bin/svm destroy <id>                  # wipe disk, NVRAM, identity, logs
```

`svm stop` asks the guest to power down and then stops QEMU if needed.
`svm attach` opens a viewer for an already-running session without stopping it.
`svm list --json` is the machine-readable form used by the launcher.

## Folder layout

```text
~/stealth-vm/
├── bin/svm                   CLI entry point (uv)
├── bin/svm-gui               clickable launcher wrapper
├── bootstrap.sh              one-time dependency install + QEMU build
├── config/
│   ├── defaults.toml         shared defaults
│   ├── hardware.toml         CPU/board/RAM/disk/MAC identity pools
│   └── profiles/             linux, win10, win10-perf, win11
├── desktop/svm.desktop       desktop-entry template
├── docs/                     architecture, network, hardening, build notes
├── guest/                    WARP setup notes and scripts
├── images/
│   ├── iso/                  installer ISOs (not wiped)
│   └── base/                 sealed base disk + NVRAM
├── scripts/
│   ├── build-qemu.sh         pinned QEMU + patch build
│   ├── configure-linux-base.sh  in-chroot base definition
│   ├── provision-linux-base.sh  host-side mount/chroot/seal wrapper
│   └── install-launcher.sh   install the desktop entry
├── src/svm/                  Python CLI
├── vms/                      per-session overlays (persistent until destroyed)
├── runtime/qemu/             patched QEMU build (no system install)
├── build/                    downloads and unpacked source
└── logs/
```

A session directory contains:

```text
vms/20261102-105914-c472/
├── disk.qcow2     overlay; backing file is the sealed base
├── vars.fd        per-session OVMF NVRAM
├── tpm/           per-session swtpm state (win11 only)
├── hardware.cfg   generated QEMU -readconfig file
├── qmp.sock       QMP control socket (0700 directory)
├── qemu.pid
├── qemu.log
└── session.json   identity + paths
```

Deleting that directory is the whole cleanup. Nothing is written back to the
base image.

## Configuration

`config/defaults.toml` has the shared values. Profiles override them with the
same section names.

```toml
[general]
default_profile = "linux"
auto_destroy = true
viewer = "remote-viewer"

[resources]
vcpus = 8
memory_mb = 4096
disk_gb = 500          # decimal GB; 465.7 GiB in the guest

[display]
backend = "spice"
video = "qxl"
audio = false

[input]
mode = "ps2"           # ps2 avoids QEMU's USB HID tablet identity

[network]
mode = "user"          # QEMU slirp; no bridge, no host forwards
nic = "e1000e"
subnet = "192.168.1.0/24"
gateway = "192.168.1.1"
dhcp_start = "192.168.1.100"
dns = "192.168.1.2"
ipv6 = false

[hardware]
machine = "q35"
firmware = "ovmf"
secure_boot = false
tpm = false
disk_bus = "sata"
sandbox = true
```

`config/profiles/linux.toml` overrides `auto_destroy = false` so desktop state
persists. A profile only needs the values it changes. Unsupported values are
rejected instead of silently ignored: only `q35`, `ovmf`, `sata`, `spice`,
`qxl`, and `network.mode = "user"` are implemented today.

To add a hardware identity, add an entry to `config/hardware.toml` with a RAM
size and disk capacity that exactly match a profile. The generator fails with a
clear message if no match exists.

## How the stealth layer works

Per session, `svm` generates a self-consistent identity from
`config/hardware.toml`:

- SMBIOS types 0/1/2/3/4/17 (BIOS, system, board, chassis, CPU, RAM).
- System UUID, board/chassis/CPU/RAM serials, disk model + serial + firmware.
- A real-vendor MAC OUI (never `52:54:00`, `00:0C:29`, `00:50:56`, `08:00:27`,
  `00:1C:42`, or `00:15:5D`).
- A Linux desktop hostname derived from the SMBIOS UUID:
  `desktop-<first6>`. `/etc/machine-id` is empty in the base, so systemd
  generates a fresh machine ID in each overlay.
- CPU vendor/family/model/stepping and brand string matching the host vendor,
  the profile's vCPU count, and preferably the host CPUID family/model.
- `-cpu host,kvm=off,hypervisor=off`, `topoext=on`, no Hyper-V enlightenments,
  `vmport=off`, and the anti-detection patch's ACPI/PCI/device renames.
- q35 topology with ICH9 AHCI, EHCI/UHCI, HDA audio, e1000e NIC, QXL VGA, and
  PCIe root ports — modeled on QEMU's own `docs/config/q35-emulated.cfg`, not a
  minimal VM.
- SATA IDENTIFY word 217 set to `1`, so Linux reports the emulated WD/Samsung
  SSD as non-rotating (`ROTA=0`).
- PS/2 input by default. q35 provides an i8042 controller; the guest reports an
  `ASUS PS/2 Mouse`, not `QEMU Tablet`.
- Guest-side `kvm` modules blacklisted. The guest sees no `kvm` module loaded.

No shared folders, no clipboard sharing, no USB redirection, no guest agent,
no host port forwards. The SPICE socket is a Unix socket inside a `0700`
directory. QEMU runs with a seccomp sandbox by default.

## Network and WARP

The guest is behind QEMU user-mode NAT:

```text
guest 192.168.1.100 → slirp 192.168.1.1 → host socket → WARP → Cloudflare → destination
```

- Destinations see a Cloudflare WARP egress IP, not your home IP.
- Your host sees only WireGuard/HTTPS traffic to Cloudflare.
- Cloudflare sees your host's public IP and the WARP device registration.
  WARP is a privacy proxy, not an anonymity network.
- IPv6 is disabled in slirp and in the guest. The netplan configuration also
  sets `link-local: []`, so networkd does not add an `fe80::/64` address to the
  guest NIC.
- There are no host port forwards and no inbound path to the guest.

The WARP client and MDM policy are installed **inside the base**. The first
boot registers the device in that session's overlay; the registration persists
across stop/start and is deleted with the session. The local MDM policy sets
`switch_locked=true`, `auto_connect=0`, and `service_mode=warp`.
`warp-auto-connect.service` starts `warp-svc`, applies the policy, registers
the overlay if needed, and connects. If WARP is unavailable, the guest falls
back to direct NAT unless Lockdown/Always On is enforced; the local policy is
intended to fail closed.

Two caveats are documented in full in `docs/NETWORK.md`:

- The slirp gateway `192.168.1.1` is a host alias. The guest can reach host
  loopback services at that address. Stop sensitive local services while a
  guest runs, or use a network namespace / passt with `map-host-loopback=none`.
- The guest RTC is UTC for Linux. Windows profiles use localtime, which can
  reveal the host timezone through the initial RTC.

## Passwordless guest

The Linux guest is intentionally passwordless:

- `user` has an empty shadow entry.
- LightDM autologins to XFCE with no prompt.
- `sudo` is passwordless for `user`.
- No password, hash, or key file is stored in the project. The autoinstall
  seed is generated from passwordless `images/iso/user-data`.

This is an isolated analysis VM, not a model for a real workstation. Do not
copy the passwordless configuration to a host you care about.

## Limits

- **No stack is 100% undetectable.** This defeats common user-mode checks
  (DMI/SMBIOS strings, the `hypervisor` CPUID bit, QEMU/Red Hat PCI IDs, device
  names, disk model, MAC OUI) and many anti-VM installers. Kernel anti-cheat,
  TPM attestation, timing analysis, memory forensics, and targeted sandbox
  evasion can still detect it.
- **QXL is the weakest device.** The guest reports
  `Red Hat, Inc. QXL paravirtual graphic card [1b36:0100]`. The patch set does
  not turn QXL into a real GPU. GPU passthrough is the only strong fix; this
  host has a single RTX 3060 in use, so passthrough is unavailable here.
- **q35 is an Intel chipset.** On an AMD host the CPU identity is AMD but the
  chipset PCI IDs are Intel. Most software does not compare them; sophisticated
  checks can.
- **e1000e on a B550 board** is plausible for many boards but not universal.
  A determined check can compare the NIC model to the board.
- **Disk capacity is decimal 500 GB.** The guest sees 465.7 GiB, like a real
  500 GB SSD. A check that compares the model's marketing capacity to the
  reported size can still notice unusual choices.
- **CPU cache and feature details come from the host.** `-cpu host` keeps the
  host's feature flags and cache topology; only family/model/stepping and the
  brand string are overridden. A check that compares cache size to the CPU
  model can notice.
- **Timing is not hidden.** RDTSC/TSC timing can reveal KVM. The optional
  host-kernel RDTSC patch is documented in `docs/HARDENING.md`; it is not
  default because it requires rebuilding the host kernel.
- **The Windows profiles are deferred.** The patched OVMF currently fails to
  boot the Windows ISO and falls back to PXE. Linux is the working profile.
- **The hypervisor is in your trusted computing base.** You are applying a
  third-party patch to QEMU to contain untrusted code. Read the patch, build
  from the pinned tarball, and keep the build under `runtime/` so you can
  delete it.
- **Guest RAM is not guaranteed zeroed** after QEMU exits. Use encrypted swap,
  disable core dumps, and reboot between high-risk runs.
- **Not for online-game anti-cheat.** That violates the game's ToS and gets
  accounts banned. Use it on software you are allowed to test.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `QEMU not found` / `NOT patched` | `sudo ./bootstrap.sh`, then `./bin/svm doctor` |
| `qemu-img not found` | `sudo apt install qemu-utils` or rerun bootstrap |
| `OVMF not found` | `sudo apt install ovmf`; check `/usr/share/OVMF` |
| `no SPICE viewer found` | `sudo apt install virt-viewer`; or use `--headless` |
| `KVM: permission denied` | `sudo usermod -aG kvm "$USER"`; log out and back in |
| Guest has no network | Check `systemctl status systemd-networkd`; the NIC is e1000e |
| WARP will not connect | Check guest clock, UDP 2408, captive portal, registration |
| Traffic leaks when WARP drops | Keep Lockdown/Always On enabled in the MDM policy |
| XFCE panel plugin missing | Rebuild the base; `xfce4-power-manager-plugins` is required |
| Build OOM-killed | `JOBS=4 ./bootstrap.sh --build --force` |
| Session kept after Ctrl-C | Expected; `svm destroy <id>` or `svm nuke` |
| Launcher opens nothing | Run `./bin/svm list --json` and `./bin/svm doctor` |

Full build errors are covered in `docs/BUILD.md`.

## Documentation

- `docs/ARCHITECTURE.md` — session lifecycle, identity model, QEMU device map.
- `docs/NETWORK.md` — WARP, DNS, leaks, LAN isolation, stronger anonymity.
- `docs/HARDENING.md` — host hardening, dedicated user, RDTSC/KVM patch.
- `docs/BUILD.md` — pinned versions, hashes, build troubleshooting.
- `guest/README.md` — WARP setup and Linux base notes.

## Credits

- [`zhaodice/qemu-anti-detection`](https://github.com/zhaodice/qemu-anti-detection) — QEMU patch set (GPL-2.0).
- [`WCharacter/RDTSC-KVM-Handler`](https://github.com/WCharacter/RDTSC-KVM-Handler) — KVM RDTSC research.
- QEMU, OVMF, virt-viewer, Cloudflare WARP.

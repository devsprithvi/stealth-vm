# svm: a malware-analysis VM that looks like a real PC

`svm` runs Windows 10 or Ubuntu desktops in QEMU/KVM with a hardware identity
that reads like an ordinary ASUS/AMD desktop. It is built for **malware
analysis and isolated software testing**: evasive samples check whether they
are inside a virtual machine and change behaviour when they are, so the
sandbox has to pass those checks for the analyst to see what the sample really
does.

Each session is a disposable qcow2 overlay on a sealed base image with a freshly
generated identity. Nothing a sample does is written to the base. Destroy the
session and everything it wrote is gone; keep it and the same machine resumes.

> **Scope.** Use this on systems you own or are authorized to test, for
> analysis, research and defensive work. It is not a tool for bypassing
> online-game anti-cheat or any service's terms; that gets accounts banned and
> is not what it is built or tested for.
>
> **No VM is 100% undetectable.** The tests in this repository find no tell in
> the checks they cover, and the known gaps are listed under [Limits](#limits).
> Read them before relying on this for a sample that targets VM detection.

## What you get

| | Windows 10 (`win10`) | Ubuntu 24.04 (`linux`) |
|---|---|---|
| Identity | ASUSTeK PRIME B550-PLUS, AMI BIOS, Ryzen 5 4500 (6C/12T) | same family of identities, regenerated per session |
| Memory / disk | 8 GiB (one DDR4 DIMM) / 500 GB SSD (465.7 GiB) | same |
| Network | Intel e1000e behind QEMU user-mode NAT, Cloudflare WARP inside the guest | same |
| Input / display | PS/2 keyboard and mouse, SPICE + QXL, 1920x1080 | same |
| Guest integration | none: no agent, clipboard, shared folders or USB passthrough | none |
| Default lifetime | disposable (wiped when closed) | disposable (wiped when closed) |
| Validated | yes, see [Validation](#validation) | yes |

Every run is a new machine: new UUID, serials, MAC address, hostname, disk
serial and NVRAM, and inside Windows a new SID, MachineGuid, computer name,
NTFS volume serial, user profile and WARP registration. Nothing installed or
used in an earlier run is present. [`docs/FRESHNESS.md`](docs/FRESHNESS.md)
lists exactly what is regenerated, what is not, and how to verify it. See [`docs/ANTI-DETECTION.md`](docs/ANTI-DETECTION.md) for how the
identity is built, why the values are cross-consistent, and what the upstream
anti-detection patch does.

## How the stealth works

1. **Patched QEMU.** QEMU 10.2.2 is built with
   [`zhaodice/qemu-anti-detection`](https://github.com/zhaodice/qemu-anti-detection)
   (pinned by commit and SHA-256), which renames the QEMU, Red Hat and Bochs
   strings and IDs that guests and malware look for. Four local patches in
   `patches/` fix what that patch leaves inconsistent or open (desktop chassis,
   DDR4 DIMMs, AMI ACPI OEM IDs, host-loopback block, no WAET table).
2. **A coherent identity.** `svm` generates SMBIOS, CPU, cache, memory, disk and
   MAC values that agree with each other and with the host CPU, because
   samples cross-check them.
3. **Nothing to find in the guest.** No guest additions, agent or shared
   folders; the guest sees no `kvm` module and no hypervisor CPUID bit.
4. **Probes to prove it.** `guest/stealth-probe.ps1` and `guest/stealth-probe.sh`
   report what code inside the guest can see.

## Requirements

- x86_64 Linux host with KVM (`/dev/kvm`) and an AMD CPU. The identity pool is
  matched to the host CPU; an Intel host falls back to the Intel pool.
- 12 host threads and 16 GiB RAM recommended. The default profiles give the
  guest 12 vCPUs and 8 GiB (see [Profiles](#profiles)).
- About 40 GiB free disk per base plus sessions.
- `sudo` once, for the build dependencies.
- [`uv`](https://docs.astral.sh/uv/) for the Python CLI, and `virt-viewer`
  (`remote-viewer`) for the SPICE window.

## Quick start

```bash
cd ~/stealth-vm
sudo ./bootstrap.sh                 # first time only: dependencies + patched QEMU
./bin/svm doctor                    # host, QEMU patch set, OVMF, viewer, bases

./bin/svm run --fresh --profile win10     # disposable Windows desktop
./bin/svm run --fresh --profile linux     # disposable Linux desktop
```

Close the viewer or shut the guest down from inside. The session is deleted.
The desktop launcher (`bin/svm-gui`) does the same: every click starts a new
machine; it never resumes an old one. To keep a session on purpose:

```bash
./bin/svm run --fresh --profile win10 --keep   # keep it after closing
./bin/svm list                      # sessions and their state
./bin/svm run <id>                  # resume a kept session
./bin/svm destroy <id>              # wipe disk, NVRAM, identity and logs
```

Flags that change lifetime: `--ephemeral` (wipe on exit), `--keep` / `--persist`
(never wipe).

## Analysing a sample

1. Start a fresh session: `./bin/svm run --fresh --profile win10`.
2. Get the sample into the guest without a shared folder. Download it inside
   the guest, or attach it as a read-only CD:
   `./bin/svm run --fresh --profile win10 --cdrom sample.iso`
   (build the ISO with `xorriso -as mkisofs -V SAMPLE -J -r -o sample.iso dir/`).
3. Type text into the guest through emulated PS/2 hardware keystrokes, with no
   clipboard channel: `./bin/svm paste 'some command'` (omit the text to type
   the host clipboard).
4. When finished, `./bin/svm destroy <id>`. The base is untouched, so the next
   session starts clean with a different identity.

**Network exposure.** The guest reaches the Internet through Cloudflare WARP, so
the sample sees a WARP egress address, not your IP. The guest cannot reach host
loopback services (blocked in the QEMU patch) and has no inbound path. QEMU's
user-mode networking still lets the guest connect to whatever your host can,
including devices on your LAN. See [`docs/NETWORK.md`](docs/NETWORK.md) for the
risk and mitigations before running worms or anything that scans networks.

## Validation

Run a probe inside the guest to see what a sample could see. Both print
`[TELL]` for each giveaway and a summary at the end.

```bash
xorriso -as mkisofs -V PROBE -J -r -o /tmp/probe.iso guest/
./bin/svm run --fresh --profile win10 --keep --cdrom /tmp/probe.iso
```

- Windows: open PowerShell, run `powershell -ep bypass -f D:\stealth-probe.ps1`
  (the drive letter can be `D:` or `E:`; check with `gdr`).
- Linux: `sudo mount /dev/sr0 /mnt && sudo bash /mnt/stealth-probe.sh`.
- Isolation test: start `python3 -m http.server 9999 --bind 127.0.0.1` on the
  host and add `-HostPort 9999` (Windows) or `--host-port 9999` (Linux). The
  probe must say the port is not reachable.

Results on 3 Nov 2026 with the patch set in this repository:

| | Windows 10 | Ubuntu 24.04 |
|---|---|---|
| Tells found | 2: PCI vendor `1B36` and "Microsoft Basic Display Adapter" (both the QXL GPU) | 2: PCI vendor `1b36` and the `qxl` kernel module (same GPU) |
| Identity, CPU, caches, memory, disk, NIC, optical drive | consistent | consistent |
| Hypervisor bit, `systeminfo`, `systemd-detect-virt` | clean | clean (`none`) |
| Host loopback reachable | no | no |
| WARP | connected | connected |

Not covered by the probes: timing checks, GPU behaviour and driver-level
checks. The full table with every check is in
[`docs/ANTI-DETECTION.md`](docs/ANTI-DETECTION.md#validation).

## Profiles

| Profile | OS | vCPU | RAM | Disk | TPM / Secure Boot | Lifetime |
|---|---|---:|---:|---|---|---|
| `win10` | Windows 10 | 12 | 8 GiB | 500 GB | no / no | disposable |
| `win10-perf` | Windows 10 | 12 | 8 GiB | 500 GB | no / no | disposable |
| `linux` | Ubuntu 24.04 | 12 | 8 GiB | 500 GB | no / no | disposable |
| `win11` | Windows 11 | 8 | 8 GiB | 500 GB | yes / yes | experimental; the emulated TPM is detectable |

Why 12 vCPUs: the host's Ryzen 5 4500 is the only identity whose cache sizes
match the real chip, and KVM passes cache and microcode details through. With
fewer vCPUs the generator must pick a different CPU whose advertised cache
disagrees with what CPUID reports. The cost is that the guest can use every host
thread; lower `vcpus` in the profile if the host becomes sluggish, and accept
the mismatch described in
[`docs/ANTI-DETECTION.md`](docs/ANTI-DETECTION.md#the-svm-layer-srcsvm).

## Commands

```text
svm doctor                     check host, patched QEMU (and that it matches patches/), OVMF, viewer, bases
svm build                      rebuild patched QEMU (deps must be installed)
svm info | version | paths     show paths, versions, whether QEMU is patched

svm new [-p PROFILE] [-b BASE] [--dry-run]
svm run [SESSION] [--fresh] [-p PROFILE] [-b BASE] [--keep|--persist|--ephemeral]
        [--headless] [--cdrom ISO] [--dry-run] [--viewer EXE] [--no-sandbox] [--debug]
svm list [--json]
svm attach SESSION             open a viewer for a running session
svm stop SESSION               ask the guest to power down, then stop QEMU
svm destroy SESSION [--force]
svm nuke [--force]             delete every session (bases untouched)
svm gc [--days N]              delete old stopped sessions
svm identity SESSION           show the generated hardware identity
svm paste [TEXT] [-s SESSION]  type text via emulated PS/2 keystrokes

svm base list
svm base create NAME --iso FILE [-p PROFILE] [--driver-iso FILE]
        [--size-gb N] [--headless] [--yes] [--force]
svm base delete NAME [--force]
```

## Base images

A base is installed once, then sealed read-only (`0444`). Every session is an
overlay on it, so never modify a base while sessions exist; `svm` refuses to.

**Linux.** `scripts/configure-linux-base.sh` is the definition of the Linux base:
desktop, Firefox, Cloudflare WARP with an always-on policy, per-session
hostname from the SMBIOS UUID, and removal of installer, cloud and telemetry
packages.

```bash
sudo ./scripts/provision-linux-base.sh   # configure and seal the installed base
```

**Windows.** `guest/windows/` holds the answer files and scripts. One command
installs Windows from the ISO, enters Audit mode (no user ever signs in), and
runs `sysprep /generalize`, so every session gets a new SID, MachineGuid and
computer name on first boot. WARP is staged but not installed or registered in
the base.
```bash
./scripts/build-win10-base.sh        # unattended, about 30-40 minutes
```
The previous base is moved to `images/base/backup/`. See
[`guest/README.md`](guest/README.md) and [`docs/FRESHNESS.md`](docs/FRESHNESS.md).

## Configuration

`config/defaults.toml` holds shared values; profiles in `config/profiles/`
override only what they change. Unsupported values are rejected rather than
silently ignored: only `q35`, `ovmf`, `sata`, `spice`, `qxl` and
`network.mode = "user"` are implemented.

`config/hardware.toml` is the identity pool: CPUs (with cache sizes), boards,
memory kits, disks and MAC OUIs. RAM and disk sizes must match a profile
exactly, and every OUI must match the IEEE registry. Add entries there rather
than changing the generator.

## Folder layout

```text
~/stealth-vm/
├── bin/svm, bin/svm-gui      CLI entry point and clickable launcher
├── bootstrap.sh              one-time dependencies + patched QEMU build
├── config/                   defaults, identity pool, profiles
├── docs/                     architecture, anti-detection, network, hardening, build
├── guest/                    stealth probes, Windows base sources (guest/windows), guest notes
├── images/iso, images/base   installer ISOs, sealed bases (+ backups)
├── patches/                  local QEMU patches, applied after the upstream one
├── scripts/                  QEMU build, Windows/Linux base builds, freshness check, launcher install
├── src/svm/                  Python CLI (cli, qemu, state, identity, smbios_tables, paste)
├── runtime/qemu/             patched QEMU (never installed system-wide)
├── vms/                      per-session overlays
└── build/, logs/
```

A session directory (`vms/<id>/`) holds `disk.qcow2`, `vars.fd`, `smbios/`,
`hardware.cfg`, the QMP and SPICE sockets, `qemu.log` and `session.json`.
Deleting it is the whole cleanup.

## Limits

Ordered by how easily a sample can use them:

1. **Timing.** `RDTSC` and `CPUID` latency reveal KVM. Not addressed; an optional
   host-kernel patch is described in `docs/HARDENING.md`.
2. **GPU.** The guest sees the QXL card (`1b36:0100`). Windows shows "Microsoft
   Basic Display Adapter" with no VRAM, Linux loads `qxl`. The strong fix is
   GPU passthrough, which needs a second GPU (this host has one).
3. **Firmware vendor.** OVMF identifies itself as "EDK II" and Windows stores
   `Ubuntu distribution of EDK II` as the BIOS version. A custom OVMF build is
   required to change it.
4. **Chipset.** Intel Q35/ICH9 IDs sit under an AMD/ASUS identity.
5. **Windows computer name and SID** come from the base, not the session.
6. **Fresh install.** No browsing history, recent files or installed programs.
7. **The WARP adapter** is visible, and some samples treat a VPN as a sandbox hint.
8. **LAN reachability and guest memory.** User-mode NAT can reach the host's LAN,
   and guest RAM is not guaranteed zeroed after QEMU exits (see `docs/NETWORK.md`
   and `docs/HARDENING.md`).
9. **The hypervisor is in your trusted computing base.** You are applying a
   third-party patch to the QEMU that contains untrusted code. It is pinned by
   hash and built under `runtime/`; read it before changing the pin.
10. **Passwordless guest.** The Linux user has an empty password and passwordless
    `sudo`. This is an isolated analysis VM, not a workstation model.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `QEMU not found` / `NOT patched` | `sudo ./bootstrap.sh`, then `./bin/svm doctor` |
| `doctor` says "patches/ changed since the build" | `./bootstrap.sh --build` (incremental) |
| `OVMF not found` | `sudo apt install ovmf` |
| `no SPICE viewer found` | `sudo apt install virt-viewer` or use `--headless` |
| `KVM: permission denied` | `sudo usermod -aG kvm "$USER"`, then log in again |
| Guest has no network | Check `qemu.log`; the NIC is an e1000e |
| WARP will not connect | Check the guest clock, UDP 2408 and captive portals |
| Windows probe drive letter | The probe CD is `D:` or `E:`; run `gdr` |
| Host feels slow while a guest runs | Lower `vcpus` in the profile |
| Build OOM-killed | `JOBS=4 ./bootstrap.sh --build --force` |

## Documentation

- [`docs/FRESHNESS.md`](docs/FRESHNESS.md): what is new in every session, what is shared, how to verify.
- [`docs/ANTI-DETECTION.md`](docs/ANTI-DETECTION.md): what the upstream patch does, what this project adds, measured results, residual gaps.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): session lifecycle, identity model, QEMU device map.
- [`docs/NETWORK.md`](docs/NETWORK.md): WARP, DNS, leaks, LAN exposure.
- [`docs/HARDENING.md`](docs/HARDENING.md): host hardening, dedicated user, timing patch.
- [`docs/BUILD.md`](docs/BUILD.md): pinned versions, hashes, build troubleshooting.
- [`guest/README.md`](guest/README.md): guest setup and probes.

## Credits

- [`zhaodice/qemu-anti-detection`](https://github.com/zhaodice/qemu-anti-detection): QEMU patch set (GPL-2.0).
- [`WCharacter/RDTSC-KVM-Handler`](https://github.com/WCharacter/RDTSC-KVM-Handler): KVM RDTSC research.
- QEMU, OVMF, virt-viewer, Cloudflare WARP.

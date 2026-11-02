# Architecture

`svm` is a thin orchestration layer around one patched QEMU binary. It does not
use libvirt, a daemon, a bridge, or a shared image store. A session is a
directory; deleting the directory is the cleanup.

## Goals and non-goals

**Goals**

- Run untrusted or VM-hostile software in a guest that looks like a normal
  desktop to user-mode inspection.
- Never expose the host's SMBIOS, disk serials, MAC address, hostname, or
  public IP to the guest.
- Keep each run isolated: fresh overlay, NVRAM, identity, and TPM state (when
  enabled) on creation. Sessions persist by default; `--ephemeral` deletes the
  session when the viewer closes.
- Keep the trusted computing base small and auditable: one QEMU process, one
  Python CLI, no management daemon.

**Non-goals**

- Defeating kernel anti-cheat, TPM attestation, or a determined human analyst.
- GPU passthrough, Looking Glass, nested virtualization, or remote desktops.
- Anonymity against Cloudflare. WARP is a privacy proxy, not Tor.
- Multi-user orchestration, snapshots, live migration, or remote desktops.

## Trust boundaries

```text
┌──────────────────────────── host (trusted) ────────────────────────────┐
│  Linux kernel + KVM          QEMU (patched, seccomp, unprivileged)     │
│  ┌──────────────┐            ┌──────────────────────────────────────┐  │
│  │ /dev/kvm     │◄──────────►│ qcow2 overlay  ──► read-only base    │  │
│  └──────────────┘            │ per-session OVMF NVRAM               │  │
│                              │ swtpm (win11 only)                   │  │
│                              │ SPICE unix socket ──► viewer         │  │
│                              │ slirp ──► host sockets ──► Internet  │  │
│                              └───────────────┬──────────────────────┘  │
│                                              │ virtio? no: AHCI,       │
│                                              │ e1000e, QXL, USB, HDA   │
│                              ┌───────────────▼──────────────────────┐  │
│                              │ guest (untrusted)                    │  │
│                              │ Windows/Linux + WARP client          │  │
│                              └──────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

- The **host kernel and KVM** are trusted. A KVM bug or a QEMU escape is outside
  the model; see `HARDENING.md`.
- **QEMU** is trusted to enforce the device model, but it is unprivileged,
  sandboxed with seccomp, and built under `runtime/qemu` rather than installed
  system-wide.
- The **guest** is untrusted. Everything it writes lives in the session
  directory. It gets no host folders, clipboard, USB redirection, guest agent,
  or TCP listener.
- The **anti-detection patch** is third-party code. It is pinned by SHA-256 and
  built from a pinned QEMU tarball; review it before trusting it.

## Components

| Component | Path | Responsibility |
|---|---|---|
| CLI | `src/svm/cli.py` | Commands, lifecycle, swtpm, viewer, prompts |
| Config | `src/svm/config.py` | TOML merge, path discovery, OVMF lookup |
| Identity | `src/svm/identity.py` | Self-consistent SMBIOS/CPU/RAM/disk/MAC identity |
| State | `src/svm/state.py` | Sessions, overlays, bases, stop/destroy, QMP |
| QEMU rendering | `src/svm/qemu.py` | `hardware.cfg`, command line, viewer, patch check |
| Defaults | `config/defaults.toml` | Global resources, display, network, hardware |
| Pools | `config/hardware.toml` | CPU, board, RAM, disk, MAC identities |
| Profiles | `config/profiles/*.toml` | Per-use-case overrides |
| Build | `scripts/build-qemu.sh` | Pinned QEMU + patch build into `runtime/qemu` |
| Bootstrap | `bootstrap.sh` | One-time apt dependencies, then build as the desktop user |

## Project layout

```text
stealth-vm/
├── bootstrap.sh              one-time sudo dependency install + build
├── bin/svm                   uv wrapper; exports SVM_ROOT
├── config/
│   ├── defaults.toml         [general] [resources] [display] [network] [hardware]
│   ├── hardware.toml         identity pools
│   └── profiles/             win10, win10-perf, win11, linux
├── src/svm/                  Python package
├── scripts/build-qemu.sh     pinned build
├── guest/                    WARP setup scripts and guest notes
├── docs/                     this directory
├── images/
│   ├── iso/                  installer ISOs (not wiped)
│   └── base/                 sealed bases: .qcow2, .vars.fd, .json, .tpm/
├── vms/                      per-session overlays (persistent until destroyed)
├── runtime/qemu/             patched build; never installed over distro QEMU
├── build/                    downloads and unpacked source
└── logs/
```

`SVM_ROOT` overrides the project root. All paths are derived from it. Base
metadata stores absolute paths, so moving the tree after sealing a base requires
recreating or editing the base JSON.

## Session lifecycle

### 1. Bootstrap

`sudo ./bootstrap.sh` installs build/runtime packages, then drops privileges to
`$SUDO_USER` and runs `scripts/build-qemu.sh`. QEMU is configured with
`--prefix=$ROOT/runtime/qemu`; `make install` writes only there. Nothing is
installed into `/usr/local` or over the distro's QEMU.

### 2. Base install

`svm base create NAME --iso FILE` creates a real qcow2 disk in
`images/base/NAME.qcow2` (not an overlay), copies the OVMF VARS template, and
starts QEMU with the installer ISO attached as a SATA CD-ROM. The install
session is a transient `vms/_install-*` directory whose `session.json` points at
the base disk and NVRAM.

When QEMU exits, `svm` asks whether to seal the base. `--yes` skips the prompt
for scripted installs. Sealing:

- writes `images/base/NAME.json`;
- `chmod 0444` on the base disk and VARS;
- deletes the transient install session.

If the install does not exit cleanly, or you answer "no", the install session is
kept under `vms/` for inspection. Re-run with `--force` to start over.

### 3. Session creation

`svm new` / `svm run --fresh`:

1. loads the profile and merges it over `defaults.toml`;
2. resolves the base named by the profile (`base = "linux"` by default) and
   checks that its OS, Secure Boot mode, and TPM availability match the
   profile;
3. generates a new identity from `hardware.toml`, using the base's recorded
   virtual disk size;
4. creates `vms/<id>/` with mode `0700`;
5. creates `disk.qcow2` as a qcow2 overlay backed by the read-only base;
6. copies the base VARS to `vars.fd` and makes it writable (`0600`);
7. copies base TPM state to `tpm/` if the profile uses a TPM;
8. writes `session.json` atomically.

The base is never opened for writing. All guest writes go to the overlay.

### 4. Boot

`svm run` renders `hardware.cfg`, starts QEMU in its own process group, waits for
the SPICE Unix socket, and launches `remote-viewer` (or `spicy`). If the viewer
exits, `svm` asks the guest to power down via QMP, then stops QEMU if needed.

### 5. Shutdown and cleanup

On a clean exit, the profile's `general.auto_destroy` decides what happens. The
Linux profile sets `auto_destroy = false`, so the overlay, NVRAM, identity,
Firefox profile, and WARP registration remain for the next `svm run <id>`. A
profile with `auto_destroy = true`, or any run with `--ephemeral`, runs:

```text
stop_session()  -> QMP system_powerdown, wait, QMP quit, SIGTERM, SIGKILL
_stop_swtpm()   -> SIGTERM, wait, SIGKILL
destroy_session() -> shutil.rmtree(vms/<id>)
```

The overlay, NVRAM, TPM state, sockets, logs, and identity are deleted. The base
and ISOs are never deleted. If QEMU exits non-zero or the session was
interrupted, it is kept for inspection; `svm destroy <id>` or `svm nuke`
removes it.

`svm gc` removes stopped sessions older than `--days` and abandoned
`_install-*` directories older than one day.

## Identity model

Each session gets a new identity. The generator is intentionally strict about
self-consistency:

- **CPU**: the pool is selected by the host CPU vendor (`AuthenticAMD` ->
  `[amd]`, otherwise `[intel]`). The generator requires a CPU whose thread count
  exactly equals the profile's `resources.vcpus`; otherwise it fails rather
  than advertise a different topology. Within that set it prefers a model
  whose family/model/stepping matches the host CPUID; on this host that selects
  the Ryzen 3 4100 (Renoir). `topoext=on` keeps the guest's 4C/8T CPUID
  topology consistent.
- **Board**: random from the selected vendor's `boards` pool. BIOS vendor,
  version, date, board family, and SKU come from the same entry.
- **RAM**: an exact `size_mb` match is required. The guest sees one DIMM whose
  size equals `-m` (QEMU builds type 17 itself); the configured kit supplies
  manufacturer, part number, speed, and serial.
- **Disk**: an exact `capacity_gb` match is required. The disk model, serial
  prefix, and firmware revision are exposed through the AHCI `ide-hd` device.
- **MAC**: a real-vendor OUI from `mac_ouis`, with a random NIC-specific tail.
  Intel OUIs are preferred because the emulated NIC is an Intel e1000e. The
  multicast bit is cleared. Hypervisor OUIs (`52:54:00`, `00:0C:29`,
  `00:50:56`, `08:00:27`, `00:1C:42`, `00:15:5D`) are not in the pool.
- **Hostname**: `DESKTOP-XXXXXXX` for Windows, `desktop-<first6-of-UUID>` for
  Linux. A small systemd service sets the Linux hostname from the SMBIOS UUID.
- **UUID and serials**: random UUIDv4; 10-character serials for system, board,
  chassis, CPU, and RAM; disk serial is prefix + 8 characters, capped at 20.

SMBIOS tables emitted per session:

| Type | Contents |
|---|---|
| 0 | BIOS vendor, version, date, UEFI flag |
| 1 | System manufacturer, product, version, serial, UUID, SKU, family |
| 2 | Base board manufacturer, product, version, serial, asset tag |
| 3 | Chassis manufacturer, version, serial, asset tag, SKU; desktop type, 1 power cord |
| 4 | CPU socket, manufacturer, version, serial, asset tag, part, max/current speed, processor family |
| 17 | DIMM locator, bank, manufacturer, serial, asset tag, part, speed, DDR4 / 64-bit / 1.2 V (local patch) |

QEMU's `-uuid` supplies the system UUID; type 1 `uuid` is skipped to avoid a
duplicate. Type 17 size is computed by QEMU from `-m`, not supplied by the
generator (QEMU 10.2.2 has no `size` option for type 17).

The CPUID leaf 1 family/model/stepping is overridden to match type 4, the brand
string is replaced with `model-id`, the hypervisor bit is cleared
(`hypervisor=off`), the KVM signature is hidden (`kvm=off`), and the VMware
timing leaf is disabled (`vmware-cpuid-freq=false`).

## Hardware model

The guest sees a q35 machine modeled on QEMU's own
`docs/config/q35-emulated.cfg`, not a minimal VM, with an e1000e NIC and QXL
VGA instead of the example's e1000 and std VGA:

| PCI address | Device | Notes |
|---|---|---|
| `00:00.0` | q35 host bridge | Intel chipset IDs |
| `00:01.0` | QXL VGA (`1b36:0100`) | 64 MiB VRAM, no virtio-gpu; the ID is still Red Hat |
| `00:19.0` | Intel e1000e | real-vendor MAC, no PXE ROM |
| `00:1a.0-2` | ICH9 UHCI companions | USB 1.1 (second controller) |
| `00:1a.7` | ICH9 EHCI | USB 2.0 (second controller) |
| `00:1b.0` | ICH9 HDA + duplex codec | audio device always present; backend `none` unless `display.audio = true` |
| `00:1c.0-3` | IOH3420 PCIe root ports | realistic topology |
| `00:1d.0-2` | ICH9 UHCI companions | USB 1.1 |
| `00:1d.7` | ICH9 EHCI | USB 2.0; USB HID only when `input.mode = "usb"`; default is PS/2 |
| `00:1e.0` | i82801b11 PCI bridge | legacy PCI bridge |
| `00:1f.0` | ICH9 LPC | |
| `00:1f.2` | ICH9 AHCI | SATA disk on `ide.0`, CDs on `ide.1`/`ide.2` |
| `00:1f.3` | ICH9 SMBus | |

The disk is `ide-hd` on `ide.0` with `model`, `serial`, and `ver` set from the
identity. qcow2 uses `cache=writeback` and `discard=unmap`; SATA IDENTIFY word
217 is set to 1 so the guest reports the SSD as non-rotating (`ROTA=0`). The
NIC has `romfile=""` so no PXE option ROM is exposed.

`-nodefaults` removes QEMU's default VGA, NIC, floppy, serial, parallel, and
monitor. `-display none`, `-monitor none`, `-serial none`, and `-parallel none`
mean the only human interface is the SPICE Unix socket.

## QEMU invocation

Key flags, in order:

```text
-name <hostname>,process=svm-<id>
-machine q35,accel=kvm,kernel_irqchip=on,vmport=off,usb=off[,smm=on]
-cpu host,family=...,model=...,stepping=...,model-id=...,
     kvm=off,hypervisor=off,vmware-cpuid-freq=false,enforce=false,
     host-phys-bits=true,topoext=on
-smp <threads>,sockets=1,cores=<cores>,threads=<threads/cores>
-m <memory_mb>M
-uuid <uuid>
-smbios type=0,... (one per table)
-rtc base=localtime|utc,driftfix=slew
-boot order=d|c
-nodefaults -display none -monitor none -serial none -parallel none
-readconfig <session>/hardware.cfg
-qmp unix:<session>/qmp.sock,server=on,wait=off
-pidfile <session>/qemu.pid
-msg timestamp=on
-drive if=pflash,... (OVMF CODE read-only, VARS writable)
-sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny
-spice unix=on,addr=<session>/spice.sock,disable-ticketing=on,
       disable-copy-paste=on,disable-agent-file-xfer=on,agent-mouse=off,
       image-compression=off,playback-compression=off,streaming-video=off
```

`-sandbox` is enabled unless `--no-sandbox` is passed. `spawn=deny` means QEMU
cannot launch helper processes; do not combine it with backends that spawn
daemons (for example passt).

Secure Boot adds `smm=on` and the CFI pflash `secure=on` global. TPM adds a
swtpm chardev, `tpmdev emulator`, and `tpm-crb` device.

## Why no libvirt

- No daemon to keep running, no XML to audit, no storage pool or virtual
  network to clean up.
- One QEMU process per session; one directory per session; one `rmtree`.
- The exact command line is visible with `svm run --dry-run`.
- No bridge, no DHCP server on the host, no NAT rules outside QEMU.

The tradeoff is no snapshots, no remote management, and no automatic resource
scheduling. Those are not needed for an isolated desktop test VM.

## Extension points

The code intentionally rejects settings it does not implement rather than
silently ignoring them:

- `hardware.machine` must be `q35`.
- `hardware.firmware` must be `ovmf`.
- `hardware.disk_bus` must be `sata`.
- `display.backend` must be `spice`.
- `display.video` must be `qxl`.
- `network.mode` is currently `user` (slirp).

To add a profile, copy `config/profiles/linux.toml` and override sections. To
add hardware, add a matching entry to `config/hardware.toml`; the generator
requires exact RAM and disk matches.

## Known architectural limits

- **QXL is an emulated GPU.** The guest reports
  `Red Hat, Inc. QXL paravirtual graphic card [1b36:0100]`. The patch does not
  hide that ID, and the stock QXL driver binds in Linux. Windows falls back to
  the Microsoft Basic Display Adapter. GPU passthrough is the only strong fix
  and is out of scope.
- **q35 is an Intel chipset.** On an AMD host the CPU identity is AMD but the
  chipset PCI IDs are Intel. Most software does not compare them; deep checks
  can.
- **TPM state is cloned.** The win11 base's swtpm state is copied per session.
  Running two clones at once duplicates the same TPM identity. Wipe after each
  run, or re-create the base for each high-assurance run.
- **Disk capacity is decimal 500 GB.** The guest sees 465.7 GiB, like a real
  500 GB SSD. The model strings use marketing capacity; a check that compares
  the model's rated capacity to the reported size can still notice unusual
  choices.
- **Windows computer name and machine SID are not per-session.** The generated
  hostname is used for the QEMU process name, DHCP, and the Linux guest's
  systemd hostname. It does not rename Windows. Windows clones inherit the
  base's computer name and SID; sysprep the base or add a first-boot rename
  task if you need fresh values per run.
- **Timing is not hidden.** RDTSC/TSC and instruction timing can reveal KVM.
  See `HARDENING.md` for the optional host-kernel RDTSC patch.
- **CPU cache and feature details come from the host.** `-cpu host` preserves
  the host's feature flags and cache topology; only family/model/stepping and
  the brand string are overridden. A cache-size check against the advertised
  CPU model can notice.
- **The slirp gateway is a host alias.** The guest can reach host loopback
  services at `192.168.1.1`. See `NETWORK.md`.
- **Memory is not guaranteed zeroed** after QEMU exits. Full-disk encryption and
  a reboot between high-risk runs are the practical mitigations.
- **Base paths are absolute.** Moving the project after sealing requires fixing
  the base JSON or recreating the base.

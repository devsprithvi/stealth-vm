# Checkpoint: stealth-vm (3 Nov 2026)

Paste this whole file into a new chat to continue. Everything described here is
committed (see `git log`).

## 0. Rules for the next chat (read first)

- **Host memory is tight: 14 GiB total, about 9 GiB free when idle.** A previous
  chat booted an 8 GiB guest and read dozens of full-size screenshots; the host
  ran out of memory and Kiro was killed. Therefore:
  - Ask before booting any guest. Run only one at a time and check `free -m` first.
  - For tests, use a smaller guest (`memory_mb = 4096` has a matching DIMM in
    `config/hardware.toml`) and 4 vCPUs, then restore the profile.
  - Do not read full-size screenshots. Crop and shrink them first, or better,
    have the probe write a text report to a small writable FAT disk image that
    the host reads with `mcopy` (test-only channel, not for real analysis).
  - Always stop QEMU and run `svm destroy <id> --force` afterwards.
- Do not start a guest to answer a question that code reading can answer.

## 1. Project and purpose

`~/stealth-vm` (`svm`) runs Windows 10 and Ubuntu 24.04 desktops in QEMU/KVM so
they look like a real ASUS/AMD desktop. The goal is a **disposable malware-analysis
sandbox**: evasive samples check for VMs and change behaviour, so the guest must
pass those checks. "Game-level stealth" was the user's metaphor for the highest
achievable stealth, not a request to bypass online-game anti-cheat. The README
states it is for authorized analysis and testing only.

Stack: QEMU 10.2.2 built under `runtime/qemu` with the upstream patch
`zhaodice/qemu-anti-detection` (pinned) plus local patches `patches/0001..0004`,
OVMF, SPICE+QXL, user-mode NAT, Cloudflare WARP inside the guest, Python CLI
(`bin/svm`, typer). Sessions are qcow2 overlays on sealed read-only bases in
`images/base/` (`win10`, `linux`). Host: Ubuntu, Ryzen 5 4500 (12 threads), one
RTX 3060 used for the host display, so no GPU passthrough.

## 2. What was done this chat

**Windows stealth root cause (fixed).** `src/svm/smbios_tables.py` wrote the
SMBIOS type 16 table with one trailing NUL instead of two. Windows lost the BIOS,
system and memory tables and reported `ALASKA | A_M_I___`. Now it reports
ASUSTeK PRIME B550-PLUS, AMI 3405 and a DDR4 DIMM. A guard now rejects a
malformed string-set. Also fixed the type 4 Processor ID dword order.

**QEMU patch `patches/0004-malware-analysis-hardening.patch`** (rebuilt and
verified): blocks guest access to host `127.0.0.1` via the slirp gateway
(`disable_host_loopback`), stops emitting the hypervisor-only WAET ACPI table,
and uses the configured model as the ATAPI product (`ASUS DRW-24D5MT`).

**Other fixes**
- `config/hardware.toml`: four MAC OUIs were mislabeled (a Dell OUI on an Intel
  NIC). All OUIs now verified against `/usr/share/ieee-data/oui.txt`.
- `identity.py`: disk serials are 15 characters; comment on the MAC tail fixed.
- `qemu.py`: `host-cache-info=on` (guest L3 was a fake 16 MiB, now the real 8 MiB);
  optical drive model; new `build_is_current()` used by `svm doctor`.
- `cli.py`: `svm run --cdrom ISO` attaches a read-only disc; installer key-press
  moved into a helper; module docstring.
- `paste.py`: rewritten to send explicit key down/up events (the old `send-key`
  approach garbled text with stuck Shift). Verified on Windows.
- `scripts/build-qemu.sh`: applies every `patches/*.patch` in order with marker
  files, incremental, skips only when `BUILD_INFO` hash matches.
- `scripts/configure-linux-base.sh`: also purges update-manager and release
  upgrader packages (takes effect only when the base is re-provisioned).
- `config/profiles/linux.toml`: 12 vCPUs so the host-matched Ryzen 5 4500 identity
  (the only one whose caches match the host) is used. `win10` was already 12.
- Probes: `guest/stealth-probe.ps1` and `guest/stealth-probe.sh` (read-only,
  print `[TELL]` lines, optional host-loopback test).
- Docs: README rewritten for malware analysis; new `docs/ANTI-DETECTION.md`
  (what the upstream patch does and does not do, what we add, measured results,
  residual gaps); stale claims fixed in BUILD, HARDENING, NETWORK, ARCHITECTURE,
  `guest/README.md`.

**Measured result (3 Nov 2026).** Windows 10 and Ubuntu 24.04 each show exactly
2 tells, both the QXL GPU (PCI `1b36:0100`, "Microsoft Basic Display Adapter" or
the `qxl` module). Identity, CPU, caches, memory, disk, NIC, optical drive are
consistent. No hypervisor bit, `systeminfo` message or `systemd-detect-virt`
result. Host loopback is unreachable. WARP connects. Timing was not tested.

## 3. Fresh-machine work (done 3 Nov 2026, evening)

The user's complaint: a new session came back with old software, and possibly the
same machine GUID. Causes found and fixed (details in `docs/FRESHNESS.md`):

1. **Launcher resumed an old session** and `linux` was persistent. Now
   `bin/svm-gui` always runs `svm run --fresh --ephemeral`; `--keep` and
   `--resume` are explicit. `linux` is disposable. Desktop entry has actions for
   Windows, keep, resume (`scripts/install-launcher.sh` was re-run).
2. **The Windows base was dirty** (WARP installed and registered, an IDE, Edge data,
   downloads, hiberfil, install-time SID/MachineGuid). Rebuilt from the ISO with
   `scripts/build-win10-base.sh`: unattended, Audit mode as built-in Administrator
   (no user ever signs in), `sysprep /generalize /oobe /shutdown`, sealed. The
   old bases and backups were deleted on request; a new build backs up the
   previous base into `images/base/backup/` automatically.
3. **Per-session Windows identity.** Sources in `guest/windows/`:
   `autounattend.xml` (build), `build-base.ps1`, `session-unattend.xml`
   (specialize + OOBE every boot), `first-logon.ps1` (replaces SQM id and
   HwProfileGuid, re-creates the NIC, removes orphan interface keys, IPv6 off,
   installs WARP from the staged MSI, registers and connects, then deletes
   itself).
4. **Disk IDs.** `src/svm/diskids.py` rewrites the NTFS volume serial and the ESP
   FAT ID in the overlay before first boot, with no root (qemu-nbd on a Unix
   socket and a small NBD client). GPT GUIDs and ext4 UUIDs are not changed
   (BCD and fstab/GRUB depend on them).
5. `svm new|run|base create` accept `--vcpus` and `--memory-mb` (stored in
   `session.json`), used for 4 GiB test guests on this 14 GiB host.
6. `scripts/freshness.py <idA> <idB>` compares two stopped sessions offline
   (needs sudo and `pip install python-registry`; Windows and Linux).

**Measured:** two Windows sessions differ in NTFS serial, MachineGuid, SID,
computer name, product id, SQM id, HwProfileGuid, NIC GUID and WARP registration.
Two Linux sessions differ in machine-id, hostname and WARP registration. A Windows
session takes about 6 minutes from power-on to a connected desktop.

**Still shared per base (documented):** GPT GUIDs, Windows InstallDate and file
times, ext4 UUID, ESP serial on Linux, the hidden Kernel Debugger adapter GUID.
Rebuild the base to change them.

**Pitfalls learned**
- Do not delete a scheduled task from inside the task: it kills the script. The
  sysprep step now starts through `Win32_Process.Create`.
- Audit mode opens its own Sysprep dialog; kill it before running sysprep.
- Sysprep leaves the SQM `MachineId` and `HwProfileGuid` in place.
- A power cut in the middle of a build leaves a half-written base that can hang
  sysprep. Delete it and rebuild instead of repairing.
- `control_bash_process start` reuses an existing process with the same command;
  vary the command string (a trailing comment) to launch a second one.
- The build script refuses to start while any QEMU guest is still shutting down.

## 4. Known remaining gaps (see `docs/ANTI-DETECTION.md`)

1. Timing (`RDTSC`/`CPUID` latency) reveals KVM; not addressed.
2. QXL GPU `1b36:0100` and Basic Display Adapter; fix needs a second GPU or an ID
   spoof plus driver work (Linux `qxl` then no longer binds).
3. OVMF reports `Ubuntu distribution of EDK II` (Windows registry
   `SystemBiosVersion`); needs a custom OVMF build.
4. Intel Q35/ICH9 chipset under an AMD identity.
5. Duplicate BGRT ACPI table (upstream stub plus firmware's own).
6. Guest can reach the host's LAN through NAT; block RFC 1918 on the host
   (documented, untested).
7. No user-activity artifacts in a fresh install; WARP adapter visible.
8. Linux base is GNOME with an Ubuntu upgrade prompt, not XFCE; re-provision with
   `sudo ./scripts/provision-linux-base.sh` (user must do this, it changes the base).

## 5. State of the workspace

- No QEMU or test processes running; `vms/` is empty. Bases `win10` (generalized,
  sealed) and `linux` (empty keyring removed) are sealed read-only.
- Everything is committed. Removed as obsolete: `images/base/backup/`,
  `images/iso/win10-unattend*`, `guest/warp-setup.ps1`, `guest/gh-setup.ps1`.
- Dev helpers live outside the repo in `~/.cache/svm-dev/` (screenshot, key
  sender, registry venv) and will be lost if that directory is removed.
- `svm doctor` reports no failures.

## 6. Suggested next steps

1. Re-provision the Linux base (`sudo ./scripts/provision-linux-base.sh`) to get the
   XFCE desktop without the Ubuntu upgrade prompt (changes the base; user decides).
2. Optional: run the stealth probes on a fresh Windows session after the rebuild
   (needs a 4 GiB guest; see section 0).

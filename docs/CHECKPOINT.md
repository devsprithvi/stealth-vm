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
**Caveat (see section 7):** this only covers what our own probe looks at. A real
product (an Electron app) flagged both guests, so the claim "only 2 tells" is not
a measure of what real software sees.

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

- At the time of writing a Linux session started from the desktop launcher
  (12 vCPU, 8 GiB, disposable) was running and the host had only ~1.4 GiB free.
  Check `./bin/svm list` and `free -m` first; close it before booting anything.
  Disposable sessions are wiped when the viewer closes, including anything the
  user installed for testing. Bases `win10` (generalized, sealed) and `linux`
  (empty keyring removed) are sealed read-only.
- Everything is committed. Removed as obsolete: `images/base/backup/`,
  `images/iso/win10-unattend*`, `guest/warp-setup.ps1`, `guest/gh-setup.ps1`.
- Dev helpers live outside the repo in `~/.cache/svm-dev/` (screenshot, key
  sender, registry venv) and will be lost if that directory is removed.
- `svm doctor` reports no failures.

## 6. Suggested next steps

Do section 7 first. The Linux base re-provision (XFCE, no upgrade prompt:
`sudo ./scripts/provision-linux-base.sh`) is optional and changes the base.

## 7. Reality check: first test against real software (user report, 3 Nov 2026)

**What happened.** The user installed Qoder (an Electron AI IDE) in the Windows
guest and the Linux guest to see whether it treats the account/device as new.
Their report: it behaved badly and appeared to detect the machine. Their words,
paraphrased: the guests behave like a real VM, but not like a unique one; a
simple Electron app already defeats us, and they want everything tested, not
assumed. **Unknown:** the exact symptom (error text, blocked sign-in, trial
refused, forced verification?), which account and sign-in method were used, and
whether it was flagged on both guests or only one. Ask for these first.

**Honest assessment.** The identity layer (SMBIOS, CPU, MAC, serials, SID,
MachineGuid, volume serial) is solid and verified, but nothing so far was tested
against a real product, and the probe only checks what we thought to check.
"Different MachineGuid" is not the same as "looks like a different device".

**Hypotheses, most likely first. Each has a cheap test that separates it from the
others; do not fix anything before running the tests.**

1. **Network reputation, not VM detection.** Every session exits through
   Cloudflare WARP. WARP egress IPs are shared and commonly scored as VPN/abuse by
   account and free-trial systems. Also the guest timezone is fixed (IST,
   en-US) and will not match the WARP exit country. *Test:* repeat the Qoder
   sign-in with WARP disconnected (guest NAT, host IP), and with a phone hotspot.
   If behaviour changes, it is network, not hardware.
2. **Stable device fingerprint across sessions.** Electron/Chromium exposes
   canvas, WebGL vendor/renderer, fonts, screen size, `hardwareConcurrency`,
   `deviceMemory`, audio, locale. These come from the software stack and are
   identical in every session (same Windows build, no real GPU, same default
   1280x800 Basic Display resolution, same fonts), so a service can cluster all
   our sessions as one device even though MachineGuid differs. *Test:* open a
   fingerprint page in two fresh sessions and compare the hashes.
3. **Graphics tell.** WebGL/GPU in the guest is "Microsoft Basic Render
   Driver" or SwiftShader; WMI `Win32_VideoController` shows PCI `VEN_1B36`
   (Red Hat). Almost certainly visible to any Electron app via
   `app.getGPUInfo()` or `chrome://gpu`. Fixing needs a real GPU path (the host
   has one RTX 3060 driving its display and no iGPU, so passthrough is not
   available) or an ID spoof plus a driver; both are hard. Measure first.
4. **Timing.** RDTSC/CPUID latency reveals KVM. Not addressed. Less likely for a
   plain Electron app, but common in anti-abuse SDKs.
5. **Account-side state.** If the same email/phone/GitHub/Google account was
   used before, the guest is irrelevant. Confirm a never-used account was tried.

**Cheapest high-value experiment: read what Qoder actually collects.**
`Qoder-win-x64-user.exe` is a public NSIS installer. On the host, extract it with
`7z x`, unpack `resources/app.asar` (`npx asar extract`), and grep the JS for
`machineId`, `node-machine-id`, `systeminformation`, `wmic`, `Win32_`,
`hypervisor`, `vmware`, `virtualbox`, `qemu`, `getGPUInfo`, `webgl`, `fingerprint`.
Do the same for the Linux build. This gives facts instead of guesses and needs no
guest. (The old Qoder install and logs were in the deleted dirty base.)

**Fingerprint harness to build.** One static HTML file (`guest/fingerprint.html`)
that prints a JSON of navigator, screen, timezone, WebGL vendor/renderer,
canvas/audio hashes, fonts and `performance` timing, plus a combined hash.
Deliver via `svm run --cdrom` (read-only, no shared folder), open from `file://`
in Edge/Firefox. Run it on: the real host browser (control), two Windows sessions,
two Linux sessions, each with and without WARP. Diff the outputs. The control
tells us what "real" looks like on this exact hardware.

**Likely fixes, only after the tests say which apply:** per-session screen
resolution, timezone and locale matched to the WARP exit; a plausible set of
installed fonts and apps; a different network egress option; canvas/WebGL
differences (hard in a VM without a GPU). State plainly to the user which items
cannot be solved without real hardware.

## 8. Static analysis of Qoder (done 3 Nov 2026, this chat) — do not re-fix GPU/CPUID yet

Ran the "cheapest high-value experiment" from section 7 before changing anything.
Recipe and tooling are now in `docs/APP-ANALYSIS.md` and `scripts/asar-extract.py`
(reusable for every future app the user tests — this was explicitly requested
as an ongoing practice, not a one-off). Downloaded the real installers (the
obvious `download.qoder.com/<name>.exe` guesses 404; found the real CDN prefix
`qoder-app/releases/latest/` by grepping the download page's JS chunks),
extracted `app.asar` with a dependency-free Python parser (no Node/npm needed
for that step), found the main-process bundle deliberately obfuscated beyond
normal minification (javascript-obfuscator.io string-array pattern, 57,810
pooled strings), decoded the entire string pool (cheap: no AST work, just
base64-decode every pool entry) and grepped it.

**Result: zero evidence of hardware/VM detection.** No `vmware`/`virtualbox`/
`qemu`/`kvm`/`hypervisor`/`xen`/`bochs`/`seabios`/`ovmf` anywhere in the pool.
No `webgl`/`getGPUInfo`/canvas-fingerprint terms either. What Qoder actually
does: a local SQLite `local_device_identity` table holds one `device_id`,
generated once and sent as `machineId` to its own login/auth and sync backend;
plus an `account-fingerprint-salt` used server-side. This is Qoder's own
app-data file (lives in the session overlay, not the sealed base — a
disposable session should get a fresh one same as a fresh user profile) and
its own account/device-pairing system, not OS/firmware VM fingerprinting.

**Conclusion: don't spend effort on GPU/CPUID/OVMF (gaps 1-6 in section 4 and
`docs/ANTI-DETECTION.md`) to fix the Qoder symptom.** None of that code path
exists in Qoder. Section 7's hypothesis 1 (WARP network reputation —
shared/abuse-scored egress IP, timezone mismatch) and hypothesis 5
(account-side state: same account/trial already used, or the
`account-fingerprint-salt` system flagging something at the account level,
independent of the VM) are now the leading explanations, with real code to
back them instead of a guess. Hypothesis 2 (stable Electron/Chromium
fingerprint across sessions) is still plausible but found no supporting code
in Qoder specifically — it would show up as a different `device_id` per
session in Qoder's own local sqlite db, worth checking once a guest boots.

**Next step (not yet done, needs the user's go-ahead to boot a guest and their
own action to sign in): hypothesis 1 and 5 tests from section 7** — repeat the
Qoder sign-in with WARP disconnected, and/or with a never-before-used account,
and see if the symptom changes. Still unknown and still needed from the user:
the exact symptom (error text, blocked sign-in, trial refused, forced
verification?) — ask before spending more guest time guessing at it.

**Host memory note for next chat:** a leftover disposable session from an
earlier chat was found still running at session start (8 GiB profile, 24 min
old, host down to 291 MiB free) and was destroyed with the user's permission.
Always check `free -m` and `svm list` at the start of a chat, before assuming
the host is in the state the previous checkpoint described.

**Built, not yet used against a guest:** `guest/fingerprint.html` per section
7's spec (navigator/screen/timezone/WebGL/canvas/audio/fonts/perf, combined
hash, self-contained, no network calls, works from `file://`). Deliver the
same way as the stealth probes (`svm run --cdrom`), run it on the real host
browser as a control plus each guest/session with and without WARP, diff the
`combinedHash` and the individual fields.

**Working notes for the next chat.**
- The user wants brutal honesty and wants things tested, not asserted. Report
  failures first. Do not claim stealth from our own probe alone.
- Host memory rule still applies. Disposable sessions at the profile default are
  8 GiB; with a Linux 8 GiB guest running the host fell to ~1.4 GiB free. Use
  `--vcpus 12 --memory-mb 4096` for tests that must keep the 12-thread CPU
  identity, and launch kept sessions with `--keep` when evidence must survive.
- `control_bash_process start` reuses an identical running command; vary the
  command string to start a second process.

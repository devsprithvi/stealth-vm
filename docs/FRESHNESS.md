# Fresh machine every time

Goal: a new session looks like a PC that nobody has used, and two sessions
cannot be linked by an identifier read inside the guest. This page lists what
is regenerated, what is shared and why, and how to check.

## Why earlier sessions were not fresh

1. **The launcher resumed the newest stopped session** instead of creating one,
   and the `linux` profile was persistent. You got your own last session back.
2. **The Windows base was dirty.** It had been used as a working machine:
   Cloudflare WARP installed *and registered*, an IDE, Edge profile data,
   downloads, Recent files, a hibernation file, and the install-time machine
   identity (SID, MachineGuid, computer name, volume serial). Every session
   inherited all of it.

## What now happens

| Layer | Mechanism |
|---|---|
| Launcher | `bin/svm-gui` always runs `svm run --fresh --ephemeral`. The session is deleted when the viewer closes. Keeping or resuming is explicit (`--keep`, `--resume`). |
| Hardware identity | New SMBIOS UUID, serials, MAC, disk serial, NVRAM per session. |
| Windows machine identity | The base is **sysprep-generalized** (`scripts/build-win10-base.sh`). The first boot of every overlay runs specialize + OOBE, which creates a new SID, MachineGuid, computer name, product id, SQM id, and a new `user` profile. |
| Disk identifiers | `svm` rewrites the NTFS volume serial and the EFI partition's FAT ID inside the overlay before first boot (`src/svm/diskids.py`). The base is not touched. |
| SQM id, hardware profile, NIC | Sysprep does not reset these in this build, so `first-logon.ps1` replaces the SQM `MachineId` and `HwProfileGuid`, removes and re-detects the network adapter (new interface GUID), and deletes orphaned interface keys. |
| WARP | Not installed and not registered in any base. Each session installs it from a staged MSI and registers a new free device. |
| Base content | No user profile (built in Audit mode, no sign-in), no third-party software, no browser data, no hibernation file, build logs removed. Linux: empty `/etc/machine-id`, no SSH host keys, no WARP state, no keyring. |

## What is still shared between sessions of one base

These cannot be randomized offline without risking an unbootable guest, or are
the same on every Windows 10 PC:

| Item | Why | Mitigation |
|---|---|---|
| GPT disk and partition GUIDs | The Windows BCD stores them. Changing them without rewriting the BCD breaks boot. | Rebuild the base (new GUIDs) when you want a new set. |
| Windows `InstallDate` and file timestamps | Set when the base was built. | Same: rebuild periodically; keep two or three bases. |
| ext4 UUIDs and GPT GUIDs on Linux | `fstab` and GRUB use them by value. | Same. |
| Installed OS build, patch level, inbox apps | It is the same image. | Normal for any PC of that build. |
| Host-derived facts: CPU family and cache sizes, public IP seen by Cloudflare | They describe the real host. | Cannot be hidden from the guest. |
| Hidden "Ethernet (Kernel Debugger)" adapter GUID, TPM-less device key name, WAP-push registration id | Windows creates them once per install. They are not visible to ordinary programs. | Rebuild the base. |
| WARP's Wintun adapter GUID | Cloudflare uses a fixed GUID for its virtual adapter. | Same on every machine with WARP; not an identifier. |

Anything a *session* writes (files, browser data, registry changes) lives only
in its overlay and disappears with it.

## Verifying

`scripts/freshness.py` reads two stopped sessions offline and prints which
identifiers differ. It needs `sudo` and `pip install python-registry`.

```bash
./bin/svm run --fresh --profile win10 --keep --vcpus 4 --memory-mb 4096   # A: wait for the desktop, close
./bin/svm run --fresh --profile win10 --keep --vcpus 4 --memory-mb 4096   # B: same
scripts/freshness.py <idA> <idB>
./bin/svm destroy <idA> --force; ./bin/svm destroy <idB> --force
```

`--vcpus 4 --memory-mb 4096` is for a tight host (14 GiB). Use the profile
defaults when memory allows. Always run one guest at a time.

Expected: every row `DIFFERENT` except `users` (same account name). For a Linux
session the table shows machine id, hostname and WARP registration `DIFFERENT`;
the root filesystem UUID and ESP ID are shared (see above).

## Measured (3 Nov 2026)

Two sessions of the rebuilt Windows base, compared offline:

| Identifier | Result |
|---|---|
| NTFS volume serial, MachineGuid, machine SID, computer name, product id | different |
| SQM machine id, hardware profile GUID, NIC interface GUID | different |
| WARP device and account id | different |
| `Users\` listing | same names (`user`), different profiles |

Two Linux sessions: machine-id, hostname, WARP registration different. The
`first-logon.ps1` script removed itself, its log and the MSI in every Windows
session, and no build log (`build-base.log`) remained in the base.

A Windows session needs about 6 minutes from power-on to a connected desktop.

## Rebuilding a base

`./scripts/build-win10-base.sh` (30-40 minutes, unattended). The previous base
is moved to `images/base/backup/`. Run it again whenever you want a base with
new install-time values (GPT GUIDs, install date). Linux:
`sudo ./scripts/provision-linux-base.sh`.

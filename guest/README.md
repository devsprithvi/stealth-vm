# Guest setup

The bases are shared, read-only templates. Nothing is registered or logged in
inside them, so every session starts as a machine nobody has used. Anything a
session needs to identify itself to an online service (WARP) is created at that
session's first boot and thrown away with the session.

## Windows base

One command builds it, unattended, from the installer ISO. No click is needed
and no user ever signs in:

```bash
./scripts/build-win10-base.sh            # about 30-40 minutes, 4 GiB guest
```

What it does (sources in `guest/windows/`):

1. Packs `autounattend.xml`, `build-base.ps1`, `first-logon.ps1`,
   `session-unattend.xml` and the Cloudflare WARP MSI into `images/iso/win10-build.iso`.
2. Moves the current base into `images/base/backup/` (never deletes it).
3. `svm base create win10` installs Windows 10 Pro, reseals into **Audit mode**
   as the built-in Administrator (no user profile is ever created), runs
   `build-base.ps1`, which stages the per-session files, turns hibernation off
   and clears logs, then runs `sysprep /generalize /oobe /shutdown`.
4. The finished base is mounted read-only-style and checked: sysprep succeeded,
   no `hiberfil.sys`, no Cloudflare data, no `Users\user`. Build logs are removed.

Each session then boots an overlay of that generalized image, so Windows runs
specialize + OOBE with `session-unattend.xml`: new SID, MachineGuid, computer
name, SQM and telemetry ids, and a new `user` profile. `first-logon.ps1`
replaces the hardware-profile GUID and re-creates the network adapter (new
interface GUID), turns IPv6 off, installs WARP from the staged MSI, registers a
new free WARP device and connects. It then deletes the MSI and itself.

First boot of a session takes about 6 minutes (specialize, OOBE, WARP). The
network adapter is removed and re-detected during that time, so it is normal
for the connection to drop briefly.

Do **not** boot the base to "finish setup", install software, or register WARP
in it. Anything done there is shared by every future session. To change what
the base contains, edit `guest/windows/` and rebuild.

The QXL card keeps its Red Hat ID (`1b36:0100`) and no QXL driver is installed,
so Windows uses the Microsoft Basic Display Adapter at 1280x800. This is the
main remaining hardware tell; see `docs/ANTI-DETECTION.md`.

## Linux base

### Automated, hands-off (recommended)

Build the base from the **official Ubuntu Server ISO** plus an unattended
(NoCloud/autoinstall) seed, so there is nothing to click:

```bash
# 1. Official ISO (verified by SHA-256 from releases.ubuntu.com)
curl -fL -o images/iso/ubuntu-24.04.5-live-server-amd64.iso \
  https://releases.ubuntu.com/24.04/ubuntu-24.04.5-live-server-amd64.iso
echo "97f3d7ffb032c3eb3b23d2c8be9cc76e60c2c1f2c0146ba5ba9fe01cafae0fd8  images/iso/ubuntu-24.04.5-live-server-amd64.iso" | sha256sum -c -

# 2. The seed: images/iso/user-data + images/iso/meta-data
#    (autoinstall: DHCP, whole-disk layout, user/password, installs the
#     Cloudflare WARP client, disables IPv6, and powers off when done)
cloud-localds images/iso/seed.iso images/iso/user-data images/iso/meta-data

# 3. Build the base. QEMU boots the installer, installs, then exits.
./bin/svm base create linux \
  --iso images/iso/ubuntu-24.04.5-live-server-amd64.iso \
  --driver-iso images/iso/seed.iso \
  --profile linux --headless --yes
```

The seed powers the guest off when installation finishes, so `--yes` seals the
base automatically. The guest login credentials are in
`images/iso/.linux-base-password` (user `user`, hostname `ubuntu`) — change them
in the seed before sealing a base you plan to keep.

### Manual (interactive)

```bash
./bin/svm base create linux --iso images/iso/<any-linux>.iso --profile linux
```

Then inside the guest run `sudo ./warp-setup.sh`, connect WARP, enable
Lockdown/Always On, disable IPv6, and shut the guest down cleanly so `svm` can
seal the base.

## Stealth probes

`stealth-probe.ps1` (Windows) and `stealth-probe.sh` (Linux) report what code
inside the guest can learn about the VM and mark each giveaway `[TELL]`. They
change nothing. Deliver them on a read-only CD so no folder is shared:

```bash
xorriso -as mkisofs -V PROBE -J -r -o /tmp/probe.iso guest/
./bin/svm run --fresh --profile win10 --keep --cdrom /tmp/probe.iso
```

- Windows: `powershell -ep bypass -f D:\stealth-probe.ps1` (the CD can be `D:`
  or `E:`; list drives with `gdr`). Use `svm paste` to type the command.
- Linux: `sudo mount /dev/sr0 /mnt && sudo bash /mnt/stealth-probe.sh`.
- Isolation: run `python3 -m http.server 9999 --bind 127.0.0.1` on the host and
  pass `-HostPort 9999` / `--host-port 9999`. The port must be unreachable.

Run a probe after every QEMU rebuild, base rebuild, or change to
`config/hardware.toml`. Interpreting the output is covered in
`docs/ANTI-DETECTION.md`.

## Verify WARP

Inside the guest:

```bash
curl https://www.cloudflare.com/cdn-cgi/trace
```

Look for `warp=on` and a `ip=` that is a Cloudflare address, not your home IP.

## Notes

- **WARP is registered per session, never in a base.** Windows does it in
  `first-logon.ps1`, Linux in `warp-auto-connect`. Two sessions therefore have
  two different Cloudflare device registrations and can run side by side.
- **WARP is not anonymity.** Cloudflare sees your host's public IP and the
  device registration. It hides your IP from the destinations you visit, not
  from Cloudflare.
- **Some malware checks for VPNs.** WARP's virtual adapter and routes are
  visible inside the guest. If the target is specifically anti-VPN, use a
  network-level VPN instead (router or host), or a Tor gateway VM.
- **No shared folders or clipboard.** Do not add them for convenience; they
  are the escape routes that matter more than any CPUID bit.

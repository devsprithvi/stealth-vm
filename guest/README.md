# Guest setup

Everything here is installed **inside the base image**, once. Sessions clone the
base, so they inherit the WARP client and its registration.

## Windows base

1. Boot the base installer:

   ```bash
   ./bin/svm base create win10 --iso images/iso/Win10_22H2_English_x64.iso
   ```

2. Install Windows normally. The disk is AHCI and the NIC is an Intel e1000e,
   both of which Windows 10 has inbox drivers for. You do not need virtio
   drivers to install or to get networking.

3. Inside Windows, open PowerShell as Administrator and run:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass -Force
   .\warp-setup.ps1
   ```

   Or install Cloudflare WARP manually from
   <https://1.1.1.1/> and skip the script.

4. Open WARP, log in, connect, then enable **Lockdown mode** (also called
   **Always On** in some versions). Lockdown makes the client fail closed: if
   WARP drops, the guest has no direct internet.

5. Disable IPv6 on the guest NIC so a WARP failure cannot leak over IPv6:

   ```powershell
   Get-NetAdapter | Disable-NetAdapterBinding -ComponentID ms_tcpip6
   ```

6. Display driver: the anti-detection patch rewrites the QXL PCI vendor ID
   from Red Hat (`1b36`) to Intel (`8086`), so the stock Red Hat QXL WDDM
   driver will not bind. Windows falls back to the Microsoft Basic Display
   Adapter, which is usable but limited to a low resolution. The `virtio-win`
   ISO is for virtio devices and does not contain a QXL driver. If you need
   higher resolution, use a custom QXL INF that matches the patched IDs, or
   accept the basic driver. This is a deliberate stealth/compatibility
   tradeoff, not a driver-installation mistake.

7. Shut Windows down from the Start menu. QEMU exits, and `svm base create`
   asks **"Installation in 'win10' finished. Seal this base?"** Answer yes.
   Do not close the SPICE window to "finish" the install; that also stops QEMU,
   but a clean guest shutdown is better for the filesystem. If you pass
   `--yes`, the base is sealed as soon as QEMU exits, so only use it after you
   have verified the install and WARP.

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

## Verify WARP

Inside the guest:

```bash
curl https://www.cloudflare.com/cdn-cgi/trace
```

Look for `warp=on` and a `ip=` that is a Cloudflare address, not your home IP.

## Notes

- **Register WARP once, in the base.** Clones inherit the registration. Run one
  stealth VM at a time; two clones sharing one WARP device registration can
  confuse the client and Cloudflare.
- **WARP is not anonymity.** Cloudflare sees your host's public IP and the
  device registration. It hides your IP from the destinations you visit, not
  from Cloudflare.
- **Some malware checks for VPNs.** WARP's virtual adapter and routes are
  visible inside the guest. If the target is specifically anti-VPN, use a
  network-level VPN instead (router or host), or a Tor gateway VM.
- **No shared folders or clipboard.** Do not add them for convenience; they
  are the escape routes that matter more than any CPUID bit.

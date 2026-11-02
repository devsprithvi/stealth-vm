# Hardening

This project runs code that may try to escape, persist, or fingerprint the VM.
The guest is expected to be compromised. The host must be treated as the thing
you are protecting.

## Threat model

| Threat | Mitigation | Residual risk |
|---|---|---|
| Guest escape via QEMU/KVM bug | Unprivileged QEMU, seccomp, minimal device model, updates | A kernel/QEMU 0-day can still escape |
| Guest reaches host services | No port forwards; slirp gateway caveat documented | Slirp maps `192.168.1.1` to host loopback |
| Guest reads host files | No shared folders, no USB host devices, no guest agent | QEMU bug or user-added share |
| Guest exfiltrates host data | No host credentials in guest; WARP; isolated network | Guest can use any network path you allow |
| Guest detects the VM | Patched QEMU, self-consistent SMBIOS/CPU/disk/MAC | QXL GPU, q35 Intel chipset, timing, TPM |
| Malicious patch/build | Pinned hashes, build as user, review patch | Third-party patch is trusted code |
| Host compromise via guest | Per-session overlay; destroy or `--ephemeral` wipes | Kernel/QEMU escape, side channels |
| Data remanence | Encrypted disk/swap, core dumps off, reboot between runs | RAM is not guaranteed zeroed |

## Host preparation

Use a dedicated Linux host if possible. At minimum:

- Keep the host kernel, QEMU dependencies, and firmware updated. Do not disable
  CPU mitigations for VM performance.
- Use full-disk encryption (LUKS) and encrypted swap. Guest RAM can be swapped
  or hibernated to host disk.
- Disable host hibernation while testing:
  ```bash
  sudo systemctl mask hibernate.target hybrid-sleep.target
  ```
- Disable KSM (kernel same-page merging). KSM can share pages between guests and
  create side channels:
  ```bash
  echo 0 | sudo tee /sys/kernel/mm/ksm/run
  ```
  Persist it if your distro enables KSM by default.
- Disable core dumps for QEMU. A core file can contain guest RAM:
  ```bash
  ulimit -c 0
  ```
  For a systemd scope, set `LimitCORE=0`.
- Do not run `svm` as root. The CLI warns if `geteuid() == 0`.
- Keep `/dev/kvm` accessible only to the `kvm` group:
  ```bash
  ls -l /dev/kvm        # crw-rw---- root kvm
  groups                # your user should include kvm
  ```
- Default-deny inbound host firewall. `svm` creates no inbound path, but the
  host may have other services:
  ```bash
  sudo ufw default deny incoming
  sudo ufw enable
  ```
- Disable nested virtualization unless you need it. Nested VMX/SVM increases
  the attack surface:
  ```bash
  # /etc/modprobe.d/kvm.conf
  options kvm_amd nested=0
  # or: options kvm_intel nested=0
  sudo update-initramfs -u
  ```
  Verify after reboot: `cat /sys/module/kvm_amd/parameters/nested` (or
  `kvm_intel`).
- Enable IOMMU if your firmware supports it, even without passthrough. It is not
  a substitute for a patched kernel, but it is good hygiene.
- Do not install libvirt, a bridge, or a host DHCP server for this project. The
  design deliberately avoids them.

## Dedicated user (recommended)

Running QEMU as your desktop user means a QEMU escape lands in your home
directory. A dedicated user limits the blast radius:

```bash
sudo useradd --create-home --shell /bin/bash svmtest
sudo usermod -aG kvm svmtest
sudo mkdir -p /srv/stealth-vm
sudo cp -a /home/karna/stealth-vm/. /srv/stealth-vm/
sudo chown -R svmtest:svmtest /srv/stealth-vm
sudo -u svmtest /srv/stealth-vm/bin/svm doctor
```

The dedicated user needs:

- read/write access to the project tree, `images/`, `vms/`, `runtime/`, `logs/`;
- read access to ISOs and OVMF firmware;
- read/write access to `/dev/kvm`;
- a way to display the SPICE viewer. Run QEMU and the viewer as the same user
  (grant that user access to your X/Wayland session), or use `--headless` and
  connect as that user. The session directory is `0700`, so a different desktop
  user cannot open the socket.

`bin/svm` uses `uv`; make sure `uv` is on the dedicated user's `PATH` (install
it for that user, or use an absolute path).

Do not give the dedicated user sudo. Do not put SSH keys, browser profiles, or
personal files in its home. If you use `--headless`, the SPICE socket lives in a
`0700` session directory; only that user can connect.

## Per-run isolation

`svm` already does the following:

- QEMU runs as your user, not root.
- `-sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny`
  unless `--no-sandbox` is passed.
- No shared folders (`-virtfs`), no clipboard (`disable-copy-paste=on`), no
  guest agent, no file transfer (`disable-agent-file-xfer=on`), no USB host
  redirection, no serial/parallel ports, no monitor socket.
- SPICE and QMP are Unix sockets inside a `0700` session directory. There is no
  TCP listener.
- The base disk is `0444`; the session uses a qcow2 overlay.
- NVRAM is per session and `0600`.
- TPM state, when used, is per session.
- The Linux profile keeps the session directory across stop/start; `svm destroy`
  or `--ephemeral` deletes it.

Do not add any of the following for convenience:

- `-virtfs` / shared folders / 9p / virtiofs.
- SPICE clipboard or USB redirection.
- `-device usb-host` or a physical USB passthrough.
- `qemu-guest-agent` or SPICE agent file transfer.
- `hostfwd` port forwards.
- A bridge or TAP to your LAN.
- A TCP SPICE or QMP listener.
- Your real Microsoft/Google account, SSH keys, browser profile, or password
  manager inside the guest.

## QEMU seccomp

The sandbox flags mean:

| Flag | Effect |
|---|---|
| `obsolete=deny` | Deny obsolete syscalls |
| `elevateprivileges=deny` | Deny setuid/setgid and capability changes |
| `spawn=deny` | Deny fork/exec of helper processes |
| `resourcecontrol=deny` | Deny setrlimit/nice/priority changes |

`spawn=deny` is why `svm` does not use the passt backend: QEMU would need to
spawn the `passt` process. If you add passt, either drop `spawn=deny` or run
passt outside QEMU. `--no-sandbox` exists for debugging only; it weakens the
isolation and should not be used for real tests.

QEMU seccomp is a syscall filter, not a complete sandbox. A QEMU escape that
stays within allowed syscalls is still possible.

## Resource limits

A runaway guest can exhaust host RAM, CPU, or process slots. Use a cgroup scope:

```bash
systemd-run --user --scope \
  -p MemoryMax=12G \
  -p MemorySwapMax=0 \
  -p CPUQuota=1000% \
  -p TasksMax=4096 \
  -p LimitCORE=0 \
  -- ./bin/svm run --fresh --profile linux
```

Adjust to your host. `MemoryMax` should leave headroom for the host; the default
Linux profile uses 4 GiB and the host has ~15 GiB. Do not set `MemoryMax` below
the guest's RAM plus QEMU overhead, or the guest will be OOM-killed.

If user-scope delegation is unavailable on your distro, use a system scope or a
small wrapper service. Do not run QEMU as root just to get cgroups.

## Network hardening

See `NETWORK.md` for the full model. The key points:

- There are no host port forwards and no inbound path to the guest.
- The slirp gateway `192.168.1.1` is a host alias. A guest can reach host
  loopback services. Stop sensitive local services while a guest runs, or use a
  network namespace / passt with `map-host-loopback=none`.
- WARP runs in the guest, so the host cannot enforce the kill switch. Enable
  Lockdown/Always On in the WARP client.
- A host-level VPN covers the pre-WARP boot window and is stronger than relying
  on the guest client alone.
- Do not bridge the guest to your LAN. If you need LAN access for a test, use a
  separate isolated network and accept the exposure.

## Patch supply chain

The anti-detection patch is third-party code that runs inside QEMU, the
hypervisor trusted to contain the guest. Treat it accordingly:

- `scripts/build-qemu.sh` pins the QEMU tarball SHA-256 and the patch SHA-256.
  If upstream changes the patch, the build fails rather than silently using new
  code.
- The patch is applied with `patch -p1 --dry-run` first, then for real. The
  build records `BUILD_INFO` under `runtime/qemu/`.
- Build as your desktop user, not root. `bootstrap.sh` uses `runuser` for this.
- `runtime/qemu` is not setuid and is not on `PATH` ahead of system binaries.
- Review the patch before updating the pin. It changes SMBIOS defaults, ACPI
  tables, PCI vendor/device IDs, and device strings. In particular, it replaces
  Red Hat PCI vendor IDs with Intel IDs and hardcodes ASUS strings.
- Verify the marker after a build:
  ```bash
  grep -aq ASUS runtime/qemu/bin/qemu-system-x86_64 && echo patched
  ./bin/svm version
  ```
- If you do not trust the patch, run unpatched QEMU. You lose most of the
  stealth layer but keep the disposable-VM isolation.

## RDTSC / KVM timing patch (optional, advanced)

The QEMU patch does not fix timing checks. `WCharacter/RDTSC-KVM-Handler`
(289★) is a host-kernel KVM patch that intercepts RDTSC exits and adds
per-VM noise/offsets. It is **not** part of the default build.

Why it is not default:

- It requires rebuilding or patching the host kernel (or building an out-of-tree
  module against your exact kernel).
- It can break timekeeping, suspend/resume, performance, and other VMs.
- Secure Boot must be disabled or the module signed.
- It does not defeat every timing check; sophisticated software uses multiple
  clocks and statistical tests.
- A kernel patch is a much larger trusted-code change than the QEMU patch.

If you need it, use a dedicated test host, read the patch, pin the commit, and
test with and without it. Do not apply it to your daily driver. A simpler
mitigation is to avoid software that specifically checks TSC timing; no
user-space QEMU setting reliably hides KVM timing.

## TPM and Secure Boot

The `win11` profile enables TPM 2.0 (`swtpm`) and Secure Boot OVMF. Both are
emulated and detectable:

- `swtpm` is a software TPM. Its manufacturer, firmware version, and CRB
  interface differ from a physical discrete/firmware TPM.
- Secure Boot OVMF is a virtual firmware. Measured boot and attestation can
  distinguish it.
- The base TPM state is copied per session. Two clones share the same TPM
  identity; run one at a time, and wipe after each run.
- Do not use this profile for software that performs remote attestation or
  seals secrets to a physical TPM.

Prefer `win10` (no TPM, no Secure Boot) unless the target requires Windows 11.

## Guest hygiene

Treat the guest as disposable even when its overlay persists; host hygiene is
what matters. Inside the guest:

- Use a local account, not a personal Microsoft/Google account.
- Do not sign into password managers, email, SSH, or corporate services.
- Do not mount host folders or enable clipboard.
- Treat any file you copy in as untrusted; use a read-only ISO or a network
  download, not a shared folder.
- You may disable Defender or Windows Update inside the guest if the test
  requires it. Never disable host security.
- Do not install guest additions, VMware tools, or VirtualBox additions.
- Keep the base free of personal data. The base contains the WARP client and
  MDM policy; each session registers its own device in its overlay. Treat both
  the base and session overlays as sensitive.

## Detection surface checklist

Run these inside the guest after boot. They are also useful before sealing a
base.

Windows (PowerShell):

```powershell
Get-CimInstance Win32_ComputerSystem | Format-List Manufacturer,Model,TotalPhysicalMemory
Get-CimInstance Win32_BIOS | Format-List Manufacturer,SMBIOSBIOSVersion,SerialNumber,ReleaseDate
Get-CimInstance Win32_BaseBoard | Format-List Manufacturer,Product,SerialNumber,Version
Get-CimInstance Win32_Processor | Format-List Name,Manufacturer,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed
Get-CimInstance Win32_PhysicalMemory | Format-List Manufacturer,PartNumber,Speed,Capacity,SerialNumber
Get-CimInstance Win32_DiskDrive | Format-List Model,SerialNumber,Size,FirmwareRevision
Get-NetAdapter | Format-List Name,InterfaceDescription,MacAddress,DriverVersion
Get-PnpDevice -Class Display | Format-List FriendlyName,InstanceId
systeminfo
```

Look for:

- `systeminfo` saying "A hypervisor has been detected. Features required for
  Hyper-V will not be displayed." This is a strong VM tell. The patch may not
  remove it.
- QXL display adapter, e1000e NIC, ICH9 chipset IDs.
- A CPU brand string that does not match the host's real CPU but is plausible.
- DIMM size equal to VM RAM, disk model/serial from the pool.
- MAC OUI that resolves to a real vendor, not a hypervisor range.

Linux guest:

```bash
sudo dmidecode -t 0,1,2,3,4,17
lspci -nn
ip addr
cat /proc/cpuinfo | grep -m1 'model name'
systemd-detect-virt
```

`systemd-detect-virt` should report `kvm` (it detects the hypervisor even with
the CPUID bit hidden, via other hints). That is expected; the goal is to defeat
user-mode anti-VM checks, not a determined analyst.

PCI IDs: the patch changes Red Hat vendor IDs to Intel, but the q35 chipset is
still an Intel emulation on an AMD host. Compare `lspci -nn` with the SMBIOS
CPU vendor; a mismatch is a tell for deep checks.

## Verify the host after a run

```bash
./bin/svm list                 # one stopped session, or none after destroy
pgrep -af 'qemu-system|swtpm'  # should print nothing
ss -xlp | grep -E 'spice|qmp|swtpm'  # should print nothing
find vms -maxdepth 2 -name session.json  # one entry for the stopped session
```

Check the base was not modified:

```bash
sha256sum images/base/*.qcow2
```

Run it before and after a session; the hash should not change. The overlay is
where all writes go.

A persistent session contains guest data. Delete it with `svm destroy <id>`
when done.

## Incident response

If you suspect a guest escape or host compromise:

1. Disconnect the host from the network. Do not use it to check email or reset
   passwords.
2. Power it off (or suspend to RAM if you need volatile evidence; powering off
   loses RAM).
3. Image the disk from another machine or a live USB. Do not mount it read-write.
4. Rebuild the host from trusted media. Rotate credentials from a different
   device.
5. Review QEMU/session logs if they still exist. Persistent sessions keep their
   logs until destroyed; an ephemeral session may have removed them already.
6. If the host is managed by an employer or handles regulated data, follow your
   incident process.

A guest escape is a host compromise. WARP, antivirus in the guest, or deleting
the session directory does not undo it.

## Data remanence

- qcow2 overlays are deleted, but deleted blocks may remain on the host disk.
  Full-disk encryption is the practical mitigation. For high-assurance runs,
  discard/TRIM the filesystem or wipe free space after deleting sessions.
- Guest RAM is freed by QEMU but not guaranteed zeroed. Disable host swap and
  hibernation, or use encrypted swap; reboot between high-risk runs.
- Core dumps can contain guest RAM. Keep `ulimit -c 0`.
- If you use hugepages, memory may persist between QEMU runs. Clear or reboot
  between runs.
- ISOs, base images, and `build/` are not wiped by session cleanup. Delete them
  explicitly if they contain sensitive data:
  ```bash
  ./bin/svm base delete linux --force
  rm -f images/iso/*
  ```

## What not to do

- Do not run QEMU or `svm` as root.
- Do not add shared folders, clipboard, USB passthrough, or a guest agent.
- Do not expose SPICE or QMP over TCP.
- Do not bridge the guest to your LAN.
- Do not use personal accounts, keys, or browser profiles in the guest.
- Do not disable host updates, CPU mitigations, or KSM protections for speed.
- Do not treat WARP as anonymity. Cloudflare sees your host IP.
- Do not run multiple clones of the same TPM state; each session registers WARP
  separately.
- Do not use this for online-game anti-cheat or any violation of a service's
  terms.

## Residual risks

No configuration makes a VM indistinguishable from bare metal. This project
raises the cost of casual detection and contains casual malware; it does not
stop a targeted adversary with kernel access, timing analysis, or a QEMU/KVM
0-day. The GPU (QXL), the q35 Intel chipset on an AMD host, the emulated TPM,
and the hypervisor's timing are the largest remaining tells.

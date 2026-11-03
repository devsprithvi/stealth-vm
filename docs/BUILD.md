# Build

`svm` uses a patched QEMU built from source into `runtime/qemu/`. Nothing is
installed system-wide. The distro's `qemu-system-x86_64` is only used if the
local build is missing, and `svm doctor` warns when it is unpatched.

## Pinned versions

| Item | Value |
|---|---|
| QEMU | `10.2.2` |
| QEMU tarball SHA-256 | `784b296ff29c1417aa72323abcb2d2ea9ab9771724f577dcd785c3b04f21e176` |
| Anti-detection patch | `zhaodice/qemu-anti-detection` |
| Patch commit | `2750c86d2d045243ba6617951487e41b25c05557` |
| Patch SHA-256 | `0d05f1a0ced91cef3fe33203b7d81c955d64694f30085544dc5e154809da110e` |
| Local patch | `patches/0001-smbios-desktop-chassis.patch` |
| Local patch | `patches/0002-smbios-ddr4-memory.patch` |
| Prefix | `runtime/qemu` |
| Target | `x86_64-softmmu` |

The patch URL is pinned to the commit, not `main`, so the downloaded bytes are
reproducible even if upstream changes. The SHA-256 check is still authoritative.

The anti-detection patch and the four local patches in `patches/` apply to a
clean QEMU 10.2.2 tree in filename order. The build script runs a
`patch -p1 --dry-run` before applying each one for real and tracks applied
patches with `.svm-applied-*` marker files in the source tree. Re-running the
script is incremental: it exits early only when the hash of `patches/*.patch`
recorded in `runtime/qemu/BUILD_INFO` matches, and otherwise applies the new
patches and runs `make` on what changed. `svm doctor` reports a stale build.

## Requirements

- Ubuntu/Debian-like Linux host with KVM (`/dev/kvm`).
- `sudo` for the one-time dependency install.
- ~10 GiB free disk: tarball, source, build objects, and installed prefix.
- 15-40 minutes on a 6-core/12-thread desktop; less on a faster machine.
- An Ubuntu 24.04.5 installer ISO for the Linux base and a Windows 10 ISO for
  the Windows base.

The host in this project is Ubuntu 26.04, kernel 7.0, Ryzen 5 4500, ~15 GiB
RAM. The build script caps parallelism at 8 jobs to avoid OOM; override with
`JOBS=4` or `JOBS=12` if you know your RAM budget.

## One-time bootstrap

```bash
cd ~/stealth-vm
sudo ./bootstrap.sh
```

This does two things:

1. `apt-get update` and installs the required packages.
2. Runs `scripts/build-qemu.sh` as `$SUDO_USER` (not root), so the build tree
   and `runtime/qemu` are owned by your desktop user.

Modes:

```bash
sudo ./bootstrap.sh --deps          # packages only
./bootstrap.sh --build             # build only, no root
./bootstrap.sh --build --force     # clean rebuild of QEMU
```

`svm build` is equivalent to `./bootstrap.sh --build` and is useful after a
patch or version change.

## Packages

Required (from `bootstrap.sh`):

```text
build-essential ninja-build meson python3-venv pkg-config
git curl xz-utils jq patch
libglib2.0-dev libpixman-1-dev zlib1g-dev libslirp-dev
libspice-protocol-dev libspice-server-dev
libusb-1.0-0-dev libaio-dev libcapstone-dev libnuma-dev liburing-dev
libseccomp-dev
libepoxy-dev libdrm-dev libgbm-dev
libpulse-dev libasound2-dev
libgnutls28-dev libgcrypt20-dev libtasn1-6-dev
libfdt-dev libcacard-dev libiscsi-dev libssh-dev libnfs-dev libbpf-dev
ovmf qemu-utils swtpm swtpm-tools virt-viewer spice-client-gtk
```

Optional (failures do not stop the build):

```text
cloud-image-utils genisoimage nftables passt
```

`passt` is installed but not used by `svm` yet; see `NETWORK.md`.

## Linux base provisioning

The Linux base is installed once from the Ubuntu Server ISO, then configured in
a chroot:

```bash
# Generate the passwordless NoCloud seed (no password or hash in the file).
cloud-localds images/iso/seed.iso images/iso/user-data images/iso/meta-data

./bin/svm base create linux \
  --iso images/iso/ubuntu-24.04.5-live-server-amd64.iso \
  --driver-iso images/iso/seed.iso \
  --profile linux --headless --yes

sudo ./scripts/provision-linux-base.sh
```

`provision-linux-base.sh` mounts the base, runs
`scripts/configure-linux-base.sh` in the chroot, removes provisioning residue,
runs `e2fsck`, and seals the base `0444`. It refuses to run while any session
exists, because changing a base invalidates every overlay. See `README.md` for
the first-boot verification.

## Manual build

If you prefer to run the steps yourself:

```bash
cd ~/stealth-vm
mkdir -p build/downloads build/src runtime/qemu

curl -fL --retry 3 \
  https://download.qemu.org/qemu-10.2.2.tar.xz \
  -o build/downloads/qemu-10.2.2.tar.xz
echo "784b296ff29c1417aa72323abcb2d2ea9ab9771724f577dcd785c3b04f21e176  build/downloads/qemu-10.2.2.tar.xz" \
  | sha256sum -c -

curl -fL --retry 3 \
  https://raw.githubusercontent.com/zhaodice/qemu-anti-detection/2750c86d2d045243ba6617951487e41b25c05557/qemu-10.2.2.patch \
  -o build/downloads/qemu-anti-detection-10.2.2.patch
echo "0d05f1a0ced91cef3fe33203b7d81c955d64694f30085544dc5e154809da110e  build/downloads/qemu-anti-detection-10.2.2.patch" \
  | sha256sum -c -

tar -xf build/downloads/qemu-10.2.2.tar.xz -C build/src
cd build/src/qemu-10.2.2
patch -p1 --dry-run < ../../downloads/qemu-anti-detection-10.2.2.patch
patch -p1 < ../../downloads/qemu-anti-detection-10.2.2.patch
touch .svm-patched

patch -p1 --dry-run < ../../../patches/0001-smbios-desktop-chassis.patch
patch -p1 < ../../../patches/0001-smbios-desktop-chassis.patch
touch .svm-chassis-patched

patch -p1 --dry-run < ../../../patches/0002-smbios-ddr4-memory.patch
patch -p1 < ../../../patches/0002-smbios-ddr4-memory.patch
touch .svm-memory-patched

./configure \
  --prefix="$HOME/stealth-vm/runtime/qemu" \
  --target-list=x86_64-softmmu \
  --enable-kvm \
  --enable-spice \
  --enable-slirp \
  --disable-docs \
  --disable-gtk \
  --disable-sdl \
  --disable-opengl \
  --disable-virglrenderer \
  --disable-werror \
  --audio-drv-list=pa

make -j6
make install
```

Then verify:

```bash
cd ~/stealth-vm
./bin/svm doctor
./bin/svm version
```

## Why these configure flags

| Flag | Reason |
|---|---|
| `--target-list=x86_64-softmmu` | Only x86_64 system emulation is needed |
| `--enable-kvm` | Hardware acceleration |
| `--enable-spice` | SPICE display over a Unix socket |
| `--enable-slirp` | User-mode networking without root |
| `--enable-seccomp` | QEMU seccomp sandbox (`-sandbox on,...`), required for `hardware.sandbox` |
| `--disable-docs` | Faster build; docs are online |
| `--disable-gtk`, `--disable-sdl` | No local QEMU window; SPICE only |
| `--disable-opengl`, `--disable-virglrenderer` | No GPU passthrough/3D |
| `--disable-werror` | Avoid build failures from new compiler warnings |
| `--audio-drv-list=pa` | PulseAudio backend for optional guest audio |

`--enable-spice` requires `libspice-server-dev` and
`libspice-protocol-dev`. `--enable-slirp` requires `libslirp-dev`. If either is
missing, configure will fail rather than silently build a QEMU without the
feature.

## What the patch changes

The anti-detection patch touches QEMU's SMBIOS defaults, ACPI tables, PCI
vendor/device IDs, and device strings. It replaces Red Hat PCI IDs with Intel
IDs and hardcodes ASUS strings. It is applied to the QEMU source, so the
resulting binary is self-contained; no runtime patch or LD_PRELOAD is involved.

Four local patches are applied after it (details in `ANTI-DETECTION.md`):

- `0001-smbios-desktop-chassis.patch` — QEMU hardcodes SMBIOS chassis type 1
  ("Other") and zero power cords; the patch makes type 3 report "Desktop" with
  one power cord, which matches the advertised desktop board.
- `0002-smbios-ddr4-memory.patch` — QEMU reports SMBIOS memory type 0x07
  ("RAM") with unknown bus widths and zero voltage; the patch reports DDR4,
  synchronous, 64-bit widths and 1.2 V, matching the DDR4 kits in
  `hardware.toml`.

- `0003-acpi-ami-oem.patch` — ACPI OEM IDs become `ALASKA` / `A M I`, as on a
  real AMI board, instead of `INTEL` / `PC8086`.
- `0004-malware-analysis-hardening.patch` — blocks guest access to host
  loopback through the slirp gateway, stops emitting the hypervisor-only WAET
  ACPI table, and makes the optical drive report its configured model.

All are small, reviewed, and applied with a `--dry-run` first.

The build records what was built:

```bash
cat runtime/qemu/BUILD_INFO
```

Example:

```text
qemu_version=10.2.2
qemu_sha256=784b...
patch_url=https://raw.githubusercontent.com/zhaodice/qemu-anti-detection/2750.../qemu-10.2.2.patch
patch_commit=2750c86...
patch_sha256=0d05...
local_patch=patches/0001-smbios-desktop-chassis.patch
local_patch_sha256=...
local_memory_patch=patches/0002-smbios-ddr4-memory.patch
local_memory_patch_sha256=...
built_at=2026-11-01T...
prefix=/home/karna/stealth-vm/runtime/qemu
```

## Verification

```bash
# The binary is local and reports the expected version.
runtime/qemu/bin/qemu-system-x86_64 --version

# The patch marker is present.
grep -aq ASUS runtime/qemu/bin/qemu-system-x86_64 && echo "patch marker: present"

# SPICE and slirp are linked.
ldd runtime/qemu/bin/qemu-system-x86_64 | grep -E 'spice|slirp'

# QXL and e1000e devices are available.
runtime/qemu/bin/qemu-system-x86_64 -device help | grep -E 'qxl|e1000e'

# The CLI sees the local build.
./bin/svm doctor
./bin/svm version
```

`svm doctor` reports `patched` only if it finds the `ASUS` marker in the local
binary. A system QEMU can be used as a fallback, but it will be reported as
`NOT patched` and the stealth layer is inactive.

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `E: Unable to locate package ...` | Stale package lists or wrong distro | `sudo apt-get update`; check the package name for your release |
| `meson: command not found` | Missing build tool | `sudo apt install meson ninja-build` |
| `Dependency "spice-server" not found` | Missing SPICE headers | `sudo apt install libspice-server-dev libspice-protocol-dev` |
| `Dependency "slirp" not found` | Missing slirp headers | `sudo apt install libslirp-dev` |
| `glib-2.0 >= ... not found` | Missing GLib headers | `sudo apt install libglib2.0-dev` |
| `Permission denied` writing `runtime/qemu` | Previous build ran as root | `sudo chown -R "$USER:$USER" runtime build` |
| `patch does not apply` | Tarball/patch mismatch or already patched | Delete `build/src/qemu-10.2.2`, re-extract, re-run |
| `sha256sum: WARNING: 1 computed checksum did NOT match` | Upstream changed or download corrupted | Do not bypass; verify the pin and re-download |
| `ASUS` marker not found | Patch not applied, or binary stripped | Check `.svm-patched`; rebuild with `--force` |
| `-spice: invalid option` | Built without SPICE | Reinstall `libspice-server-dev` and rebuild |
| `qemu-system-x86_64: -sandbox ...` fails | Seccomp blocks a needed syscall | Use `--no-sandbox` only to confirm; report the syscall |
| `KVM: permission denied` | User not in `kvm` group | `sudo usermod -aG kvm "$USER"`; log out/in |
| `OVMF not found` | Firmware package missing | `sudo apt install ovmf`; check `/usr/share/OVMF` |
| `Secure Boot OVMF is missing` | Firmware package lacks `.ms.fd` | Use `win10`, or install a secure-boot OVMF package |
| `swtpm not found` | TPM helper missing | `sudo apt install swtpm swtpm-tools` |
| `no SPICE viewer found` | Viewer missing | `sudo apt install virt-viewer`; or `--headless` |
| Build OOM-killed | Too many parallel jobs | `JOBS=4 ./bootstrap.sh --build --force` |
| Build takes too long | Normal for QEMU | Use a faster machine or accept 15-40 min |

For a failed configure, read `build/src/qemu-10.2.2/config.log` (or the
`meson-logs/meson-log.txt` in the build directory). For a failed compile, the
last error is usually a missing `-dev` package.

## Offline build

On a machine with the same packages installed:

1. Download the tarball and patch on a connected machine.
2. Put them in `build/downloads/` with the exact names:
   - `qemu-10.2.2.tar.xz`
   - `qemu-anti-detection-10.2.2.patch`
3. Run `./bootstrap.sh --build`.

The script still verifies both SHA-256 values and will not use different bytes.

## Updating QEMU or the patch

1. Pick a QEMU release and a patch commit that supports it.
2. Update `QEMU_VERSION`, `QEMU_SHA256`, `PATCH_COMMIT`, `PATCH_URL`, and
   `PATCH_SHA256` in `scripts/build-qemu.sh`.
3. Verify the patch applies to a clean tree:
   ```bash
   cd build/src/qemu-$NEW_VERSION
   patch -p1 --dry-run < ../../downloads/qemu-anti-detection-$NEW_VERSION.patch
   ```
4. Rebuild with `./bootstrap.sh --build --force`.
5. Run `svm doctor` and boot a Linux smoke-test base before trusting Windows.
6. Recreate bases after a major QEMU/machine-type change. The `q35` alias points
   at the latest machine type; device model changes can affect Windows drivers
   and activation.

Do not mix a QEMU tarball with a patch built for a different release. The
`--dry-run` and SHA-256 checks are there to stop that.

## Uninstall / clean rebuild

```bash
# Remove the patched build only.
rm -rf runtime/qemu

# Remove downloads and unpacked source too.
rm -rf build/downloads build/src

# Remove all bases and sessions (asks for confirmation).
./bin/svm nuke
./bin/svm base delete linux --force
```

Distro packages installed by `bootstrap.sh` remain. Remove them with your
package manager if you no longer need QEMU build dependencies.

## Disk and time budget

| Item | Approx. size |
|---|---|
| QEMU tarball | ~130 MiB |
| Unpacked source | ~1.5 GiB |
| Build objects | ~2-4 GiB |
| Installed prefix | ~300-600 MiB |
| Linux base (sparse) | ~11 GiB after provisioning |
| Windows base (optional) | ~20-40 GiB after install |
| Session overlay | Grows with guest writes; persists until destroyed |

A 500 GB qcow2 disk is sparse; the file only grows as the guest writes. Keep at
least 40 GiB free for the Linux base and a session, more for large installers.

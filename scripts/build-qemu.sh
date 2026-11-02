#!/usr/bin/env bash
# Build QEMU 10.2.2 with zhaodice/qemu-anti-detection into runtime/qemu.
# Nothing is installed system-wide.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QEMU_VERSION="10.2.2"
QEMU_SHA256="784b296ff29c1417aa72323abcb2d2ea9ab9771724f577dcd785c3b04f21e176"
PATCH_COMMIT="2750c86d2d045243ba6617951487e41b25c05557"
PATCH_URL="https://raw.githubusercontent.com/zhaodice/qemu-anti-detection/${PATCH_COMMIT}/qemu-${QEMU_VERSION}.patch"
PATCH_SHA256="0d05f1a0ced91cef3fe33203b7d81c955d64694f30085544dc5e154809da110e"

PREFIX="$ROOT/runtime/qemu"
DOWNLOADS="$ROOT/build/downloads"
SRC_ROOT="$ROOT/build/src"
SRC="$SRC_ROOT/qemu-$QEMU_VERSION"
TARBALL="$DOWNLOADS/qemu-$QEMU_VERSION.tar.xz"
PATCH="$DOWNLOADS/qemu-anti-detection-$QEMU_VERSION.patch"
LOCAL_PATCH="$ROOT/patches/0001-smbios-desktop-chassis.patch"
LOCAL_MEMORY_PATCH="$ROOT/patches/0002-smbios-ddr4-memory.patch"
LOCAL_ACPI_PATCH="$ROOT/patches/0003-acpi-ami-oem.patch"
BINARY="$PREFIX/bin/qemu-system-x86_64"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
    FORCE=1
fi

# QEMU's build is memory-hungry. Cap the default so a 12-thread desktop does
# not OOM; override with JOBS=... for a bigger machine.
JOBS="${JOBS:-$(nproc)}"
if (( JOBS > 8 )); then
    JOBS=8
fi

mkdir -p "$DOWNLOADS" "$SRC_ROOT" "$PREFIX"

if [[ -x "$BINARY" && "$FORCE" -eq 0 ]]; then
    echo "patched QEMU already built: $BINARY"
    echo "use '$0 --force' to rebuild"
    exit 0
fi

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing build tool: $1" >&2
        echo "run: sudo ./bootstrap.sh --deps" >&2
        exit 1
    }
}
need curl
need sha256sum
need tar
need patch
need make
need gcc
need meson
need ninja
need python3
need pkg-config

if [[ ! -f "$TARBALL" ]]; then
    echo "downloading QEMU $QEMU_VERSION"
    curl -fL --retry 3 --retry-delay 2 \
        "https://download.qemu.org/qemu-$QEMU_VERSION.tar.xz" \
        -o "$TARBALL"
fi
echo "$QEMU_SHA256  $TARBALL" | sha256sum -c -

if [[ ! -f "$PATCH" ]]; then
    echo "downloading anti-detection patch"
    curl -fL --retry 3 --retry-delay 2 "$PATCH_URL" -o "$PATCH"
fi
echo "$PATCH_SHA256  $PATCH" | sha256sum -c -

if [[ "$FORCE" -eq 1 && -d "$SRC" ]]; then
    rm -rf "$SRC"
fi
if [[ ! -d "$SRC" ]]; then
    echo "extracting QEMU $QEMU_VERSION"
    tar -xf "$TARBALL" -C "$SRC_ROOT"
fi

cd "$SRC"
if [[ ! -f .svm-patched ]]; then
    echo "applying anti-detection patch"
    patch -p1 --dry-run < "$PATCH" >/dev/null
    patch -p1 < "$PATCH"
    touch .svm-patched
else
    echo "anti-detection patch already applied"
fi

if [[ ! -f "$LOCAL_PATCH" ]]; then
    echo "missing local patch: $LOCAL_PATCH" >&2
    exit 1
fi
if [[ ! -f .svm-chassis-patched ]]; then
    echo "applying desktop-chassis SMBIOS patch"
    patch -p1 --dry-run < "$LOCAL_PATCH" >/dev/null
    patch -p1 < "$LOCAL_PATCH"
    touch .svm-chassis-patched
else
    echo "desktop-chassis SMBIOS patch already applied"
fi

if [[ ! -f "$LOCAL_MEMORY_PATCH" ]]; then
    echo "missing local patch: $LOCAL_MEMORY_PATCH" >&2
    exit 1
fi
if [[ ! -f .svm-memory-patched ]]; then
    echo "applying DDR4-memory SMBIOS patch"
    patch -p1 --dry-run < "$LOCAL_MEMORY_PATCH" >/dev/null
    patch -p1 < "$LOCAL_MEMORY_PATCH"
    touch .svm-memory-patched
else
    echo "DDR4-memory SMBIOS patch already applied"
fi

if [[ ! -f "$LOCAL_ACPI_PATCH" ]]; then
    echo "missing local patch: $LOCAL_ACPI_PATCH" >&2
    exit 1
fi
if [[ ! -f .svm-acpi-patched ]]; then
    echo "applying AMI ACPI OEM-ID patch"
    patch -p1 --dry-run < "$LOCAL_ACPI_PATCH" >/dev/null
    patch -p1 < "$LOCAL_ACPI_PATCH"
    touch .svm-acpi-patched
else
    echo "AMI ACPI OEM-ID patch already applied"
fi

echo "configuring (prefix: $PREFIX)"
./configure \
    --prefix="$PREFIX" \
    --target-list=x86_64-softmmu \
    --enable-kvm \
    --enable-spice \
    --enable-slirp \
    --enable-seccomp \
    --disable-docs \
    --disable-gtk \
    --disable-sdl \
    --disable-opengl \
    --disable-virglrenderer \
    --disable-werror \
    --audio-drv-list=pa

echo "building with $JOBS jobs"
make -j"$JOBS"
make install

if [[ ! -x "$BINARY" ]]; then
    echo "build finished but $BINARY is missing" >&2
    exit 1
fi

"$BINARY" --version
if grep -aq "ASUS" "$BINARY"; then
    echo "patch marker: present"
else
    echo "warning: patch marker 'ASUS' not found in the binary" >&2
fi

cat > "$PREFIX/BUILD_INFO" <<EOF
qemu_version=$QEMU_VERSION
qemu_sha256=$QEMU_SHA256
patch_url=$PATCH_URL
patch_commit=$PATCH_COMMIT
patch_sha256=$PATCH_SHA256
local_patch=patches/0001-smbios-desktop-chassis.patch
local_patch_sha256=$(sha256sum "$LOCAL_PATCH" | cut -d' ' -f1)
local_memory_patch=patches/0002-smbios-ddr4-memory.patch
local_memory_patch_sha256=$(sha256sum "$LOCAL_MEMORY_PATCH" | cut -d' ' -f1)
local_acpi_patch=patches/0003-acpi-ami-oem.patch
local_acpi_patch_sha256=$(sha256sum "$LOCAL_ACPI_PATCH" | cut -d' ' -f1)
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
prefix=$PREFIX
EOF

echo
echo "patched QEMU installed at: $BINARY"
echo "next: ./bin/svm doctor"

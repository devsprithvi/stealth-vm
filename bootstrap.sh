#!/usr/bin/env bash
# One-time bootstrap: install distro packages, then build patched QEMU
# into runtime/qemu as your desktop user.
#
#   sudo ./bootstrap.sh            # deps + build
#   sudo ./bootstrap.sh --deps     # deps only
#   ./bootstrap.sh --build         # build only (no root)
#   ./bootstrap.sh --build --force # clean rebuild
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="all"
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --deps) MODE="deps" ;;
        --build) MODE="build" ;;
        --force) FORCE=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [[ "$MODE" == "build" ]]; then
    exec bash "$ROOT/scripts/build-qemu.sh" $([[ "$FORCE" -eq 1 ]] && echo --force)
fi

if [[ "${EUID}" -ne 0 ]]; then
    echo "This step installs packages, so it needs root:" >&2
    echo "  sudo $0 $*" >&2
    exit 1
fi

TARGET_USER="${SUDO_USER:-}"
if [[ -z "$TARGET_USER" || "$TARGET_USER" == "root" ]]; then
    echo "Run this with sudo from your desktop account, not from a root shell." >&2
    exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

export DEBIAN_FRONTEND=noninteractive
echo "updating package lists"
apt-get update

# Required for the patched build and for running guests.
PACKAGES=(
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
)

# Nice-to-have; failures here do not stop the build.
OPTIONAL=(
    cloud-image-utils genisoimage nftables passt gh
)

install_one() {
    local pkg="$1"
    if apt-cache show "$pkg" >/dev/null 2>&1; then
        echo "  + $pkg"
        apt-get install -y --no-install-recommends "$pkg" >/dev/null
    else
        echo "  ! $pkg (not available; skipped)"
        return 1
    fi
}

echo "installing required packages"
for pkg in "${PACKAGES[@]}"; do
    install_one "$pkg" || {
        echo "required package failed: $pkg" >&2
        exit 1
    }
done

echo "installing optional packages"
for pkg in "${OPTIONAL[@]}"; do
    install_one "$pkg" || true
done

if [[ "$MODE" == "deps" ]]; then
    echo "dependencies installed. Next: ./bootstrap.sh --build"
    exit 0
fi

echo "building patched QEMU as $TARGET_USER"
runuser -u "$TARGET_USER" -- \
    env HOME="$TARGET_HOME" \
    bash -c "cd '$ROOT' && ./scripts/build-qemu.sh $([[ "$FORCE" -eq 1 ]] && echo --force)"

echo
echo "done. Next:"
echo "  cd $ROOT && ./bin/svm doctor"

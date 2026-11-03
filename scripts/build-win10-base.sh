#!/usr/bin/env bash
# Rebuild the Windows 10 base from the installer ISO: unattended, no user
# activity, generalized with sysprep, sealed read-only.
#
# What you get: a base with no user profile, no installed third-party software,
# no browser data and no online registrations (WARP is only an installer; every
# session installs and registers it itself). Because the base is generalized,
# each session's first boot runs Windows specialize + OOBE, which creates a new
# SID, MachineGuid, computer name and "user" profile. svm additionally gives the
# session overlay its own NTFS volume serial before it boots.
#
# The old base is moved to images/base/backup/, never deleted.
#
# Usage: ./scripts/build-win10-base.sh [--iso PATH] [--memory-mb 4096] [--vcpus 4]
# Needs sudo only for the final offline check/cleanup of the finished base.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ISO="$ROOT/images/iso/Win10_21H1_English_x64.iso"
MEMORY_MB=4096
VCPUS=4
NAME=win10
MSI="$ROOT/build/downloads/Cloudflare_WARP.msi"
MSI_URL="https://downloads.cloudflareclient.com/v1/download/windows/ga"
BUILD_ISO="$ROOT/images/iso/win10-build.iso"
BASE_DIR="$ROOT/images/base"
MNT="${SVM_BASE_MNT:-/mnt/win10base}"
NBD="${SVM_BASE_NBD:-/dev/nbd1}"
QEMU_NBD="$ROOT/runtime/qemu/bin/qemu-nbd"

while [ $# -gt 0 ]; do
    case "$1" in
        --iso) ISO="${2:?}"; shift 2 ;;
        --memory-mb) MEMORY_MB="${2:?}"; shift 2 ;;
        --vcpus) VCPUS="${2:?}"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }

[ -f "$ISO" ] || die "Windows ISO not found: $ISO"
command -v xorriso >/dev/null || die "xorriso is required"
[ -x "$QEMU_NBD" ] || QEMU_NBD="$(command -v qemu-nbd)" || die "qemu-nbd not found"
[ -x "$ROOT/.venv/bin/svm" ] || [ -x "$ROOT/bin/svm" ] || die "bin/svm missing"

# The host is small; an oversized guest killed the desktop once.
avail_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
need_mb=$((MEMORY_MB + 2048))
[ "$avail_mb" -ge "$need_mb" ] || die "only ${avail_mb} MiB free; need ${need_mb} MiB for a ${MEMORY_MB} MiB guest"
pgrep -f 'qemu-system-x86_64' >/dev/null && die "a QEMU guest is already running; stop it first"
if find "$ROOT/vms" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | grep -q .; then
    die "sessions exist and would point at the old base; run: ./bin/svm nuke --force"
fi

echo "==> WARP installer"
if [ ! -f "$MSI" ]; then
    mkdir -p "$(dirname "$MSI")"
    curl -fL -o "$MSI.part" "$MSI_URL"
    mv "$MSI.part" "$MSI"
fi
file "$MSI" | grep -q "MSI Installer" || die "$MSI is not an MSI package"
echo "sha256 $(sha256sum "$MSI" | cut -d' ' -f1)"

echo "==> build ISO"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/svm"
cp guest/windows/autounattend.xml "$stage/autounattend.xml"
cp guest/windows/build-base.ps1 guest/windows/first-logon.ps1 \
    guest/windows/session-unattend.xml "$stage/svm/"
cp "$MSI" "$stage/svm/warp.msi"
rm -f "$BUILD_ISO"
xorriso -as mkisofs -quiet -J -r -V SVMBUILD -o "$BUILD_ISO" "$stage"

echo "==> move the current base to backup/"
stamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BASE_DIR/backup"
for ext in qcow2 vars.fd json; do
    src="$BASE_DIR/$NAME.$ext"
    [ -e "$src" ] && mv "$src" "$BASE_DIR/backup/$NAME.$ext.pre-clean-$stamp"
done
rm -rf "$BASE_DIR/$NAME.tpm"

echo "==> install Windows, enter Audit mode, sysprep (about 20-40 minutes)"
./bin/svm base create "$NAME" --iso "$ISO" --driver-iso "$BUILD_ISO" \
    --profile win10 --vcpus "$VCPUS" --memory-mb "$MEMORY_MB" --headless --yes

echo "==> offline verification and cleanup of the finished base"
[ "$(id -u)" -eq 0 ] || SUDO=sudo
BASE="$BASE_DIR/$NAME.qcow2"
cleanup() {
    set +e
    ${SUDO:-} umount "$MNT" 2>/dev/null
    ${SUDO:-} "$QEMU_NBD" --disconnect "$NBD" >/dev/null 2>&1
    chmod 0444 "$BASE" "$BASE_DIR/$NAME.vars.fd" 2>/dev/null
    rm -rf "$stage"
}
trap cleanup EXIT
chmod u+w "$BASE"
${SUDO:-} modprobe nbd max_part=16
${SUDO:-} "$QEMU_NBD" --connect="$NBD" "$BASE"
sleep 1
${SUDO:-} mkdir -p "$MNT"
${SUDO:-} mount -t ntfs-3g -o rw,big_writes "${NBD}p3" "$MNT"

sysprep_tag="$MNT/Windows/System32/Sysprep/Sysprep_succeeded.tag"
if [ ! -f "$sysprep_tag" ]; then
    echo "--- build log from the guest ---"
    cat "$MNT/build-base.log" 2>/dev/null || echo "(no C:\\build-base.log)"
    die "sysprep did not complete (no Sysprep_succeeded.tag); base NOT usable"
fi
echo "ok  sysprep generalize completed"

# Build residue. Windows recreates anything it needs.
${SUDO:-} rm -f "$MNT/build-base.log" "$MNT/Windows/Setup/Scripts/run-sysprep.ps1"
${SUDO:-} find "$MNT/Windows/Panther" "$MNT/Windows/System32/Sysprep/Panther" \
    -maxdepth 2 -type f \( -name '*.log' -o -name '*.etl' \) -delete 2>/dev/null || true
${SUDO:-} rm -f "$MNT"/Windows/INF/setupapi*.log

fail=0
check_absent() {
    if [ -e "$MNT/$1" ]; then echo "FAIL $1 is present"; fail=1; else echo "ok  no $1"; fi
}
check_absent hiberfil.sys
check_absent "ProgramData/Cloudflare"
check_absent "Program Files/Cloudflare"
check_absent "Users/user"
echo "profiles: $(ls "$MNT/Users" | tr '\n' ' ')"
echo "programs: $(ls "$MNT/Program Files" | tr '\n' ' ')"
[ -f "$MNT/Windows/Setup/Scripts/warp.msi" ] || { echo "FAIL warp.msi was not staged"; fail=1; }
[ -f "$MNT/Windows/Setup/Scripts/first-logon.ps1" ] || { echo "FAIL first-logon.ps1 was not staged"; fail=1; }
[ "$fail" -eq 0 ] || die "base verification failed"

echo "base ready: $BASE"

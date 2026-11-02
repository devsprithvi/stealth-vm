#!/usr/bin/env bash
# Provision the Ubuntu base in a chroot, then seal it read-only.
#
# This is the reproducible host-side wrapper around
# scripts/configure-linux-base.sh. It refuses to run while any session exists,
# because changing a base invalidates every overlay backed by it.
#
# Usage: sudo ./scripts/provision-linux-base.sh [base.qcow2]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${1:-$ROOT/images/base/linux.qcow2}"
VARS="${BASE%.qcow2}.vars.fd"
MNT="${SVM_BASE_MNT:-/mnt/linuxbase}"
NBD="${SVM_BASE_NBD:-/dev/nbd0}"

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0 [base.qcow2]" >&2
    exit 1
fi
if [ ! -f "$BASE" ]; then
    echo "base not found: $BASE" >&2
    exit 1
fi
if find "$ROOT/vms" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | grep -q .; then
    echo "sessions exist; delete them first: ./bin/svm nuke --force" >&2
    exit 1
fi

cleanup() {
    set +e
    mountpoint -q "$MNT/dev/pts" && umount "$MNT/dev/pts"
    mountpoint -q "$MNT/dev" && umount "$MNT/dev"
    mountpoint -q "$MNT/proc" && umount "$MNT/proc"
    mountpoint -q "$MNT" && umount "$MNT"
    qemu-nbd --disconnect "$NBD" >/dev/null 2>&1
    rmdir "$MNT" 2>/dev/null
}
trap cleanup EXIT

chmod u+w "$BASE"
modprobe nbd max_part=16
qemu-nbd --connect="$NBD" "$BASE"
sleep 1
mkdir -p "$MNT"
mount "${NBD}p2" "$MNT"
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts"
mount --bind /proc "$MNT/proc"

# The sealed image points resolv.conf at systemd-resolved; use real DNS for
# apt inside the chroot. The provisioning script restores the symlink.
rm -f "$MNT/etc/resolv.conf"
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' >"$MNT/etc/resolv.conf"
install -m 0755 "$ROOT/scripts/configure-linux-base.sh" \
    "$MNT/root/configure-linux-base.sh"
chroot "$MNT" /bin/bash /root/configure-linux-base.sh

# Keep the base free of provisioning residue.
rm -f "$MNT/root/configure-linux-base.sh"
find "$MNT/run" "$MNT/tmp" "$MNT/var/tmp" -mindepth 1 -delete 2>/dev/null || true
rm -rf "$MNT/root/.cache" "$MNT/root/.local" "$MNT/root/.config"
rm -f "$MNT/root/.bash_history" "$MNT/home/user/.bash_history"
rm -rf "$MNT/home/user/.cache" "$MNT/home/user/.local"
rm -f "$MNT/home/user/.Xauthority" "$MNT/home/user/.xsession-errors"
find "$MNT/var/log" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true

sync
umount "$MNT/dev/pts"
umount "$MNT/dev"
umount "$MNT/proc"
umount "$MNT"
e2fsck -fn "${NBD}p2"
qemu-nbd --disconnect "$NBD"
chmod 0444 "$BASE" "$VARS" 2>/dev/null || true
qemu-img check "$BASE"

echo "base provisioned and sealed: $BASE"

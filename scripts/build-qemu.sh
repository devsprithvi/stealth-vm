#!/usr/bin/env bash
# Build QEMU 10.2.2 into runtime/qemu with two layers of patches:
#
#   1. zhaodice/qemu-anti-detection (pinned by commit + SHA-256): renames the
#      QEMU / Red Hat / Bochs identifiers that guests and malware look for.
#   2. patches/*.patch (ours, applied in filename order): consistency fixes
#      and hardening on top of layer 1. See docs/ANTI-DETECTION.md.
#
# Nothing is installed system-wide. Re-running is safe and incremental: patches
# that are already applied are detected and skipped, and `make` only rebuilds
# what changed. Use --force for a clean rebuild from the pristine tarball.
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
PATCH_DIR="$ROOT/patches"
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

# One hash over every local patch: if it matches what the existing binary was
# built from, there is nothing to do.
shopt -s nullglob
LOCAL_PATCHES=("$PATCH_DIR"/*.patch)
shopt -u nullglob
LOCAL_HASH="$(cat "${LOCAL_PATCHES[@]}" 2>/dev/null | sha256sum | cut -d' ' -f1)"
if [[ -x "$BINARY" && "$FORCE" -eq 0 ]] \
    && grep -qx "local_patches_sha256=$LOCAL_HASH" "$PREFIX/BUILD_INFO" 2>/dev/null; then
    echo "patched QEMU is up to date: $BINARY"
    echo "use '$0 --force' to rebuild from scratch"
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

# Apply a patch exactly once, tracked by a marker file in the source tree.
# (Reverse-applying to detect "already applied" does not work: later patches
# touch lines the earlier ones changed.) The legacy marker names written by
# earlier versions of this script are honoured so an existing tree is not
# patched twice.
apply_patch() {
    local file="$1" label="$2" legacy="${3:-}"
    local marker=".svm-applied-$(basename "$file")"
    if [[ -f "$marker" ]]; then
        echo "already applied: $label"
        return
    fi
    if [[ -n "$legacy" && -f "$legacy" ]]; then
        mv "$legacy" "$marker"
        echo "already applied: $label"
        return
    fi
    patch -p1 --dry-run -s < "$file" >/dev/null || {
        echo "patch does not apply cleanly: $label" >&2
        echo "(rebuild from a clean tree with: $0 --force)" >&2
        exit 1
    }
    patch -p1 -s < "$file"
    touch "$marker"
    echo "applied: $label"
}

apply_patch "$PATCH" "upstream anti-detection ($PATCH_COMMIT)" .svm-patched
if (( ${#LOCAL_PATCHES[@]} == 0 )); then
    echo "no local patches found in $PATCH_DIR" >&2
    exit 1
fi
for file in "${LOCAL_PATCHES[@]}"; do
    case "$(basename "$file")" in
        0001-*) legacy=.svm-chassis-patched ;;
        0002-*) legacy=.svm-memory-patched ;;
        0003-*) legacy=.svm-acpi-patched ;;
        *) legacy= ;;
    esac
    apply_patch "$file" "$(basename "$file")" "$legacy"
done

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

{
    echo "qemu_version=$QEMU_VERSION"
    echo "qemu_sha256=$QEMU_SHA256"
    echo "patch_url=$PATCH_URL"
    echo "patch_commit=$PATCH_COMMIT"
    echo "patch_sha256=$PATCH_SHA256"
    for file in "${LOCAL_PATCHES[@]}"; do
        echo "local_patch=patches/$(basename "$file") sha256=$(sha256sum "$file" | cut -d' ' -f1)"
    done
    echo "local_patches_sha256=$LOCAL_HASH"
    echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "prefix=$PREFIX"
} > "$PREFIX/BUILD_INFO"

echo
echo "patched QEMU installed at: $BINARY"
echo "next: ./bin/svm doctor"

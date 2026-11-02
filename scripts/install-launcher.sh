#!/usr/bin/env bash
# Install the clickable desktop launcher for the current user.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
sed "s|@ROOT@|$ROOT|g" "$ROOT/desktop/svm.desktop" >"$APPS/svm.desktop"
chmod 0755 "$APPS/svm.desktop"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
if [ -z "$DESKTOP_DIR" ] || [ "$DESKTOP_DIR" = "$HOME" ]; then
    DESKTOP_DIR="$HOME/Desktop"
fi
mkdir -p "$DESKTOP_DIR"
cp "$APPS/svm.desktop" "$DESKTOP_DIR/svm.desktop"
chmod 0755 "$DESKTOP_DIR/svm.desktop"
gio set "$DESKTOP_DIR/svm.desktop" metadata::trusted true 2>/dev/null || true

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
fi

printf 'installed %s\n' "$APPS/svm.desktop"
printf 'installed %s\n' "$DESKTOP_DIR/svm.desktop"

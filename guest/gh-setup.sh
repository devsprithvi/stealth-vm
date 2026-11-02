#!/usr/bin/env bash
# GitHub CLI setup for a Linux guest (Debian/Ubuntu).
# Run with sudo inside the guest.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "run with sudo: sudo $0" >&2
    exit 1
fi

echo "adding official GitHub CLI repository"
mkdir -p -m 0755 /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
chmod 0644 /etc/apt/keyrings/githubcli-archive-keyring.gpg

cat >/etc/apt/sources.list.d/github-cli.list <<'EOF'
deb [arch=amd64 signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main
EOF

apt-get update
apt-get install -y gh git

echo ""
echo "GitHub CLI installed successfully:"
gh --version

#!/usr/bin/env bash
# Cloudflare WARP setup for a Linux guest (Debian/Ubuntu).
# Run with sudo inside the guest.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "run with sudo" >&2
    exit 1
fi

. /etc/os-release
CODENAME="${VERSION_CODENAME:-}"

if [[ -z "$CODENAME" ]]; then
    echo "could not detect the distro codename" >&2
    exit 1
fi

echo "adding Cloudflare WARP repository for $CODENAME"
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
    | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg

echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $CODENAME main" \
    > /etc/apt/sources.list.d/cloudflare-client.list

apt-get update
apt-get install -y cloudflare-warp

echo "registering a free WARP device"
warp-cli --accept-tos registration new || true
warp-cli --accept-tos connect || true
warp-cli --accept-tos status || true

cat <<'EOF'

Next steps:
  1. Enable Lockdown / Always On in the WARP client settings.
  2. Disable IPv6:
       sysctl -w net.ipv6.conf.all.disable_ipv6=1
       sysctl -w net.ipv6.conf.default.disable_ipv6=1
  3. Verify:
       curl https://www.cloudflare.com/cdn-cgi/trace
     (expect warp=on and a Cloudflare egress IP)
EOF

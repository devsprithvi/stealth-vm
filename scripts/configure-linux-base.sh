#!/usr/bin/env bash
# Configure the Ubuntu base image from inside a chroot.
#
# This script is intentionally idempotent. It is the reproducible definition
# of the Linux base: browser, WARP, network, hostname, locale, cleanup.
# Run as root inside the mounted base:
#
#   sudo chroot /mnt/linuxbase /bin/bash /root/configure-linux-base.sh
#
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export APT_LISTCHANGES_FRONTEND=none
export NEEDRESTART_MODE=a
# The base starts with C.UTF-8; avoid perl/locale noise until locale-gen runs.
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

log() { printf '\n==> %s\n' "$*"; }

# Chroot post-install scripts must not try to start services. This file is
# removed on exit and never reaches the sealed base.
cat >/usr/sbin/policy-rc.d <<'EOF'
#!/bin/sh
exit 101
EOF
chmod 0755 /usr/sbin/policy-rc.d
trap 'rm -f /usr/sbin/policy-rc.d' EXIT

log "base packages"
# Re-add the Cloudflare source only after its key has been dearmored; this
# also makes a rerun after a failed key import clean.
rm -f /etc/apt/sources.list.d/cloudflare-client.list
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg locales dmidecode amd64-microcode \
    xserver-xorg-video-qxl xserver-xorg-input-libinput at-spi2-core \
    xfce4 xfce4-terminal xfce4-appfinder tumbler gvfs \
    xfce4-power-manager xfce4-power-manager-plugins \
    xfce4-notifyd xfce4-screenshooter xfce4-taskmanager \
    lightdm lightdm-gtk-greeter \
    gnome-keyring libsecret-1-0 libsecret-tools \
    pipewire-audio pipewire-pulse wireplumber alsa-utils pavucontrol xfce4-pulseaudio-plugin \
    systemd-resolved netplan.io

# Keep the graphical stack and AMD microcode manual so a later autoremove
# cannot delete them. amd64-microcode is the normal AMD desktop package.
apt-mark manual \
    xserver-xorg-core xserver-xorg-video-qxl xserver-xorg-input-libinput \
    at-spi2-core xfce4 xfce4-terminal xfce4-appfinder tumbler gvfs \
    xfce4-power-manager xfce4-power-manager-plugins \
    xfce4-notifyd xfce4-screenshooter xfce4-taskmanager \
    lightdm lightdm-gtk-greeter \
    gnome-keyring libsecret-1-0 libsecret-tools \
    pipewire-audio pipewire-pulse wireplumber alsa-utils pavucontrol xfce4-pulseaudio-plugin \
    systemd-resolved netplan.io amd64-microcode \
    gh git >/dev/null

log "Mozilla apt repository and Firefox"
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg \
    -o /etc/apt/keyrings/packages.mozilla.org.asc
chmod 0644 /etc/apt/keyrings/packages.mozilla.org.asc
cat >/etc/apt/sources.list.d/mozilla.list <<'EOF'
deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main
EOF
cat >/etc/apt/preferences.d/mozilla <<'EOF'
Package: *
Pin: origin packages.mozilla.org
Pin-Priority: 1000
EOF
apt-get update
apt-get install -y firefox

log "Cloudflare WARP apt repository and client"
# Cloudflare publishes an ASCII-armored key; apt needs the dearmored form.
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
    | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
chmod 0644 /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
cat >/etc/apt/sources.list.d/cloudflare-client.list <<'EOF'
deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ noble main
EOF
apt-get update
apt-get install -y cloudflare-warp

log "GitHub CLI apt repository and client"
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
chmod 0644 /etc/apt/keyrings/githubcli-archive-keyring.gpg
cat >/etc/apt/sources.list.d/github-cli.list <<'EOF'
deb [arch=amd64 signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main
EOF
apt-get update
apt-get install -y gh git

log "apply available security updates"
apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade

log "remove installer, cloud, VMware and SPICE agents"
# Intel microcode packages are unnecessary on an AMD desktop.
apt-get purge -y \
    cloud-init cloud-initramfs-copymods cloud-initramfs-dyn-netconf \
    open-vm-tools spice-vdagent snapd intel-microcode iucode-tool 2>/dev/null || true

# Server/telemetry packages that are not part of a desktop and can phone home
# or retain installer metadata. Removing ubuntu-minimal (a metapackage) is
# harmless; its actual dependencies are protected.
apt-get purge -y \
    apport apport-core-dump-handler landscape-common open-iscsi pollinate \
    sosreport ubuntu-pro-client ubuntu-pro-client-l10n \
    update-manager-core update-notifier-common \
    ubuntu-release-upgrader-core ubuntu-release-upgrader-gtk \
    update-manager update-notifier gnome-initial-setup 2>/dev/null || true
apt-get autoremove -y --purge
rm -rf /etc/cloud /var/lib/cloud /var/log/cloud-init* /var/log/installer \
    /etc/subiquity /var/lib/subiquity /var/log/curtin* /var/log/ubuntu-advantage* \
    /snap /var/snap /var/lib/snapd
rm -rf /var/crash /var/lib/apport /var/lib/update-notifier /etc/update-manager \
    /etc/pollinate /var/lib/pollinate
rm -f /etc/apt/sources.list.d/ubuntu.sources.curtin.orig
rm -f /etc/apt/apt.conf.d/20snapd.conf
rm -rf /etc/apt/preferences.d.save
rm -f /etc/ssh/ssh_host_*
# The installer leaves curtin breadcrumbs in fstab comments.
sed -i '/curtin installation/d' /etc/fstab
rm -f /root/.bash_history /home/user/.bash_history
rm -rf /home/user/.cache /home/user/.local/share/recently-used.xbel

log "network: DHCP through systemd-networkd, no cloud-init"
cat >/etc/netplan/01-ethernet.yaml <<'EOF'
network:
  version: 2
  renderer: networkd
  ethernets:
    all-en:
      match:
        name: "en*"
      dhcp4: true
      dhcp6: false
      accept-ra: false
      # Do not create an IPv6 link-local address. Without this, networkd
      # adds fe80::/64 even when the kernel's disable_ipv6 sysctl is set.
      link-local: []
      optional: true
EOF
chmod 0600 /etc/netplan/01-ethernet.yaml
cat >/etc/sysctl.d/99-disable-ipv6.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
# Enable offline; systemctl may not be PID 1 inside the chroot.
SYSTEMD_OFFLINE=1 systemctl enable \
    systemd-networkd.service systemd-networkd-wait-online.service \
    systemd-resolved.service >/dev/null 2>&1 || true

log "hostname fallback (per-session service overrides it from SMBIOS UUID)"
printf 'desktop\n' >/etc/hostname
sed -i 's/^127\.0\.1\.1.*/127.0.1.1 desktop/' /etc/hosts
cat >/usr/lib/systemd/systemd-hostname-identity <<'EOF'
#!/bin/sh
set -eu
uuid_file=/sys/class/dmi/id/product_uuid
[ -r "$uuid_file" ] || exit 0
uuid=$(tr 'A-F' 'a-f' <"$uuid_file" | tr -d '-')
[ -n "$uuid" ] || exit 0
name="desktop-$(printf '%s' "$uuid" | cut -c1-6)"
printf '%s\n' "$name" >/etc/hostname
hostname "$name" 2>/dev/null || true
if grep -q '^127\.0\.1\.1' /etc/hosts; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1 $name/" /etc/hosts
else
    printf '127.0.1.1 %s\n' "$name" >>/etc/hosts
fi
EOF
chmod 0755 /usr/lib/systemd/systemd-hostname-identity
cat >/etc/systemd/system/systemd-hostname-identity.service <<'EOF'
[Unit]
Description=Synchronize hostname with system firmware identity
After=local-fs.target
Before=display-manager.service

[Service]
Type=oneshot
ExecStart=/usr/lib/systemd/systemd-hostname-identity
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
ln -sf /etc/systemd/system/systemd-hostname-identity.service \
    /etc/systemd/system/multi-user.target.wants/systemd-hostname-identity.service

log "WARP auto-connect and always-on policy (per-session registration)"
# Local MDM policy: lock the switch and start connected. This is the
# supported Linux WARP mechanism for Always On / Lockdown behaviour.
install -d -m 0700 /var/lib/cloudflare-warp
cat >/var/lib/cloudflare-warp/mdm.xml <<'EOF'
<dict>
  <key>switch_locked</key>
  <true/>
  <key>auto_connect</key>
  <integer>0</integer>
  <key>service_mode</key>
  <string>warp</string>
</dict>
EOF
chmod 0600 /var/lib/cloudflare-warp/mdm.xml
cat >/usr/local/sbin/warp-auto-connect <<'EOF'
#!/bin/sh
set -u
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Wait for warp-svc to answer; registration state is per overlay.
i=0
while [ "$i" -lt 30 ]; do
    if warp-cli --accept-tos status >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 1
done

# Apply the local always-on policy, then register this overlay if needed.
# Free WARP needs no account or password.
warp-cli --accept-tos mdm refresh >/dev/null 2>&1 || true
if ! warp-cli --accept-tos registration show >/dev/null 2>&1; then
    warp-cli --accept-tos registration new >/dev/null 2>&1 || true
fi
warp-cli --accept-tos mode warp >/dev/null 2>&1 || true
warp-cli --accept-tos connect >/dev/null 2>&1 || true
EOF
chmod 0755 /usr/local/sbin/warp-auto-connect
cat >/etc/systemd/system/warp-auto-connect.service <<'EOF'
[Unit]
Description=Cloudflare WARP automatic connection
After=network-online.target warp-svc.service
Wants=network-online.target warp-svc.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/warp-auto-connect
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
ln -sf /etc/systemd/system/warp-auto-connect.service \
    /etc/systemd/system/multi-user.target.wants/warp-auto-connect.service
SYSTEMD_OFFLINE=1 systemctl enable warp-svc.service >/dev/null 2>&1 || true

log "locale, timezone, keyboard"
locale-gen en_US.UTF-8 >/dev/null
update-locale LANG=en_US.UTF-8 LANGUAGE= LC_ALL= >/dev/null
ln -sf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime
printf 'Asia/Kolkata\n' >/etc/timezone
cat >/etc/default/keyboard <<'EOF'
XKBMODEL="pc105"
XKBLAYOUT="us"
XKBVARIANT=""
XKBOPTIONS=""
BACKSPACE="guess"
EOF

log "desktop defaults and Firefox policies"
mkdir -p /etc/firefox/policies
cat >/etc/firefox/policies/policies.json <<'EOF'
{
  "policies": {
    "DisableTelemetry": true,
    "DisableFirefoxStudies": true,
    "DisablePocket": true,
    "DisableFeedbackCommands": true,
    "DontCheckDefaultBrowser": true,
    "OverrideFirstRunPage": "",
    "OverridePostUpdatePage": "",
    "NoDefaultBookmarks": true,
    "OfferToSaveLogins": true,
    "PasswordManagerEnabled": true,
    "SearchSuggestEnabled": false,
    "FirefoxHome": {
      "Search": true,
      "TopSites": false,
      "SponsoredTopSites": false,
      "Pocket": false,
      "SponsoredPocket": false,
      "Snippets": false,
      "Highlights": false
    },
    "Preferences": {
      "termsofuse.bypassNotification": true,
      "datareporting.policy.dataSubmissionPolicyBypassNotification": true,
      "toolkit.telemetry.enabled": false,
      "toolkit.telemetry.unified": false,
      "toolkit.telemetry.archive.enabled": false,
      "datareporting.healthreport.uploadEnabled": false,
      "datareporting.policy.dataSubmissionEnabled": false,
      "browser.ping-centre.telemetry": false,
      "browser.newtabpage.activity-stream.feeds.telemetry": false,
      "browser.newtabpage.activity-stream.telemetry": false,
      "app.shield.optoutstudies.enabled": false,
      "app.normandy.enabled": false,
      "extensions.pocket.enabled": false,
      "browser.search.suggest.enabled": false,
      "browser.urlbar.suggest.searches": false,
      "browser.urlbar.suggest.quicksuggest.nonsponsored": false,
      "browser.urlbar.suggest.quicksuggest.sponsored": false,
      "browser.shell.checkDefaultBrowser": false,
      "browser.aboutwelcome.enabled": false,
      "browser.messaging-system.whatsNewPanel.enabled": false,
      "browser.discovery.enabled": false
    }
  }
}
EOF
chmod 0644 /etc/firefox/policies/policies.json
if [ -f /etc/xdg/xfce4/helpers.rc ]; then
    sed -i 's/^WebBrowser=.*/WebBrowser=firefox/' /etc/xdg/xfce4/helpers.rc
else
    printf 'WebBrowser=firefox\n' >/etc/xdg/xfce4/helpers.rc
fi
update-alternatives --install /usr/bin/x-www-browser x-www-browser /usr/bin/firefox 200 >/dev/null 2>&1 || true
update-alternatives --set x-www-browser /usr/bin/firefox >/dev/null 2>&1 || true
update-alternatives --install /usr/bin/gnome-www-browser gnome-www-browser /usr/bin/firefox 200 >/dev/null 2>&1 || true
update-alternatives --set gnome-www-browser /usr/bin/firefox >/dev/null 2>&1 || true
mkdir -p /home/user/.config
cat >/home/user/.config/mimeapps.list <<'EOF'
[Default Applications]
x-scheme-handler/http=firefox.desktop
x-scheme-handler/https=firefox.desktop
text/html=firefox.desktop
EOF
chown -R user:user /home/user/.config

log "passwordless local user (no passwords in files or prompts)"
id user >/dev/null 2>&1 || useradd -m -s /bin/bash user
usermod -aG sudo,audio,video,plugdev user
# Empty shadow entry: the account has no password. LightDM autologin enters
# the desktop, and sudo is passwordless. Nothing secret is stored on disk.
usermod -p '' user
cat >/etc/sudoers.d/90-user-nopasswd <<'EOF'
user ALL=(ALL) NOPASSWD:ALL
EOF
chmod 0440 /etc/sudoers.d/90-user-nopasswd
visudo -cf /etc/sudoers >/dev/null

log "autologin into XFCE"
cat >/etc/lightdm/lightdm.conf <<'EOF'
[Seat:*]
autologin-user=user
autologin-user-timeout=0
greeter-hide-users=true
user-session=xfce
EOF
chmod 0644 /etc/lightdm/lightdm.conf

# Start from the stock XFCE panel. A previous boot can leave stale plugin
# entries (for example a removed plugin id), which produces warnings and an
# incomplete panel on the next session.
rm -rf /home/user/.config/xfce4

log "reduce virtualization tells"
cat >/etc/modprobe.d/99-blacklist-kvm.conf <<'EOF'
blacklist kvm
blacklist kvm_amd
blacklist kvm_intel
EOF

log "machine-id is generated fresh in each writable overlay"
: >/etc/machine-id
chmod 0444 /etc/machine-id
ln -sf /etc/machine-id /var/lib/dbus/machine-id

log "clean installer and build residue"
apt-get clean
rm -rf /var/lib/apt/lists/*
# The base should not ship logs from package installation or the image build.
find /var/log -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
rm -rf /var/cache/apt/archives/partial/* /var/cache/debconf/*.dat-old
apt-get update
apt-get check
dpkg --audit

# Only point resolv.conf at systemd-resolved after all build-time network
# operations are finished; the stub resolver does not run inside the chroot.
ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf

log "installed versions"
dpkg-query -W -f='${Package} ${Version}\n' firefox cloudflare-warp systemd-resolved netplan.io 2>/dev/null || true
printf '\nbase configuration complete\n'

#!/usr/bin/env bash
# stealth-probe.sh - report what user-mode software inside a Linux guest can
# learn about the virtual machine. Read-only: it changes nothing.
#
# Run it inside the guest (the base user has passwordless sudo):
#   sudo bash stealth-probe.sh [--no-pause] [--host-port N]
#
# --host-port N  isolation test: first start a listener on the HOST, e.g.
#                python3 -m http.server N --bind 127.0.0.1
#                The probe then tries to reach it through the slirp gateway.
#                Reaching it means the guest can talk to host loopback services.
#
# Every line that looks like a virtualization giveaway is printed as [TELL]
# and repeated in the summary. "No tells" means these checks found none, not
# that the VM is undetectable (see docs/ANTI-DETECTION.md for what is out of
# reach of user-mode checks: timing, GPU, chipset ID, ...).
set -u

PAUSE=1
HOST_PORT=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-pause) PAUSE=0 ;;
        --host-port) shift; HOST_PORT="${1:-0}" ;;
    esac
    shift
done

# Anything matching this in a vendor/model/driver string is a giveaway.
PAT='qemu|virtio|bochs|bxpc|seabios|ovmf|edk ?ii|tianocore|red ?hat|spice|vbox|virtualbox|vmware|vmw|xen|kvm|parallels|innotek|1b36|1af4|1234|15ad|80ee'

TELLS=()
section() { printf '\n\033[36m=== %s ===\033[0m\n' "$1"; }
tell()    { TELLS+=("$1"); printf '  \033[31m[TELL] %s\033[0m\n' "$1"; }
page()    { if [ "$PAUSE" = 1 ]; then read -r -p '-- Enter --' _ </dev/tty; clear; fi; }
# Print a "label : value" row; flag it when the value matches the pattern.
row() {
    printf '%-10s: %s\n' "$1" "$2"
    if printf '%s' "$2" | grep -Eiq "$PAT"; then tell "$1 matches VM pattern: $2"; fi
}

[ "$PAUSE" = 1 ] && clear

section "1. Firmware identity (DMI)"
for f in sys_vendor product_name product_version product_serial \
         bios_vendor bios_version bios_date board_vendor board_name chassis_vendor; do
    row "$f" "$(cat /sys/class/dmi/id/$f 2>/dev/null || echo '?')"
done
chassis=$(cat /sys/class/dmi/id/chassis_type 2>/dev/null || echo '?')
printf '%-10s: %s\n' chassis_type "$chassis (3 = Desktop)"
[ "$chassis" != 3 ] && [ "$chassis" != 6 ] && [ "$chassis" != 7 ] && tell "chassis type $chassis is not a desktop/tower"
printf '%-10s: %s\n' dmi_uuid "$(cat /sys/class/dmi/id/product_uuid 2>/dev/null || echo 'need root')"
if command -v dmidecode >/dev/null; then
    printf '%-10s: %s\n' memory "$(dmidecode -t 17 2>/dev/null | awk -F': ' '/Manufacturer|Part Number|Speed:|Type:|Size:/{printf "%s | ", $2}')"
    dmidecode -t 17 2>/dev/null | grep -Eiq 'Type: (RAM|Unknown)' && tell "memory device type is generic RAM/Unknown, not DDR4"
    dmidecode -t 0 2>/dev/null | grep -qi 'Virtual Machine' && tell "BIOS characteristics flag 'Virtual Machine'"
fi
page

section "2. CPU"
row model "$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ //')"
row vendor "$(grep -m1 vendor_id /proc/cpuinfo | cut -d: -f2- | sed 's/^ //')"
printf '%-10s: %s threads, %s\n' topology "$(nproc)" "$(lscpu | awk -F: '/Core\(s\) per socket|Thread\(s\) per core/{gsub(/ /,"",$2); printf "%s ", $2}')"
printf '%-10s: %s\n' caches "$(lscpu | awk -F: '/^L[123]/{gsub(/^ +/,"",$2); printf "%s=%s  ", $1, $2}')"
grep -m1 -qw hypervisor /proc/cpuinfo && tell "CPUID hypervisor bit is set (flag 'hypervisor' in /proc/cpuinfo)"
printf '%-10s: %s\n' microcode "$(grep -m1 microcode /proc/cpuinfo | cut -d: -f2- | sed 's/^ //')"
printf '%-10s: %s\n' mem_total "$(awk '/MemTotal/{printf "%.2f GiB", $2/1048576}' /proc/meminfo)"
virt=$(systemd-detect-virt 2>/dev/null || true)
printf '%-10s: %s\n' detect_virt "${virt:-none}"
[ -n "$virt" ] && [ "$virt" != none ] && tell "systemd-detect-virt reports '$virt'"
[ -e /sys/hypervisor/type ] && tell "/sys/hypervisor/type exists: $(cat /sys/hypervisor/type)"
# "Booting paravirtualized kernel on bare hardware" is printed on real PCs too;
# only a named hypervisor (KVM, Xen, VMware, ...) is a giveaway.
dmesg 2>/dev/null | grep -Ei 'hypervisor detected|kvm-clock|qemu|bochs|paravirtualized kernel on (kvm|xen|vmware|hyper-v)' | head -5 | while read -r l; do tell "dmesg: $l"; done
page

section "3. PCI devices"
if command -v lspci >/dev/null; then
    lspci -nn
    ids=$(lspci -n | awk '{print $3}' | cut -d: -f1 | sort -u | tr '\n' ' ')
    printf 'Vendors   : %s\n' "$ids"
    for v in 1b36 1af4 1234 15ad 80ee; do
        printf '%s' "$ids" | grep -qw "$v" && tell "PCI vendor $v present"
    done
    lspci -nn | grep -Ei "$PAT" | while read -r l; do tell "lspci: $l"; done
fi
page

section "4. Storage, network, input, USB"
lsblk -d -o NAME,MODEL,SERIAL,ROTA,SIZE 2>/dev/null
lsblk -d -o MODEL 2>/dev/null | grep -Ei "$PAT" | while read -r l; do tell "disk model: $l"; done
[ "$(lsblk -d -n -o ROTA /dev/sda 2>/dev/null | tr -d ' ')" = 1 ] && tell "SSD reported as rotational (ROTA=1)"
for i in /sys/class/net/*; do
    n=$(basename "$i"); [ "$n" = lo ] && continue
    mac=$(cat "$i/address"); drv=$(basename "$(readlink -f "$i/device/driver" 2>/dev/null)")
    printf 'NIC       : %s  %s  driver=%s\n' "$n" "$mac" "$drv"
    case "$(printf '%s' "$mac" | tr a-f A-F | cut -c1-8)" in
        52:54:00|00:0C:29|00:50:56|08:00:27|00:1C:42|00:15:5D) tell "hypervisor MAC OUI on $n: $mac" ;;
    esac
    printf '%s' "$drv" | grep -Eiq 'virtio|vmxnet|hv_netvsc' && tell "paravirtual NIC driver on $n: $drv"
done
echo "-- input devices:"
grep -E '^N: Name=' /proc/bus/input/devices
grep -E '^N: Name=' /proc/bus/input/devices | grep -Ei "$PAT|tablet" | while read -r l; do tell "input: $l"; done
echo "-- usb:"
command -v lsusb >/dev/null && lsusb | head -12
page

section "5. Modules, services, processes, ACPI"
lsmod | awk '{print $1}' | grep -Ei 'qxl|virtio|kvm|vbox|vmw|hv_|bochs|9p|vsock|drm_kms_helper$' | tr '\n' ' '; echo
lsmod | awk '{print $1}' | grep -Ei 'virtio|kvm|vbox|vmw|hv_|9p|vsock' | while read -r m; do tell "kernel module loaded: $m"; done
lsmod | awk '{print $1}' | grep -qx qxl && tell "qxl kernel module loaded (virtual GPU driver)"
ps -eo comm= | grep -Ei 'vdagent|qemu-ga|vmtoolsd|vboxservice|spice|hv_' | sort -u | while read -r p; do tell "process: $p"; done
echo "ACPI tables: $(ls /sys/firmware/acpi/tables 2>/dev/null | tr '\n' ' ')"
ls /sys/firmware/acpi/tables 2>/dev/null | grep -qx WAET && tell "ACPI table WAET present (hypervisor-only)"
for t in DSDT FACP RSDT; do
    f=/sys/firmware/acpi/tables/$t
    [ -r "$f" ] && printf '%-6s OEM: %s / %s\n' "$t" "$(dd if=$f bs=1 skip=10 count=6 2>/dev/null | tr -d '\0')" "$(dd if=$f bs=1 skip=16 count=8 2>/dev/null | tr -d '\0')"
done
if [ -r /sys/firmware/acpi/tables/DSDT ]; then
    strings -n 4 /sys/firmware/acpi/tables/DSDT | grep -Eio 'qemu[0-9a-z]*|bochs|bxpc' | sort -u | while read -r s; do tell "DSDT contains '$s'"; done
fi
ls /sys/bus/acpi/devices 2>/dev/null | grep -Ei 'qemu|bochs' | while read -r d; do tell "ACPI device $d"; done
page

section "6. Display and network egress"
command -v xrandr >/dev/null && DISPLAY="${DISPLAY:-:0}" xrandr 2>/dev/null | grep -E ' connected|\*' | head -3
if command -v warp-cli >/dev/null; then
    st=$(warp-cli --accept-tos status 2>/dev/null | head -2 | tr '\n' ' ')
    printf '%-10s: %s\n' warp "$st"
    if printf '%s' "$st" | grep -qi 'Connected'; then
        curl -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|warp|colo)='
    else
        tell "WARP is not connected: traffic would leave with the host IP"
    fi
fi
printf '%-10s: %s min\n' uptime "$(awk '{printf "%d", $1/60}' /proc/uptime)"
page

if [ "$HOST_PORT" -gt 0 ]; then
    section "7. Host isolation (loopback via slirp gateway)"
    if timeout 4 bash -c "exec 3<>/dev/tcp/192.168.1.1/$HOST_PORT" 2>/dev/null; then
        tell "guest reached HOST loopback port $HOST_PORT via 192.168.1.1"
    else
        printf '  \033[32mhost loopback port %s is NOT reachable from the guest (good)\033[0m\n' "$HOST_PORT"
    fi
    page
fi

section "SUMMARY"
if [ "${#TELLS[@]}" -eq 0 ]; then
    printf '\033[32mNo tells found by these checks.\033[0m\n'
else
    printf '\033[31m%d tell(s):\033[0m\n' "${#TELLS[@]}"
    printf '  - %s\n' "${TELLS[@]}"
fi

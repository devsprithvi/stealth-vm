# Network model

The guest gets one emulated Intel e1000e NIC on a private QEMU user-mode
(slirp) network. There is no bridge, no TAP device, no host DHCP server, no
port forward, and no inbound path to the guest. Cloudflare WARP runs **inside
the guest** and is the intended egress path.

## Configuration

`config/defaults.toml`:

```toml
[network]
mode = "user"                 # QEMU slirp; no bridge, no host forwards
nic = "e1000e"
subnet = "192.168.1.0/24"
gateway = "192.168.1.1"
dhcp_start = "192.168.1.100"
dns = "192.168.1.2"
ipv6 = false
```

`dns` must differ from `gateway`; `svm` refuses to render a config where they
are equal. The guest receives:

| Setting | Value |
|---|---|
| Guest address | `192.168.1.100` (first DHCP lease) |
| Netmask | `255.255.255.0` |
| Gateway | `192.168.1.1` |
| DNS | `192.168.1.2` |
| IPv6 | disabled in slirp and should be disabled on the guest NIC |
| DHCP hostname | the session's generated hostname |
| Domain | `lan` |

The DHCP hostname is not the Windows computer name. Windows keeps the name set
during base installation; `svm` does not rename the guest OS per session. See
`ARCHITECTURE.md` for the sysprep caveat.

The subnet is deliberately a normal home-router range instead of QEMU's famous
`10.0.2.0/24`. Change all four values together if `192.168.1.0/24` collides with
something in your environment. The subnet is internal to slirp; it does not
create a host interface or touch your LAN.

## Packet flow

```text
guest application
  │
  ├─ WARP virtual adapter (full-tunnel mode)
  │     └─ WireGuard/UDP to a Cloudflare edge
  │
  └─ e1000e 192.168.1.100
        └─ slirp 192.168.1.1
              └─ host socket (QEMU process)
                    └─ Cloudflare edge
                          └─ destination
```

With WARP connected and Lockdown/Always On enabled, destinations see a
Cloudflare egress IP. Your ISP sees encrypted WireGuard traffic from the host to
Cloudflare. Cloudflare sees your host's public IP and the WARP device
registration.

Without WARP, slirp sends traffic directly from the host and destinations see
your public IP. There is no automatic kill switch in `svm`; the kill switch is
WARP's Lockdown mode inside the guest.

## What is hidden from the guest

- Host SMBIOS/DMI, board, BIOS, disk serials, and UUID (replaced per session).
- Host MAC address. The guest sees the generated e1000e MAC.
- Host hostname. The DHCP hostname is the guest's generated hostname.
- Host IP addresses and routes. The guest sees only `192.168.1.0/24`.
- Host open ports via port forwarding: none are configured. The slirp gateway
  still aliases host loopback; see the caveat below.
- Host shared folders, clipboard, USB devices, serial ports, and guest agent:
  none are configured.

## What is not hidden

| Data | Guest can see it? | Destination sees it? | Cloudflare sees it? |
|---|---|---|---|
| Host public IP | Only if WARP is off | No, while WARP is on | **Yes** |
| Host LAN IP | No | No | No |
| Host MAC | No | No | No |
| Host hostname | No | No | No |
| Guest private IP | Yes (`192.168.1.100`) | No (NAT) | No |
| Guest MAC | Yes | No | No |
| Host timezone | **Yes, indirectly** | No | No |
| Host CPU features | Partially (CPUID) | No | No |

Two caveats matter:

1. **The slirp gateway is a host alias.** `192.168.1.1` maps to the host's
   loopback. A guest can connect to services listening on the host's
   `127.0.0.1`, even though it cannot reach the host's LAN IP. This is a
   property of QEMU user-mode networking, not a `svm` bug. Do not run sensitive
   local services while a guest is running. For stronger isolation, use a
   network namespace for QEMU or the passt backend with
   `map-host-loopback=none` (not implemented in `svm` yet).
2. **The RTC can reveal the host timezone.** Windows guests use
   `-rtc base=localtime`, so the initial RTC reflects the host's local time.
   Windows normally sets its own timezone after boot, but a determined program
   can sample the RTC early. Linux guests use `base=utc`. If this matters,
   configure Windows with `RealTimeIsUniversal=1` and switch the profile to UTC;
   `svm` does not expose an RTC override yet.

## DNS behavior

The guest's DHCP DNS is `192.168.1.2`, a virtual slirp address. Slirp forwards
those queries to the host's configured resolver. This has two consequences:

- Before WARP is connected, guest DNS queries can reach the host's ISP resolver.
  Lockdown mode does not necessarily cover the boot window before the WARP
  client is up.
- After WARP is connected, the WARP client normally intercepts DNS and uses
  DNS-over-HTTPS to Cloudflare. Do not assume every resolver path is captured:
  applications that hard-code DoH/DoT endpoints can bypass the guest's DHCP DNS.

For strict leak prevention, enable WARP at boot, enable Lockdown/Always On, and
consider a host-level VPN or a network-level Tor gateway. A host-level VPN also
covers the pre-WARP boot window.

## WARP setup

Install the WARP client and MDM policy **inside the base image**, once.
Sessions clone the base and inherit the client and policy; the first boot
registers the device in that session's overlay, and the registration persists
across stop/start until the session is destroyed. See `guest/README.md` and the
scripts in `guest/`.

Windows, elevated PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\warp-setup.ps1
```

Linux guest:

```bash
sudo ./warp-setup.sh
```

Then, in the WARP client:

1. Log in / register the device.
2. Connect.
3. Enable **Lockdown mode** (newer clients call it **Always On**). This makes
   the client fail closed: if WARP drops, the guest has no direct internet.
4. Disable IPv6 on the guest NIC:
   - Windows: `Get-NetAdapter | Disable-NetAdapterBinding -ComponentID ms_tcpip6`
   - Linux: the base writes `/etc/sysctl.d/99-disable-ipv6.conf` and
     `/etc/netplan/01-ethernet.yaml` with `link-local: []`. Without the netplan
     key, networkd still adds an `fe80::/64` address even when the kernel
     `disable_ipv6` sysctl is set.
5. Verify before sealing the base.

`svm` also sets `ipv6 = "off"` on the slirp netdev, so the guest has no IPv6
connectivity even if its NIC still has the binding.

Each session registers its own WARP device. The base contains only the client
and MDM policy; deleting a session deletes its registration. Free-plan device
limits apply if you create many sessions.

## Verification

Inside the guest:

```bash
curl -s https://www.cloudflare.com/cdn-cgi/trace
```

Expect `warp=on` and an `ip=` that is a Cloudflare address, not your home IP.

```bash
# Linux client
warp-cli --accept-tos status
warp-cli --accept-tos settings

# Windows client (elevated)
warp-cli --accept-tos status
```

Fail-closed test: disconnect WARP while Lockdown is on, then try to reach the
internet. The request should fail, not fall back to direct NAT.

```bash
curl --max-time 5 https://1.1.1.1
curl -6 --max-time 5 https://ipv6.google.com
```

Both should fail (the second because IPv6 is disabled). Reconnect WARP before
continuing.

Other checks:

- `ipconfig /all` (Windows) or `ip addr` (Linux): one e1000e NIC, no IPv6
  global address, DNS `192.168.1.2`.
- `tracert` / `traceroute` to a public host: the first hop is `192.168.1.1`;
  beyond that you should not see your home router or ISP.
- Browser WebRTC can reveal the guest's private IP. It is not the host IP, but
  it is still a fingerprint. Disable WebRTC for sensitive tests.
- A DNS leak test should show Cloudflare's resolver, not your ISP's.

## Host-side visibility

The host sees:

- QEMU's host sockets to Cloudflare edge addresses (WireGuard UDP) while WARP is
  connected.
- QEMU's DNS lookups to the host resolver during the pre-WARP window.
- QEMU's connections to host loopback if the guest probes `192.168.1.1`.

The host does **not** see a TAP interface, a bridge, DHCP leases, or guest
traffic in plaintext. Slirp terminates TCP/UDP in the QEMU process, so the host
sees ordinary sockets owned by QEMU.

## Stronger options

These are not implemented by `svm` today, but they are the right direction if
the slirp caveats matter:

1. **Host-level WireGuard/OpenVPN.** Route all host traffic through a VPN at the
   router or host firewall. QEMU's slirp then inherits the tunnel, including the
   pre-WARP boot window. The VPN provider sees your host IP; destinations see
   the VPN egress. You can still run WARP inside the guest for a second hop, at
   a latency cost.
2. **passt backend with `map-host-loopback=none`.** QEMU 10.2.2 supports the
   passt backend, and passt can refuse to map the host loopback. It also performs
   better than slirp. `svm` currently renders only `type = "user"`.
3. **Network namespace for QEMU.** Run QEMU in a netns with its own loopback and
   NAT, so `192.168.1.1` is the namespace, not the host. Requires root setup and
   is outside the current CLI.
4. **Tor gateway VM.** Route the guest through a separate Whonix-style gateway.
   This is the strongest anonymity option and the most complex; DNS and
   timezone leaks still need care.
5. **Separate physical host or network.** For malware that may be targeted, a
   dedicated machine on an isolated VLAN is stronger than any in-guest privacy
   tool.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Guest has no link | e1000e driver missing (old Windows) | Use Windows 10/11 or add the driver ISO |
| Guest gets `169.254.x.x` | slirp not running / QEMU network error | Check `qemu.log`, `svm run --debug` |
| DNS fails | `dns` equals `gateway`, or host resolver down | Keep `dns = 192.168.1.2`; test host DNS |
| WARP will not connect | UDP blocked, clock skew, captive portal | Check host network, guest clock, WARP logs |
| Traffic leaks when WARP drops | Lockdown/Always On not enabled | Enable it in the WARP client |
| IPv6 still works | NIC binding not disabled | Disable `ms_tcpip6` / sysctl; keep `ipv6=false` |
| Guest can reach host services | slirp gateway is a host alias | Stop local services, use netns/passt |
| Very slow throughput | slirp is single-threaded | Expected; use passt/virtio-net for performance |

## WARP is not anonymity

WARP hides your IP from the sites you visit, but Cloudflare still sees your
host's public IP and can correlate the device registration. It is a privacy
proxy with a trusted operator, not a mixnet. If your threat model includes
Cloudflare or your ISP correlating traffic, use Tor or a separate network, and
do not rely on WARP alone.

"""Generate a plausible, self-consistent hardware identity per session."""

from __future__ import annotations

import secrets
import string
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ALNUM = string.ascii_uppercase + string.digits
_HOST_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def host_cpu_vendor() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("vendor_id"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "AuthenticAMD"


def host_cpu_signature() -> tuple[int, int, int] | None:
    """Return the host CPU's (family, model, stepping), if available.

    KVM passes the host's microcode revision through to the guest. Picking a
    CPU identity with the same family/model keeps the guest's reported
    microcode revision consistent with the emulated CPU instead of exposing
    the host's revision under an unrelated model.
    """
    fields: dict[str, str] = {}
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
            if all(k in fields for k in ("cpu family", "model", "stepping")):
                break
    except OSError:
        return None
    try:
        return int(fields["cpu family"]), int(fields["model"]), int(fields["stepping"])
    except (KeyError, ValueError):
        return None


def _pick_cpu(hardware: dict[str, Any], vcpus: int) -> tuple[dict[str, Any], str]:
    vendor = "amd" if host_cpu_vendor() == "AuthenticAMD" else "intel"
    pool = hardware.get(vendor, {})
    cpus: list[dict[str, Any]] = pool.get("cpus", [])
    if not cpus:
        raise ValueError(f"no CPU identities configured for vendor '{vendor}'")

    exact = [cpu for cpu in cpus if int(cpu["threads"]) == vcpus]
    if exact:
        host = host_cpu_signature()
        if host is not None:
            same_family_model = [
                cpu
                for cpu in exact
                if int(cpu["family"]) == host[0] and int(cpu["model"]) == host[1]
            ]
            if same_family_model:
                same_stepping = [
                    cpu
                    for cpu in same_family_model
                    if int(cpu["stepping"]) == host[2]
                ]
                return secrets.choice(same_stepping or same_family_model), vendor
        return secrets.choice(exact), vendor

    available = ", ".join(str(n) for n in sorted({int(cpu["threads"]) for cpu in cpus}))
    raise ValueError(
        f"no {vendor} CPU identity with exactly {vcpus} threads "
        f"(available: {available}); add a matching entry to config/hardware.toml"
    )


def _pick_memory(hardware: dict[str, Any], memory_mb: int) -> dict[str, Any]:
    kits: list[dict[str, Any]] = hardware.get("memory", [])
    exact = [kit for kit in kits if int(kit["size_mb"]) == memory_mb]
    if exact:
        return secrets.choice(exact)
    available = ", ".join(str(n) for n in sorted({int(kit["size_mb"]) for kit in kits}))
    raise ValueError(
        f"no memory kit matching {memory_mb} MiB (available: {available}); "
        "add a matching entry to config/hardware.toml"
    )


def _pick_disk(hardware: dict[str, Any], disk_gb: int) -> dict[str, Any]:
    disks: list[dict[str, Any]] = hardware.get("disks", [])
    exact = [disk for disk in disks if int(disk["capacity_gb"]) == disk_gb]
    if exact:
        return secrets.choice(exact)
    available = ", ".join(str(n) for n in sorted({int(disk["capacity_gb"]) for disk in disks}))
    raise ValueError(
        f"no disk model matching {disk_gb} GiB (available: {available}); "
        "add a matching entry to config/hardware.toml"
    )


def _serial(prefix: str = "", length: int = 8) -> str:
    body = "".join(secrets.choice(_ALNUM) for _ in range(length))
    return f"{prefix}{body}"[:20]


def _mac(oui: str) -> str:
    parts = oui.split(":")
    # The OUI (first three octets) comes from the pool and is already unicast
    # and globally administered; only the device-specific tail is random.
    tail = [secrets.randbelow(256) for _ in range(3)]
    return ":".join([p.upper() for p in parts] + [f"{b:02X}" for b in tail])


def _mac_oui(hardware: dict[str, Any], nic: str) -> str:
    """Pick an OUI that matches the emulated NIC vendor.

    An Intel e1000e with a Realtek OUI is a small but free-to-avoid
    inconsistency, so prefer Intel OUIs for Intel NICs.
    """
    ouis = hardware.get("mac_ouis", [])
    if not ouis:
        raise ValueError("no MAC OUIs configured in config/hardware.toml")
    if nic in {"e1000e", "e1000"}:
        intel = [entry for entry in ouis if "intel" in str(entry.get("vendor", "")).lower()]
        if intel:
            ouis = intel
    return str(secrets.choice(ouis)["oui"])


def _hostname(os_name: str, system_uuid: str) -> str:
    """Derive a stable, realistic hostname from the system UUID.

    The Linux guest runs the same derivation at boot, so the hostname it
    reports always matches the QEMU -name and the SMBIOS type 1 UUID.
    """
    compact = system_uuid.replace("-", "").lower()
    if os_name == "windows":
        chars = "".join(_HOST_CHARS[int(ch, 16) % len(_HOST_CHARS)] for ch in compact[:7])
        return "DESKTOP-" + chars
    return "desktop-" + compact[:6]


def generate_identity(
    hardware: dict[str, Any],
    *,
    vcpus: int,
    memory_mb: int,
    disk_gb: int,
    os_name: str = "windows",
    nic: str = "e1000e",
) -> dict[str, Any]:
    cpu, vendor = _pick_cpu(hardware, vcpus)
    pool = hardware[vendor]
    board = secrets.choice(pool["boards"])
    memory = _pick_memory(hardware, memory_mb)
    disk = _pick_disk(hardware, disk_gb)
    mac_oui = _mac_oui(hardware, nic)

    system_uuid = str(uuid.uuid4())
    hostname = _hostname(os_name, system_uuid)
    system_serial = _serial(length=10)
    board_serial = _serial(length=10)
    chassis_serial = _serial(length=10)
    cpu_serial = _serial(length=10)
    memory_serial = _serial(length=10)
    # Real Samsung and WD SATA serials are 15 characters; pad the pool's
    # model-specific prefix to that length instead of a fixed 8-char tail.
    disk_prefix = str(disk["serial_prefix"])
    disk_serial = _serial(prefix=disk_prefix, length=max(4, 15 - len(disk_prefix)))

    identity: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "vendor": vendor,
        "hostname": hostname,
        "uuid": system_uuid,
        "mac": _mac(mac_oui),
        "cpu": {
            "name": cpu["name"],
            "manufacturer": pool["cpu_manufacturer"],
            "socket": pool["socket"],
            "family": int(cpu["family"]),
            "model": int(cpu["model"]),
            "stepping": int(cpu["stepping"]),
            "cores": int(cpu["cores"]),
            "threads": int(cpu["threads"]),
            "base_speed": int(cpu.get("base_speed", cpu["max_speed"])),
            "max_speed": int(cpu["max_speed"]),
            "processor_family": int(cpu["processor_family"]),
            "processor_upgrade": int(cpu.get("processor_upgrade", 0x01)),
            "external_clock": int(cpu.get("external_clock", 100)),
            "voltage": int(cpu.get("voltage", 0x8C)),
            "l1_cache_kb": int(cpu.get("l1_cache_kb", int(cpu["cores"]) * 64)),
            "l2_cache_kb": int(cpu.get("l2_cache_kb", int(cpu["cores"]) * 512)),
            "l3_cache_kb": int(cpu.get("l3_cache_kb", 8192)),
        },
        "board": {
            "manufacturer": board["manufacturer"],
            "product": board["product"],
            "version": board["version"],
            "bios_vendor": board["bios_vendor"],
            "bios_version": board["bios_version"],
            "bios_date": board["bios_date"],
            "family": board["family"],
            "sku": board["sku"],
        },
        "memory": {
            "manufacturer": memory["manufacturer"],
            "part": memory["part"],
            "speed": int(memory["speed"]),
            "size_mb": int(memory["size_mb"]),
            "serial": memory_serial,
        },
        "disk": {
            "model": disk["model"],
            "capacity_gb": int(disk["capacity_gb"]),
            "serial": disk_serial,
            "firmware": disk["firmware"],
        },
        "smbios": {
            "0": {
                "vendor": board["bios_vendor"],
                "version": board["bios_version"],
                "date": board["bios_date"],
                "uefi": "on",
            },
            "1": {
                "manufacturer": board["manufacturer"],
                "product": board["product"],
                "version": board["version"],
                "serial": system_serial,
                "uuid": system_uuid,
                "sku": board["sku"],
                "family": board["family"],
            },
            "2": {
                "manufacturer": board["manufacturer"],
                "product": board["product"],
                "version": board["version"],
                "serial": board_serial,
                "asset": "Default string",
            },
            "3": {
                "manufacturer": board["manufacturer"],
                "version": board["version"],
                "serial": chassis_serial,
                "asset": "Default string",
                "sku": board["sku"],
            },
        },
    }
    return identity


def smbios_args(identity: dict[str, Any]) -> list[str]:
    """Render the identity as a list of -smbios arguments.

    QEMU's -smbios parser is comma-separated, so commas inside values would
    split the option. The -uuid flag already carries the system UUID.  Types 4,
    7, 16 and 17 are supplied as binary structures by smbios_tables.py instead.
    """
    args: list[str] = []
    for smbios_type, fields in identity["smbios"].items():
        if smbios_type in {"4", "7", "16", "17"}:
            continue
        value = f"type={smbios_type}"
        for key, field_value in fields.items():
            if smbios_type == "1" and key == "uuid":
                continue
            safe = " ".join(str(field_value).replace(",", " ").split())
            value += f",{key}={safe}"
        args.extend(["-smbios", value])
    return args


def summary_rows(identity: dict[str, Any]) -> list[tuple[str, str]]:
    cpu = identity["cpu"]
    board = identity["board"]
    disk = identity["disk"]
    memory = identity["memory"]
    return [
        ("Hostname", identity["hostname"]),
        ("UUID", identity["uuid"]),
        ("MAC", identity["mac"]),
        ("CPU", cpu["name"]),
        ("CPU topology", f'{cpu["cores"]}C / {cpu["threads"]}T'),
        ("Board", f'{board["manufacturer"]} {board["product"]}'),
        ("BIOS", f'{board["bios_vendor"]} {board["bios_version"]} ({board["bios_date"]})'),
        ("Disk", f'{disk["model"]} [{disk["serial"]}]'),
        ("Memory", f'{memory["manufacturer"]} {memory["part"]} ({memory["size_mb"] // 1024} GiB)'),
    ]

#!/usr/bin/env python3
"""Print the machine identifiers inside a stopped session's disk (Windows or Linux).

Run it for two sessions of the same base and compare: every identifier below
should differ. Reads the overlay offline (qemu-nbd + a read-only ntfs-3g
mount), so it needs sudo and the guest must be powered off. Developer tool.

    scripts/freshness.py <session-id> [<session-id> ...]

Needs the `python-registry` package (pip install python-registry).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    from Registry import Registry
except ImportError:  # pragma: no cover
    sys.exit("pip install python-registry")

ROOT = Path(__file__).resolve().parent.parent
QEMU_NBD = ROOT / "runtime/qemu/bin/qemu-nbd"
NBD = "/dev/nbd2"
MNT = Path("/mnt/svm-freshness")


def sh(*args: str, check: bool = True) -> str:
    result = subprocess.run(["sudo", "-n", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def value(hive: Path, path: str, name: str) -> str:
    try:
        key = Registry.Registry(str(hive)).open(path)
        return str(key.value(name).value())
    except Exception as exc:  # noqa: BLE001 - report, keep going
        return f"<{type(exc).__name__}>"


def subkeys(hive: Path, path: str) -> list[str]:
    try:
        return sorted(k.name() for k in Registry.Registry(str(hive)).open(path).subkeys())
    except Exception:  # noqa: BLE001
        return []


def inspect_linux() -> dict[str, object]:
    """Identifiers of an Ubuntu session (root filesystem is partition 2)."""
    sh("mount", "-o", "ro,noload", f"{NBD}p2", str(MNT))
    try:
        def cat(path: str) -> str:
            return sh("cat", str(MNT / path)).strip() or "<empty>"

        def exists(path: str) -> str:
            return "present" if (MNT / path).exists() else "absent"

        warp_dir = MNT / "var/lib/cloudflare-warp"
        warp = "<none>"
        reg = warp_dir / "reg.json"
        if sh("test", "-e", str(reg), check=False) == "" and sh("ls", str(warp_dir), check=False):
            text = sh("cat", str(reg), check=False)
            ids = re.findall(r'"(?:device_id|registration_id|id)"\s*:\s*"([^"]+)"', text)
            warp = ",".join(ids) or "reg.json " + str(len(text)) + " bytes"
        ssh_keys = sh("sh", "-c", f"ls {MNT}/etc/ssh 2>/dev/null | grep -c host_ || true").strip()
        return {
            "machine_id": cat("etc/machine-id"),
            "hostname": cat("etc/hostname"),
            "root_fs_uuid": sh("blkid", "-s", "UUID", "-o", "value", f"{NBD}p2").strip(),
            "esp_serial": sh("blkid", "-s", "UUID", "-o", "value", f"{NBD}p1").strip(),
            "ssh_host_keys": ssh_keys,
            "warp_registration": warp,
            "users": ",".join(sorted(p.name for p in (MNT / "home").iterdir())),
        }
    finally:
        pass


def inspect(overlay: Path) -> dict[str, object]:
    sh("modprobe", "nbd", "max_part=16")
    sh(str(QEMU_NBD), "-r", f"--connect={NBD}", str(overlay))
    try:
        time.sleep(1)
        sh("mkdir", "-p", str(MNT))
        if "ntfs" not in sh("blkid", "-s", "TYPE", "-o", "value", f"{NBD}p3", check=False):
            try:
                return inspect_linux()
            finally:
                sh("umount", str(MNT), check=False)
        sh("mount", "-t", "ntfs-3g", "-o", "ro", f"{NBD}p3", str(MNT))
        try:
            serial = sh("blkid", "-s", "UUID", "-o", "value", f"{NBD}p3").strip()
            cfg = MNT / "Windows/System32/config"
            # Copy the hives out: they are root-owned on the mount.
            tmp = Path("/tmp/svm-hives")
            sh("rm", "-rf", str(tmp))
            sh("mkdir", "-p", str(tmp))
            for name in ("SOFTWARE", "SYSTEM"):
                sh("cp", str(cfg / name), str(tmp / name))
            sh("chmod", "-R", "a+rX", str(tmp))
            software, system = tmp / "SOFTWARE", tmp / "SYSTEM"
            nt = r"Microsoft\Windows NT\CurrentVersion"
            profiles = subkeys(software, nt + r"\ProfileList")
            sids = [p for p in profiles if p.startswith("S-1-5-21-")]
            machine_sid = re.sub(r"-\d+$", "", sids[0]) if sids else "<none>"
            warp_dir = MNT / "ProgramData/Cloudflare"
            warp = "<none>"
            conf = warp_dir / "conf.json"
            if conf.exists():
                text = sh("cat", str(conf))
                ids = re.findall(r'"(?:id|device_id|registration_id)"\s*:\s*"([^"]+)"', text)
                warp = ",".join(ids) or f"conf.json {len(text)} bytes"
            users = sorted(p.name for p in (MNT / "Users").iterdir())
            return {
                "ntfs_volume_serial": serial,
                "machine_guid": value(software, r"Microsoft\Cryptography", "MachineGuid"),
                "machine_sid": machine_sid,
                "computer_name": value(
                    system, r"ControlSet001\Control\ComputerName\ComputerName", "ComputerName"
                ),
                "install_date": value(software, nt, "InstallDate"),
                "product_id": value(software, nt, "ProductId"),
                "sqm_machine_id": value(software, r"Microsoft\SQMClient", "MachineId"),
                "hw_profile_guid": value(
                    system, r"ControlSet001\Control\IDConfigDB\Hardware Profiles\0001", "HwProfileGuid"
                ),
                "tcpip_interfaces": ",".join(
                    subkeys(system, r"ControlSet001\Services\Tcpip\Parameters\Interfaces")
                ),
                "warp_registration": warp,
                "users": ",".join(users),
            }
        finally:
            sh("umount", str(MNT), check=False)
    finally:
        sh(str(QEMU_NBD), "--disconnect", NBD, check=False)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    reports: dict[str, dict[str, object]] = {}
    for sid in sys.argv[1:]:
        overlay = ROOT / "vms" / sid / "disk.qcow2"
        if not overlay.is_file():
            matches = sorted((ROOT / "vms").glob(sid + "*/disk.qcow2"))
            if len(matches) != 1:
                sys.exit(f"no unique session for {sid!r}")
            overlay = matches[0]
        reports[overlay.parent.name] = inspect(overlay)
    print(json.dumps(reports, indent=2))
    if len(reports) > 1:
        names = list(reports)
        keys = list(next(iter(reports.values())))
        print("\nidentifier              status")
        for key in keys:
            vals = {str(reports[n][key]) for n in names}
            status = "DIFFERENT" if len(vals) == len(names) else "SAME  <-- shared"
            print(f"{key:<23} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

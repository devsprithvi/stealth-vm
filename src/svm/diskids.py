"""Give a new session's disk fresh filesystem serial numbers.

Every session is an overlay on a shared base, so the NTFS volume serial and the
FAT volume ID of the EFI system partition would be identical in every run.
Malware commonly reads the C: volume serial (``GetVolumeInformation``) as a
machine identifier, so those two values are rewritten in the overlay before the
guest first boots. The base is never touched.

No root and no kernel nbd device are needed: ``qemu-nbd`` serves the overlay on
a Unix socket and a minimal NBD client below patches the boot sectors.

Not changed on purpose:

* GPT disk and partition GUIDs. Windows' BCD stores them, so changing them
  without rewriting the BCD makes the guest unbootable.
* ext4 UUIDs. ``fstab`` and GRUB reference them by value.
"""

from __future__ import annotations

import secrets
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

SECTOR = 512

_NBD_MAGIC = b"NBDMAGIC"
_NBD_IHAVEOPT = b"IHAVEOPT"
_NBD_REQUEST_MAGIC = 0x25609513
_NBD_REPLY_MAGIC = 0x67446698
_NBD_CMD_READ = 0
_NBD_CMD_WRITE = 1
_NBD_CMD_DISC = 2
_NBD_CMD_FLUSH = 3
_NBD_OPT_EXPORT_NAME = 1
_NBD_FLAG_C_FIXED_NEWSTYLE = 1
_NBD_FLAG_C_NO_ZEROES = 2
_NBD_FLAG_NO_ZEROES = 2

_EFI_SYSTEM_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
_BASIC_DATA_GUID = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"


class DiskIdError(RuntimeError):
    """The overlay could not be inspected or patched."""


class NbdClient:
    """Just enough of the NBD protocol (newstyle, EXPORT_NAME) to read/write."""

    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(str(path))
        self._handle = 0
        self.size = self._handshake()

    def _recv(self, count: int) -> bytes:
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise DiskIdError("NBD server closed the connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _handshake(self) -> int:
        if self._recv(8) != _NBD_MAGIC or self._recv(8) != _NBD_IHAVEOPT:
            raise DiskIdError("not an NBD newstyle server")
        (server_flags,) = struct.unpack(">H", self._recv(2))
        client_flags = _NBD_FLAG_C_FIXED_NEWSTYLE
        no_zeroes = bool(server_flags & _NBD_FLAG_NO_ZEROES)
        if no_zeroes:
            client_flags |= _NBD_FLAG_C_NO_ZEROES
        self._sock.sendall(struct.pack(">I", client_flags))
        # NBD_OPT_EXPORT_NAME with the default (empty) export.
        self._sock.sendall(
            _NBD_IHAVEOPT + struct.pack(">II", _NBD_OPT_EXPORT_NAME, 0)
        )
        size, _transmission_flags = struct.unpack(">QH", self._recv(10))
        if not no_zeroes:
            self._recv(124)
        return size

    def _request(self, command: int, offset: int, length: int, payload: bytes = b"") -> bytes:
        self._handle += 1
        self._sock.sendall(
            struct.pack(
                ">IHHQQI", _NBD_REQUEST_MAGIC, 0, command, self._handle, offset, length
            )
            + payload
        )
        magic, error, handle = struct.unpack(">IIQ", self._recv(16))
        if magic != _NBD_REPLY_MAGIC or handle != self._handle:
            raise DiskIdError("unexpected NBD reply")
        if error:
            raise DiskIdError(f"NBD error {error} at offset {offset}")
        return self._recv(length) if command == _NBD_CMD_READ else b""

    def read(self, offset: int, length: int) -> bytes:
        return self._request(_NBD_CMD_READ, offset, length)

    def write(self, offset: int, data: bytes) -> None:
        self._request(_NBD_CMD_WRITE, offset, len(data), data)

    def flush(self) -> None:
        self._request(_NBD_CMD_FLUSH, 0, 0)

    def close(self) -> None:
        try:
            self._handle += 1
            self._sock.sendall(
                struct.pack(">IHHQQI", _NBD_REQUEST_MAGIC, 0, _NBD_CMD_DISC, self._handle, 0, 0)
            )
        except OSError:
            pass
        self._sock.close()


def _guid(raw: bytes) -> str:
    """Render a GPT mixed-endian GUID."""
    a, b, c = struct.unpack("<IHH", raw[:8])
    return f"{a:08x}-{b:04x}-{c:04x}-{raw[8:10].hex()}-{raw[10:16].hex()}"


def _partitions(disk: NbdClient) -> list[tuple[int, int, str]]:
    """Return (first_lba, last_lba, type_guid) for each used GPT entry."""
    header = disk.read(SECTOR, 92)
    if header[:8] != b"EFI PART":
        raise DiskIdError("no GPT header on the overlay")
    entries_lba, count, entry_size = struct.unpack("<QII", header[72:88])
    raw = disk.read(entries_lba * SECTOR, count * entry_size)
    found: list[tuple[int, int, str]] = []
    for index in range(count):
        entry = raw[index * entry_size : (index + 1) * entry_size]
        if entry[:16] == b"\0" * 16:
            continue
        first, last = struct.unpack("<QQ", entry[32:48])
        found.append((first, last, _guid(entry[:16])))
    return found


def _patch_ntfs(disk: NbdClient, first: int, last: int) -> str | None:
    boot = disk.read(first * SECTOR, SECTOR)
    if boot[3:11] != b"NTFS    ":
        return None
    bytes_per_sector = struct.unpack("<H", boot[11:13])[0]
    if bytes_per_sector != SECTOR:
        raise DiskIdError(f"unsupported NTFS sector size {bytes_per_sector}")
    total_sectors = struct.unpack("<Q", boot[0x28:0x30])[0]
    # The backup boot sector is the last sector of the volume.
    backup_lba = first + total_sectors
    if backup_lba > last:
        raise DiskIdError("NTFS backup boot sector lies outside the partition")
    backup = disk.read(backup_lba * SECTOR, SECTOR)
    serial = secrets.token_bytes(8)
    patched = boot[:0x48] + serial + boot[0x50:]
    disk.write(first * SECTOR, patched)
    if backup == boot:
        disk.write(backup_lba * SECTOR, patched)
    return serial[::-1].hex().upper()  # shown like `vol`/`fsutil`: big-endian


def _patch_fat32(disk: NbdClient, first: int) -> str | None:
    boot = disk.read(first * SECTOR, SECTOR)
    if boot[0x52:0x5A] != b"FAT32   " or boot[510:512] != b"\x55\xaa":
        return None
    backup_sector = struct.unpack("<H", boot[0x32:0x34])[0]
    serial = secrets.token_bytes(4)
    patched = boot[:0x43] + serial + boot[0x47:]
    backup = b""
    if backup_sector:
        backup = disk.read((first + backup_sector) * SECTOR, SECTOR)
    disk.write(first * SECTOR, patched)
    if backup_sector and backup == boot:
        disk.write((first + backup_sector) * SECTOR, patched)
    return f"{serial[3]:02X}{serial[2]:02X}-{serial[1]:02X}{serial[0]:02X}"


def randomize_volume_ids(disk: NbdClient) -> dict[str, str]:
    """Rewrite the NTFS serial and the ESP's FAT32 ID. Returns what was set."""
    result: dict[str, str] = {}
    for first, last, type_guid in _partitions(disk):
        if type_guid == _BASIC_DATA_GUID:
            serial = _patch_ntfs(disk, first, last)
            if serial:
                result["ntfs_serial"] = serial
        elif type_guid == _EFI_SYSTEM_GUID:
            serial = _patch_fat32(disk, first)
            if serial:
                result["esp_serial"] = serial
    disk.flush()
    return result


def randomize_overlay_ids(qemu_nbd: Path, overlay: Path) -> dict[str, Any]:
    """Serve ``overlay`` over a private Unix socket and randomize its IDs."""
    sock_path = overlay.with_name("diskids.sock")
    sock_path.unlink(missing_ok=True)
    server = subprocess.Popen(
        [
            str(qemu_nbd),
            "--format=qcow2",
            f"--socket={sock_path}",
            "--cache=writeback",
            str(overlay),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    client: NbdClient | None = None
    try:
        deadline = time.monotonic() + 10
        while not sock_path.exists():
            if server.poll() is not None:
                err = server.stderr.read().decode(errors="replace") if server.stderr else ""
                raise DiskIdError(f"qemu-nbd failed to start: {err.strip()}")
            if time.monotonic() > deadline:
                raise DiskIdError("qemu-nbd did not create its socket")
            time.sleep(0.05)
        client = NbdClient(sock_path)
        return randomize_volume_ids(client)
    finally:
        if client is not None:
            client.close()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        sock_path.unlink(missing_ok=True)

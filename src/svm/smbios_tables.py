"""Build per-session SMBIOS structures that QEMU has no CLI support for.

QEMU's ``-smbios`` parser only exposes a small subset of the SMBIOS fields.
In particular it cannot describe cache tables (type 7), a multi-slot memory
array (type 16), an AM4 socket / cache handles (type 4), or a complete DDR4
DIMM (type 17).  Loading a binary structure with ``-smbios file=`` makes QEMU
skip its own table for that type, so we can provide complete, self-consistent
tables instead of patching QEMU.

The file format is the exact SMBIOS wire format: a fixed header followed by a
string-set terminated by two NUL bytes.  Each file contains exactly one
structure because QEMU only records the first structure's type as supplied.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

# Fixed handles keep the cross-references simple and avoid collisions with the
# handles QEMU chooses for types 0-3, 19 and 32.
T4_HANDLE = 0x0400
T7_L1_HANDLE = 0x0700
T7_L2_HANDLE = 0x0701
T7_L3_HANDLE = 0x0702
T16_HANDLE = 0x1000
T17_HANDLE = 0x1100

# Lengths include the four-byte structure header.
T4_LEN = 0x30  # SMBIOS 3.0 processor information
T7_LEN = 0x1B  # SMBIOS 3.1 cache information (with extended sizes)
T16_LEN = 0x17  # SMBIOS 2.7 physical memory array
T17_LEN = 0x28  # SMBIOS 2.8 memory device


def _cpuid_eax(family: int, model: int, stepping: int) -> int:
    """Encode an x86 family/model/stepping the way CPUID leaf 1 does."""
    base_family = family if family < 0x0F else 0x0F
    ext_family = family - 0x0F if family > 0x0F else 0
    base_model = model & 0x0F
    ext_model = model >> 4
    return (
        (ext_family << 20)
        | (ext_model << 16)
        | (base_family << 8)
        | (base_model << 4)
        | stepping
    )


def _strings(*values: str) -> tuple[bytes, dict[str, int]]:
    """Return an SMBIOS string-set and the 1-based index of each value."""
    table: dict[str, int] = {}
    chunks: list[bytes] = []
    for value in values:
        if value not in table:
            table[value] = len(chunks) + 1
            chunks.append(value.encode("ascii", "replace") + b"\0")
    return b"".join(chunks) + b"\0", table


def _structure(stype: int, handle: int, fields: bytes, strings: bytes) -> bytes:
    # SMBIOS requires the string-set to end with two NULs (a structure with no
    # strings is just b"\0\0"). One NUL desynchronises every parser that walks
    # the table, so everything after the structure is silently lost.
    if not strings.endswith(b"\0\0"):
        raise ValueError(f"SMBIOS type {stype}: string-set must end with two NULs")
    length = 4 + len(fields)
    header = struct.pack("<BBH", stype, length, handle)
    return header + fields + strings


def _cache_size16(size_kb: int) -> int:
    """Encode a cache size in the 16-bit SMBIOS field.

    Values below 32 MiB use 1 KiB units.  Larger values set bit 15 and use
    64 KiB units, as required by the SMBIOS specification.
    """
    if size_kb < 0x8000:
        return size_kb
    units = (size_kb + 63) // 64
    if units > 0x7FFF:
        raise ValueError(f"cache too large for SMBIOS type 7: {size_kb} KiB")
    return 0x8000 | units


def _cache(
    handle: int,
    name: str,
    level: int,
    size_kb: int,
    *,
    associativity: int,
) -> bytes:
    # Enabled, write-back, internal, not socketed.  The configuration word is
    # level | (enabled << 7) | (write-back << 8).
    config = level | (1 << 7) | (1 << 8)
    size16 = _cache_size16(size_kb)
    fields = struct.pack(
        "<BHHHHHBBBBII",
        1,  # socket designation (string 1)
        config,
        size16,
        size16,
        0x0010,  # supported SRAM: pipeline burst
        0x0010,  # current SRAM: pipeline burst
        1,  # cache speed in ns
        0x06,  # error correction: multi-bit ECC (matches the host firmware)
        0x05,  # system cache type: unified
        associativity,
        size_kb,
        size_kb,
    )
    strings, _ = _strings(name)
    return _structure(7, handle, fields, strings)


def type7_tables(cpu: dict[str, Any]) -> dict[str, bytes]:
    cores = int(cpu["cores"])
    l1 = int(cpu.get("l1_cache_kb", cores * 64))
    l2 = int(cpu.get("l2_cache_kb", cores * 512))
    l3 = int(cpu.get("l3_cache_kb", 8192))
    return {
        "type7-l1.bin": _cache(
            T7_L1_HANDLE, "L1 - Cache", 1, l1, associativity=0x07
        ),
        "type7-l2.bin": _cache(
            T7_L2_HANDLE, "L2 - Cache", 2, l2, associativity=0x07
        ),
        "type7-l3.bin": _cache(
            T7_L3_HANDLE, "L3 - Cache", 3, l3, associativity=0x08
        ),
    }


def type4_table(identity: dict[str, Any]) -> bytes:
    cpu = identity["cpu"]
    family = int(cpu["family"])
    model = int(cpu["model"])
    stepping = int(cpu["stepping"])
    cores = int(cpu["cores"])
    threads = int(cpu["threads"])
    vendor = str(cpu["manufacturer"])
    version = str(cpu["name"])
    socket = str(cpu.get("socket", "CPU"))
    serial = str(identity.get("cpu_serial", "Not Specified"))
    asset = "Not Specified"
    max_speed = int(cpu.get("max_speed", 0))
    current_speed = int(cpu.get("base_speed", max_speed))
    upgrade = int(cpu.get("processor_upgrade", 0x01))
    external_clock = int(cpu.get("external_clock", 100))
    voltage = int(cpu.get("voltage", 0x8C))
    processor_family = int(cpu.get("processor_family", 0x6B))
    characteristics = 0x00FC  # 64-bit, multi-core, HT, NX, SVM, power control
    features = 0x178BFBFF if identity.get("vendor") == "amd" else 0xBFEBFBFF
    processor_id = _cpuid_eax(family, model, stepping)

    # SMBIOS type 4 uses 0xFE when the family does not fit in the first byte;
    # modern AMD/Intel families fit, and real firmware puts the value in both
    # the legacy byte and the SMBIOS 2.6+ family2 field.
    family_byte = processor_family if processor_family < 0x100 else 0xFE

    strings, idx = _strings(socket, vendor, version, serial, asset, version)
    fields = struct.pack(
        "<BBBBIIBBHHHBBHHHBBBBBBHHHHH",
        idx[socket],
        0x03,  # central processor
        family_byte,
        idx[vendor],
        processor_id,  # Processor ID low dword: EAX from CPUID leaf 1
        features,  # Processor ID high dword: EDX from CPUID leaf 1
        idx[version],
        voltage,
        external_clock,
        max_speed,
        current_speed,
        0x41,  # populated, enabled
        upgrade,
        T7_L1_HANDLE,
        T7_L2_HANDLE,
        T7_L3_HANDLE,
        idx[serial],
        idx[asset],
        idx[version],
        cores & 0xFF,
        cores & 0xFF,
        threads & 0xFF,
        characteristics,
        processor_family,
        cores,
        cores,
        threads,
    )
    assert len(fields) == T4_LEN - 4, len(fields)
    return _structure(4, T4_HANDLE, fields, strings)


def type16_table(identity: dict[str, Any]) -> bytes:
    # A B550-class board has four DIMM slots and accepts 128 GiB.  Keeping the
    # array larger than the installed DIMM avoids the "max capacity equals
    # installed RAM" VM fingerprint.
    max_capacity_kb = 128 * 1024 * 1024
    fields = struct.pack(
        "<BBBIHHQ",
        0x03,  # location: system board
        0x03,  # use: system memory
        0x03,  # error correction: none (DDR4 non-ECC)
        max_capacity_kb,
        0xFFFE,  # no error information
        4,  # DIMM slots
        0,  # extended maximum capacity (not used)
    )
    return _structure(16, T16_HANDLE, fields, b"\0\0")


def type17_table(identity: dict[str, Any]) -> bytes:
    memory = identity["memory"]
    size_mb = int(memory["size_mb"])
    strings, idx = _strings(
        "DIMM_A1",
        "BANK 0",
        str(memory["manufacturer"]),
        str(memory["serial"]),
        "Not Specified",
        str(memory["part"]),
    )
    fields = struct.pack(
        "<HHHHHBBBBBHHBBBBBIHHHH",
        T16_HANDLE,
        0xFFFE,  # no error information
        64,
        64,
        size_mb,
        0x09,  # DIMM
        0,  # not part of a set
        idx["DIMM_A1"],
        idx["BANK 0"],
        0x1A,  # DDR4
        0x0080,  # synchronous
        int(memory["speed"]),
        idx[str(memory["manufacturer"])],
        idx[str(memory["serial"])],
        idx["Not Specified"],
        idx[str(memory["part"])],
        0x01,  # single rank
        0,  # no extended size
        int(memory["speed"]),
        1200,
        1200,
        1200,
    )
    assert len(fields) == T17_LEN - 4, len(fields)
    return _structure(17, T17_HANDLE, fields, strings)


def write_tables(directory: Path, identity: dict[str, Any]) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes] = {
        "type4.bin": type4_table(identity),
        **type7_tables(identity["cpu"]),
        "type16.bin": type16_table(identity),
        "type17.bin": type17_table(identity),
    }
    written: list[Path] = []
    for name, data in files.items():
        path = directory / name
        path.write_bytes(data)
        written.append(path)
    return written


def file_args(directory: Path) -> list[str]:
    args: list[str] = []
    for path in sorted(directory.glob("*.bin")):
        args += ["-smbios", f"file={path}"]
    return args

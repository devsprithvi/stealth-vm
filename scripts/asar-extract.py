#!/usr/bin/env python3
"""Dependency-free Electron .asar extractor/grepper.

Used for static analysis of Electron apps before deciding what a stealth-vm
session needs to defend against (see docs/APP-ANALYSIS.md). No Node.js or
`npm install asar` required: the asar container is just
[Chromium Pickle framing][JSON header][concatenated file bytes], and the
header is always plain ASCII JSON, so we find it by scanning for the leading
'{' and let json.JSONDecoder.raw_decode tell us exactly where it ends.

Usage:
  asar-extract.py extract <app.asar> <out_dir>
  asar-extract.py grep <app.asar> <needle> [needle ...]   # case-insensitive substring scan
"""
import json
import os
import sys


def load_header(data: bytes):
    start = data.index(b"{")
    assert start < 32, f"unexpected header start offset {start}"
    text = data[start:start + 20_000_000].decode("utf-8")
    header, end = json.JSONDecoder().raw_decode(text)
    header_end_abs = start + end
    # File data starts at the next 4-byte-aligned offset after the header.
    data_start = (header_end_abs + 3) & ~3
    return header, data_start


def walk(node, prefix=""):
    files = node.get("files")
    if files is None:
        return
    for name, child in files.items():
        path = f"{prefix}/{name}" if prefix else name
        if "files" in child:
            yield from walk(child, path)
        else:
            yield path, child


def extract(asar_path, out_dir=None, grep_needles=None):
    with open(asar_path, "rb") as f:
        data = f.read()
    header, data_start = load_header(data)
    count = 0
    matches = []
    for path, meta in walk(header):
        count += 1
        if meta.get("unpacked"):
            continue
        size = meta["size"]
        offset = int(meta["offset"])
        abs_off = data_start + offset
        content = data[abs_off:abs_off + size]
        if grep_needles is not None:
            low = content.lower()
            if any(n in low for n in grep_needles):
                matches.append(path)
            continue
        out_path = os.path.join(out_dir, path)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as out:
            out.write(content)
    return count, matches


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    mode, asar_path = sys.argv[1], sys.argv[2]
    if mode == "extract":
        n, _ = extract(asar_path, out_dir=sys.argv[3])
        print(f"extracted {n} files to {sys.argv[3]}")
    elif mode == "grep":
        needles = [s.lower().encode() for s in sys.argv[3:]]
        n, matches = extract(asar_path, grep_needles=needles)
        print(f"scanned {n} files, {len(matches)} matched")
        for m in matches:
            print(m)
    else:
        print(__doc__)
        sys.exit(1)

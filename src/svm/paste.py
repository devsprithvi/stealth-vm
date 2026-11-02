"""Stealth host-to-guest text injection via emulated PS/2 hardware keystrokes.

This simulates physical keypresses on the emulated i8042 PS/2 keyboard.
Because it uses standard hardware keyboard interrupts and scancodes,
the guest sees authentic physical typing with zero VirtIO devices,
zero guest agents, and zero anti-cheat/anti-VM detection risk.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

# Map of ASCII character -> (qcode_key_name, needs_shift)
_KEY_MAP: dict[str, tuple[str, bool]] = {
    # Letters
    "a": ("a", False), "b": ("b", False), "c": ("c", False), "d": ("d", False),
    "e": ("e", False), "f": ("f", False), "g": ("g", False), "h": ("h", False),
    "i": ("i", False), "j": ("j", False), "k": ("k", False), "l": ("l", False),
    "m": ("m", False), "n": ("n", False), "o": ("o", False), "p": ("p", False),
    "q": ("q", False), "r": ("r", False), "s": ("s", False), "t": ("t", False),
    "u": ("u", False), "v": ("v", False), "w": ("w", False), "x": ("x", False),
    "y": ("y", False), "z": ("z", False),
    "A": ("a", True), "B": ("b", True), "C": ("c", True), "D": ("d", True),
    "E": ("e", True), "F": ("f", True), "G": ("g", True), "H": ("h", True),
    "I": ("i", True), "J": ("j", True), "K": ("k", True), "L": ("l", True),
    "M": ("m", True), "N": ("n", True), "O": ("o", True), "P": ("p", True),
    "Q": ("q", True), "R": ("r", True), "S": ("s", True), "T": ("t", True),
    "U": ("u", True), "V": ("v", True), "W": ("w", True), "X": ("x", True),
    "Y": ("y", True), "Z": ("z", True),
    # Digits
    "0": ("0", False), "1": ("1", False), "2": ("2", False), "3": ("3", False),
    "4": ("4", False), "5": ("5", False), "6": ("6", False), "7": ("7", False),
    "8": ("8", False), "9": ("9", False),
    # Shifted digits / symbols
    "!": ("1", True), "@": ("2", True), "#": ("3", True), "$": ("4", True),
    "%": ("5", True), "^": ("6", True), "&": ("7", True), "*": ("8", True),
    "(": ("9", True), ")": ("0", True),
    # Whitespace & control
    " ": ("spc", False),
    "\t": ("tab", False),
    "\n": ("ret", False),
    # Punctuation & symbols
    "-": ("minus", False), "_": ("minus", True),
    "=": ("equal", False), "+": ("equal", True),
    "[": ("bracket_left", False), "{": ("bracket_left", True),
    "]": ("bracket_right", False), "}": ("bracket_right", True),
    "\\": ("backslash", False), "|": ("backslash", True),
    ";": ("semicolon", False), ":": ("semicolon", True),
    "'": ("apostrophe", False), '"': ("apostrophe", True),
    ",": ("comma", False), "<": ("comma", True),
    ".": ("dot", False), ">": ("dot", True),
    "/": ("slash", False), "?": ("slash", True),
    "`": ("grave_accent", False), "~": ("grave_accent", True),
}

# Normalization for common unicode characters that user copies from browsers/docs
_UNICODE_REPLACEMENTS: dict[str, str] = {
    "\u00a0": " ",      # non-breaking space
    "\u2018": "'",      # left single quotation mark
    "\u2019": "'",      # right single quotation mark
    "\u201c": '"',      # left double quotation mark
    "\u201d": '"',      # right double quotation mark
    "\u2013": "-",      # en dash
    "\u2014": "-",      # em dash
    "\u2026": "...",    # horizontal ellipsis
}


def normalize_text(text: str) -> str:
    """Normalize text into typable ASCII characters."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for orig, repl in _UNICODE_REPLACEMENTS.items():
        text = text.replace(orig, repl)
    return text


def read_host_clipboard() -> str:
    """Read the current text from the host clipboard or primary selection using wl-paste or xclip."""
    if shutil.which("wl-paste"):
        # Try clipboard selection first
        for args in (["wl-paste", "--no-newline"], ["wl-paste", "--primary", "--no-newline"]):
            try:
                result = subprocess.run(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=2.0,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
            except (subprocess.TimeoutExpired, OSError):
                pass

    if shutil.which("xclip"):
        # Try clipboard selection first, then primary
        for sel in ("clipboard", "primary"):
            try:
                result = subprocess.run(
                    ["xclip", "-selection", sel, "-o"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=2.0,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
            except (subprocess.TimeoutExpired, OSError):
                pass

    return ""


def send_text_as_keystrokes(
    qmp_sock: Path,
    text: str,
    delay_ms: float = 15.0,
    hold_time_ms: int = 12,
) -> int:
    """Send text into the VM via QMP send-key as PS/2 hardware keystrokes.

    Uses an explicit hold-time and inter-key delay so that shifted characters
    (like :, ?, uppercase letters) and symbols never bleed into subsequent keys
    or overflow the emulated PS/2 controller buffer.
    """
    if not qmp_sock.exists():
        raise FileNotFoundError(f"QMP socket does not exist: {qmp_sock}")

    text = normalize_text(text)
    delay_sec = max(0.005, delay_ms / 1000.0)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10.0)
        conn.connect(str(qmp_sock))
        stream = conn.makefile("rwb")
        stream.readline()  # QMP greeting
        stream.write(b'{"execute":"qmp_capabilities"}\n')
        stream.flush()
        stream.readline()  # capabilities return

        count = 0
        for ch in text:
            mapping = _KEY_MAP.get(ch)
            if mapping is None:
                continue

            qcode, need_shift = mapping
            keys: list[dict[str, str]] = []
            if need_shift:
                keys.append({"type": "qcode", "data": "shift"})
            keys.append({"type": "qcode", "data": qcode})

            payload = json.dumps({
                "execute": "send-key",
                "arguments": {
                    "keys": keys,
                    "hold-time": hold_time_ms,
                },
            })
            stream.write(payload.encode("utf-8") + b"\n")
            stream.flush()
            stream.readline()
            count += 1
            if delay_sec > 0:
                time.sleep(delay_sec)

        return count

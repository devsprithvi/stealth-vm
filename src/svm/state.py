"""Session and base-image state.

A session is one directory under vms/.  Everything the guest writes lives in
that directory (overlay disk, NVRAM, TPM state, sockets, logs), so destroying
the session is a single rmtree.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .identity import generate_identity


class StateError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _qemu_img(cfg: Config, args: list[str]) -> None:
    if cfg.paths.qemu_img is None:
        raise StateError("qemu-img not found. Run: sudo ./bootstrap.sh")
    result = subprocess.run(
        [str(cfg.paths.qemu_img), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise StateError(
            f"qemu-img {' '.join(args)} failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def _safe_component(value: str, what: str) -> str:
    """Reject path separators and traversal in user-supplied names."""
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\0" in value
    ):
        raise StateError(f"invalid {what}: {value!r}")
    return value


@dataclass
class Session:
    dir: Path
    data: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.data["id"])

    @property
    def disk(self) -> Path:
        return Path(self.data["paths"]["disk"])

    @property
    def vars(self) -> Path:
        return Path(self.data["paths"]["vars"])

    @property
    def hw_config(self) -> Path:
        return Path(self.data["paths"]["hw_config"])

    @property
    def spice(self) -> Path:
        return Path(self.data["paths"]["spice"])

    @property
    def qmp(self) -> Path:
        return Path(self.data["paths"]["qmp"])

    @property
    def pidfile(self) -> Path:
        return Path(self.data["paths"]["pid"])

    @property
    def log(self) -> Path:
        return Path(self.data["paths"]["log"])

    @property
    def tpm_dir(self) -> Path | None:
        value = self.data["paths"].get("tpm")
        return Path(value) if value else None

    @property
    def kind(self) -> str:
        return str(self.data.get("kind", "run"))

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / "session.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        os.chmod(path, 0o600)

    def pid(self) -> int | None:
        try:
            return int(self.pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def is_running(self) -> bool:
        pid = self.pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            return True
        return b"qemu-system" in cmdline

    def size_bytes(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self.dir):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
        return total

    def age(self) -> str:
        try:
            created = datetime.fromisoformat(str(self.data["created_at"]))
        except (KeyError, ValueError):
            return "?"
        delta = datetime.now(timezone.utc) - created
        seconds = max(0, int(delta.total_seconds()))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"

    def created_timestamp(self) -> float:
        try:
            created = datetime.fromisoformat(str(self.data["created_at"]))
        except (KeyError, ValueError):
            try:
                return self.dir.stat().st_mtime
            except OSError:
                return time.time()
        return created.timestamp()


def _paths_for(cfg: Config, session_id: str) -> dict[str, str]:
    directory = cfg.paths.vms / session_id
    return {
        "dir": str(directory),
        "disk": str(directory / "disk.qcow2"),
        "vars": str(directory / "vars.fd"),
        "hw_config": str(directory / "hardware.cfg"),
        "spice": str(directory / "spice.sock"),
        "qmp": str(directory / "qmp.sock"),
        "pid": str(directory / "qemu.pid"),
        "log": str(directory / "qemu.log"),
        "tpm": str(directory / "tpm"),
    }


def _resources(profile: dict[str, Any]) -> tuple[int, int, int]:
    """Return (vcpus, memory_mb, disk_gb).

    disk_gb is decimal GB, matching the labelled capacity of the emulated
    SSD. The guest sees ~465.7 GiB for a 500 GB disk, exactly like hardware.
    """
    resources = profile.get("resources", {})
    return (
        int(resources.get("vcpus", 8)),
        int(resources.get("memory_mb", 8192)),
        int(resources.get("disk_gb", 500)),
    )


def new_session(
    cfg: Config,
    profile_name: str,
    base_name: str,
    *,
    dry_run: bool = False,
) -> Session:
    profile = cfg.profile(profile_name)
    base = load_base(cfg, base_name)
    profile_os = str(profile.get("os", "windows"))
    base_os = str(base.get("os", profile_os))
    if base_os != profile_os:
        raise StateError(
            f"base '{base_name}' is for {base_os}, but profile "
            f"'{profile_name}' is {profile_os}"
        )
    hardware = profile.get("hardware", {})
    base_secure_boot = bool(base.get("secure_boot", False))
    profile_secure_boot = bool(hardware.get("secure_boot", False))
    if profile_secure_boot != base_secure_boot:
        raise StateError(
            f"base '{base_name}' secure_boot={base_secure_boot}, but profile "
            f"'{profile_name}' secure_boot={profile_secure_boot}; use a matching base"
        )
    if bool(hardware.get("tpm", False)) and not base.get("tpm"):
        raise StateError(
            f"base '{base_name}' has no TPM state, but profile "
            f"'{profile_name}' requires a TPM; recreate the base with this profile"
        )
    session_id = new_session_id()
    directory = cfg.paths.vms / session_id
    paths = _paths_for(cfg, session_id)
    vcpus, memory_mb, disk_gb = _resources(profile)
    # The overlay inherits the base disk's virtual size. Use that size for the
    # identity so the guest's reported capacity cannot disagree with reality.
    disk_gb = int(base.get("size_gb", disk_gb))
    identity = generate_identity(
        cfg.hardware,
        vcpus=vcpus,
        memory_mb=memory_mb,
        disk_gb=disk_gb,
        os_name=profile_os,
        nic=str(profile.get("network", {}).get("nic", "e1000e")),
    )
    data: dict[str, Any] = {
        "id": session_id,
        "kind": "run",
        "profile": profile_name,
        "base": base_name,
        "created_at": _now(),
        "status": "created",
        "identity": identity,
        "paths": paths,
    }
    session = Session(directory, data)
    if dry_run:
        return session

    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    try:
        _qemu_img(
            cfg,
            [
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(base["disk"]),
                paths["disk"],
            ],
        )
        shutil.copy2(base["vars"], paths["vars"])
        # copy2 preserves the read-only mode of a sealed base; QEMU must be
        # able to write the per-session NVRAM.
        os.chmod(paths["vars"], 0o600)
        tpm_source = base.get("tpm")
        if tpm_source and Path(tpm_source).is_dir():
            shutil.copytree(tpm_source, paths["tpm"])
        session.save()
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return session


def new_install_session(
    cfg: Config,
    profile_name: str,
    base_name: str,
    disk: Path,
    variables: Path,
    *,
    tpm_dir: Path | None = None,
    disk_gb: int | None = None,
) -> Session:
    """A transient session that writes directly to a base disk."""
    profile = cfg.profile(profile_name)
    session_id = "_install-" + new_session_id()
    directory = cfg.paths.vms / session_id
    paths = _paths_for(cfg, session_id)
    paths["disk"] = str(disk)
    paths["vars"] = str(variables)
    paths["tpm"] = str(tpm_dir) if tpm_dir else ""
    vcpus, memory_mb, profile_disk_gb = _resources(profile)
    identity = generate_identity(
        cfg.hardware,
        vcpus=vcpus,
        memory_mb=memory_mb,
        disk_gb=disk_gb or profile_disk_gb,
        os_name=str(profile.get("os", "windows")),
        nic=str(profile.get("network", {}).get("nic", "e1000e")),
    )
    data: dict[str, Any] = {
        "id": session_id,
        "kind": "install",
        "profile": profile_name,
        "base": base_name,
        "created_at": _now(),
        "status": "created",
        "identity": identity,
        "paths": paths,
    }
    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    session = Session(directory, data)
    session.save()
    return session


def load_session(cfg: Config, session_id: str) -> Session:
    session_id = _safe_component(session_id, "session id")
    directory = cfg.paths.vms / session_id
    path = directory / "session.json"
    if not path.is_file():
        raise StateError(f"no such session: {session_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Session(directory, data)


def resolve_session(cfg: Config, value: str) -> Session:
    value = _safe_component(value, "session id")
    exact = cfg.paths.vms / value
    if (exact / "session.json").is_file():
        return load_session(cfg, value)
    matches = [
        path.name
        for path in cfg.paths.vms.iterdir()
        if path.is_dir() and path.name.startswith(value) and (path / "session.json").is_file()
    ] if cfg.paths.vms.is_dir() else []
    if not matches:
        raise StateError(f"no session matching '{value}'")
    if len(matches) > 1:
        raise StateError(f"'{value}' is ambiguous: {', '.join(sorted(matches))}")
    return load_session(cfg, matches[0])


def list_sessions(cfg: Config) -> list[Session]:
    if not cfg.paths.vms.is_dir():
        return []
    sessions: list[Session] = []
    for path in cfg.paths.vms.iterdir():
        if not path.is_dir():
            continue
        state = path / "session.json"
        if not state.is_file():
            continue
        try:
            sessions.append(Session(path, json.loads(state.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(sessions, key=lambda s: str(s.data.get("created_at", "")), reverse=True)


def qmp_command(sock: Path, command: str, timeout: float = 2.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(sock))
        stream = conn.makefile("rwb")
        stream.readline()  # QMP greeting
        stream.write(b'{"execute":"qmp_capabilities"}\n')
        stream.flush()
        stream.readline()
        stream.write(json.dumps({"execute": command}).encode() + b"\n")
        stream.flush()
        line = stream.readline()
        return json.loads(line) if line else {}


def stop_session(session: Session, timeout: int = 20) -> None:
    if not session.is_running():
        return
    pid = session.pid()
    if pid is None:
        return

    try:
        qmp_command(session.qmp, "system_powerdown")
    except (OSError, ValueError, json.JSONDecodeError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not session.is_running():
            return
        time.sleep(0.25)

    try:
        qmp_command(session.qmp, "quit")
    except (OSError, ValueError, json.JSONDecodeError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not session.is_running():
            return
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def destroy_session(session: Session, *, force: bool = False) -> None:
    if session.is_running():
        if not force:
            raise StateError(
                f"session {session.id} is running; stop it first or use --force"
            )
        stop_session(session)
    shutil.rmtree(session.dir, ignore_errors=True)


# Base images -------------------------------------------------------------


def base_json(cfg: Config, name: str) -> Path:
    name = _safe_component(name, "base name")
    return cfg.paths.bases / f"{name}.json"


def load_base(cfg: Config, name: str, *, require_files: bool = True) -> dict[str, Any]:
    path = base_json(cfg, name)
    if not path.is_file():
        available = ", ".join(sorted(str(b.get("name", "?")) for b in list_bases(cfg))) or "none"
        raise StateError(
            f"base image '{name}' not found (available: {available}).\n"
            f"Create one with: svm base create {name} --iso <installer.iso>"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"base '{name}' has unreadable metadata {path}: {exc}") from exc
    if require_files:
        try:
            disk = Path(data["disk"])
            variables = Path(data["vars"])
        except (KeyError, TypeError) as exc:
            raise StateError(
                f"base '{name}' has invalid metadata {path}: missing {exc}"
            ) from exc
        if not disk.is_file() or not variables.is_file():
            raise StateError(f"base '{name}' is incomplete: {disk} / {variables}")
    return data


def list_bases(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.paths.bases.is_dir():
        return []
    bases: list[dict[str, Any]] = []
    for path in sorted(cfg.paths.bases.glob("*.json")):
        try:
            bases.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return bases


def base_size_bytes(base: dict[str, Any]) -> int:
    total = 0
    for key in ("disk", "vars"):
        value = base.get(key)
        if not value:
            continue
        try:
            total += Path(value).stat().st_size
        except (OSError, TypeError, ValueError):
            pass
    tpm = base.get("tpm")
    try:
        tpm_dir = Path(tpm) if tpm else None
    except (TypeError, ValueError):
        tpm_dir = None
    if tpm_dir is not None and tpm_dir.is_dir():
        for root, _dirs, files in os.walk(tpm_dir):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    return total


def save_base(cfg: Config, data: dict[str, Any]) -> None:
    path = base_json(cfg, str(data["name"]))
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)


def delete_base(cfg: Config, name: str) -> None:
    # Metadata may outlive the files (failed install, manual cleanup); delete
    # what is left instead of refusing to clean up an incomplete base.
    base = load_base(cfg, name, require_files=False)
    for key in ("disk", "vars"):
        value = base.get(key)
        if not value:
            continue
        try:
            Path(value).unlink(missing_ok=True)
        except (OSError, TypeError, ValueError):
            pass
    tpm = base.get("tpm")
    if tpm:
        try:
            shutil.rmtree(tpm, ignore_errors=True)
        except (TypeError, ValueError):
            pass
    base_json(cfg, name).unlink(missing_ok=True)


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"

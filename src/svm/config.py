"""Configuration and path discovery for svm."""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_NAME = "stealth-vm"


def project_root() -> Path:
    """Return the project root.

    SVM_ROOT wins so the CLI can be used from a checkout or a moved tree.
    """
    env = os.environ.get("SVM_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / APP_NAME).resolve()


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class Ovmf:
    code: Path
    vars: Path
    code_sb: Path | None = None
    vars_sb: Path | None = None

    @property
    def secure_boot_available(self) -> bool:
        return self.code_sb is not None and self.vars_sb is not None


def _first(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def find_ovmf() -> Ovmf | None:
    roots = [Path("/usr/share/OVMF"), Path("/usr/share/edk2/ovmf"), Path("/usr/share/qemu")]
    override = os.environ.get("SVM_OVMF_DIR")
    if override:
        roots.insert(0, Path(override).expanduser())
    code = _first(
        [
            p
            for root in roots
            for p in (root / "OVMF_CODE_4M.fd", root / "OVMF_CODE.fd")
        ]
    )
    variables = _first(
        [
            p
            for root in roots
            for p in (root / "OVMF_VARS_4M.fd", root / "OVMF_VARS.fd")
        ]
    )
    code_sb = _first(
        [
            p
            for root in roots
            for p in (
                root / "OVMF_CODE_4M.ms.fd",
                root / "OVMF_CODE_4M.secboot.fd",
                root / "OVMF_CODE.secboot.fd",
                root / "OVMF_CODE.ms.fd",
            )
        ]
    )
    vars_sb = _first(
        [
            p
            for root in roots
            for p in (
                root / "OVMF_VARS_4M.ms.fd",
                root / "OVMF_VARS_4M.secboot.fd",
                root / "OVMF_VARS.ms.fd",
                root / "OVMF_VARS.secboot.fd",
            )
        ]
    )
    if code is None or variables is None:
        return None
    return Ovmf(code=code, vars=variables, code_sb=code_sb, vars_sb=vars_sb)


@dataclass(frozen=True)
class Paths:
    root: Path
    config: Path
    profiles: Path
    images: Path
    bases: Path
    isos: Path
    vms: Path
    runtime: Path
    logs: Path
    build: Path
    qemu_prefix: Path
    qemu_bin: Path | None
    qemu_img: Path | None
    ovmf: Ovmf | None


def _which(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found) if found else None


@dataclass
class Config:
    root: Path
    defaults: dict[str, Any]
    profiles: dict[str, dict[str, Any]]
    hardware: dict[str, Any]
    paths: Paths

    @property
    def default_profile(self) -> str:
        return str(self.defaults.get("general", {}).get("default_profile", "win10"))

    def profile(self, name: str) -> dict[str, Any]:
        if name not in self.profiles:
            available = ", ".join(sorted(self.profiles))
            raise KeyError(f"unknown profile '{name}' (available: {available})")
        merged = deep_merge(self.defaults, self.profiles[name])
        merged["name"] = name
        return merged

    def profile_names(self) -> list[str]:
        return sorted(self.profiles)


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_config(root: Path | None = None) -> Config:
    root = (root or project_root()).resolve()
    config_dir = root / "config"
    defaults_path = config_dir / "defaults.toml"
    hardware_path = config_dir / "hardware.toml"
    profiles_dir = config_dir / "profiles"

    if not defaults_path.is_file():
        raise FileNotFoundError(
            f"missing {defaults_path}; run svm from the project root or set SVM_ROOT"
        )

    defaults = _read_toml(defaults_path)
    hardware = _read_toml(hardware_path) if hardware_path.is_file() else {}
    profiles: dict[str, dict[str, Any]] = {}
    if profiles_dir.is_dir():
        for path in sorted(profiles_dir.glob("*.toml")):
            profiles[path.stem] = _read_toml(path)

    qemu_prefix = root / "runtime" / "qemu"
    local_qemu = qemu_prefix / "bin" / "qemu-system-x86_64"
    local_img = qemu_prefix / "bin" / "qemu-img"
    qemu_bin = local_qemu if local_qemu.is_file() else _which("qemu-system-x86_64")
    qemu_img = local_img if local_img.is_file() else _which("qemu-img")

    paths = Paths(
        root=root,
        config=config_dir,
        profiles=profiles_dir,
        images=root / "images",
        bases=root / "images" / "base",
        isos=root / "images" / "iso",
        vms=root / "vms",
        runtime=root / "runtime",
        logs=root / "logs",
        build=root / "build",
        qemu_prefix=qemu_prefix,
        qemu_bin=qemu_bin,
        qemu_img=qemu_img,
        ovmf=find_ovmf(),
    )
    return Config(
        root=root,
        defaults=defaults,
        profiles=profiles,
        hardware=hardware,
        paths=paths,
    )


def ensure_dirs(cfg: Config) -> None:
    for path in (
        cfg.paths.images,
        cfg.paths.bases,
        cfg.paths.isos,
        cfg.paths.vms,
        cfg.paths.runtime,
        cfg.paths.logs,
        cfg.paths.build / "downloads",
        cfg.paths.build / "src",
    ):
        path.mkdir(parents=True, exist_ok=True)

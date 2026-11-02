from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import Config, ensure_dirs, load_config
from .identity import summary_rows
from .qemu import (
    build_command,
    is_patched,
    qemu_version,
    render_hardware_config,
    tail,
    viewer_command,
    wait_for_socket,
)
from .state import (
    StateError,
    base_json,
    delete_base,
    destroy_session,
    human_bytes,
    list_bases,
    list_sessions,
    load_base,
    load_session,
    new_install_session,
    new_session,
    resolve_session,
    save_base,
    stop_session,
    base_size_bytes,
)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    help="Disposable, stealth-configured QEMU virtual machines.",
)
base_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Manage persistent base images (installed once, cloned per session).",
)
app.add_typer(base_app, name="base")

console = Console()


def _fail(message: str, code: int = 1) -> None:
    console.print(f"[red]error:[/] {message}")
    raise typer.Exit(code)


def _cfg() -> Config:
    try:
        cfg = load_config()
        ensure_dirs(cfg)
        return cfg
    except Exception as exc:  # noqa: BLE001 - surface a clean CLI error
        _fail(str(exc))


def _base_name(cfg: Config, profile_name: str, explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    profile = cfg.profile(profile_name)
    return str(profile.get("base", profile_name))


def _running_sessions_for_base(cfg: Config, base_name: str) -> list[str]:
    return [
        session.id
        for session in list_sessions(cfg)
        if session.data.get("base") == base_name and session.is_running()
    ]


def _session_identity_table(identity: dict[str, Any]) -> Table:
    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column("Field", style="dim", no_wrap=True)
    table.add_column("Value")
    for key, value in summary_rows(identity):
        table.add_row(key, value)
    return table


def _swtpm_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return True
    return b"swtpm" in cmdline


def _start_swtpm(session) -> None:
    tpm_dir = session.tpm_dir
    if tpm_dir is None:
        raise StateError("TPM requested but session has no TPM directory")
    tpm_dir.mkdir(parents=True, exist_ok=True)
    _stop_swtpm(session)
    swtpm = shutil.which("swtpm")
    if swtpm is None:
        raise StateError("swtpm not found. Run: sudo ./bootstrap.sh")
    sock = tpm_dir / "swtpm.sock"
    pidfile = tpm_dir / "swtpm.pid"
    sock.unlink(missing_ok=True)
    pidfile.unlink(missing_ok=True)
    result = subprocess.run(
        [
            swtpm,
            "socket",
            "--tpm2",
            "--tpmstate",
            f"dir={tpm_dir}",
            "--ctrl",
            f"type=unixio,path={sock}",
            "--log",
            f"file={tpm_dir / 'swtpm.log'}",
            "--pid",
            f"file={pidfile}",
            "--daemon",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise StateError(f"swtpm failed: {result.stderr.strip() or result.stdout.strip()}")
    if not wait_for_socket(sock, 5):
        raise StateError(f"swtpm did not create {sock}")


def _stop_swtpm(session) -> None:
    tpm_dir = session.tpm_dir
    if tpm_dir is None:
        return
    pidfile = tpm_dir / "swtpm.pid"
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if not _swtpm_is_running(pid):
        pidfile.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pidfile.unlink(missing_ok=True)
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _swtpm_is_running(pid):
            pidfile.unlink(missing_ok=True)
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    pidfile.unlink(missing_ok=True)


def _wipe_session(session) -> None:
    """Stop QEMU and swtpm before deleting the directory.

    swtpm's pidfile lives inside the session (or the base TPM directory), so
    deleting first would orphan the daemon.
    """
    stop_session(session)
    _stop_swtpm(session)
    destroy_session(session, force=True)


def _run_qemu(
    cfg: Config,
    session,
    *,
    install_iso: Optional[Path] = None,
    driver_iso: Optional[Path] = None,
    headless: bool = False,
    no_sandbox: bool = False,
    debug: bool = False,
    viewer_override: Optional[str] = None,
) -> tuple[int, bool]:
    profile = cfg.profile(str(session.data["profile"]))
    tpm = bool(profile.get("hardware", {}).get("tpm", False))
    command = build_command(
        cfg,
        session,
        install_iso=install_iso,
        driver_iso=driver_iso,
        no_sandbox=no_sandbox,
        debug=debug,
    )
    if tpm:
        _start_swtpm(session)
    if debug:
        console.print(f"[dim]{shlex.join(command)}[/]")

    session.log.parent.mkdir(parents=True, exist_ok=True)
    log_handle = session.log.open("ab")
    try:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        log_handle.close()
        _stop_swtpm(session)
        raise StateError(f"failed to start QEMU: {exc}") from exc
    session.data["status"] = "running"
    session.save()

    viewer_process: Optional[subprocess.Popen] = None
    started = True
    try:
        if not headless:
            if not wait_for_socket(session.spice, timeout=20):
                started = False
                stop_session(session, timeout=5)
                try:
                    return process.wait(timeout=5), started
                except subprocess.TimeoutExpired:
                    process.kill()
                    return process.wait(), started
            if viewer_override:
                viewer_cmd = [viewer_override, f"spice+unix://{session.spice}"]
            else:
                viewer_cmd = viewer_command(cfg, session)
            viewer_process = subprocess.Popen(viewer_cmd)

            while process.poll() is None and viewer_process.poll() is None:
                time.sleep(0.25)
            if viewer_process.poll() is not None and process.poll() is None:
                stop_session(session, timeout=15)

        if headless:
            return process.wait(), started

        try:
            return process.wait(timeout=15), started
        except subprocess.TimeoutExpired:
            stop_session(session, timeout=5)
            return process.wait(), started
    except KeyboardInterrupt:
        stop_session(session, timeout=10)
        raise
    finally:
        if process.poll() is None:
            stop_session(session, timeout=10)
        if viewer_process is not None and viewer_process.poll() is None:
            viewer_process.terminate()
            try:
                viewer_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                viewer_process.kill()
        _stop_swtpm(session)
        log_handle.close()
        session.data["status"] = "stopped"
        session.save()


@app.command()
def doctor() -> None:
    """Check the host, the patched QEMU build, firmware and viewers."""
    cfg = _cfg()
    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    def row(name: str, ok: bool, detail: str, warn: bool = False) -> None:
        status = "[green]ok[/]" if ok else ("[yellow]warn[/]" if warn else "[red]fail[/]")
        table.add_row(name, status, detail)

    row("project root", os.access(cfg.root, os.W_OK), str(cfg.root))
    kvm = Path("/dev/kvm")
    kvm_ok = kvm.exists() and os.access(kvm, os.R_OK | os.W_OK)
    row("KVM", kvm_ok, str(kvm) if kvm_ok else "not readable/writable")

    if cfg.paths.qemu_bin is None:
        row("QEMU", False, "not found — run: sudo ./bootstrap.sh")
    else:
        patched = is_patched(cfg)
        local = cfg.paths.qemu_bin.is_relative_to(cfg.root)
        row(
            "QEMU",
            patched,
            f"{qemu_version(cfg)} ({'patched' if patched else 'NOT patched'}, {cfg.paths.qemu_bin})",
            warn=not patched,
        )
        if not local and not patched:
            row("patched build", False, "system QEMU only; stealth patches inactive")

    row(
        "qemu-img",
        cfg.paths.qemu_img is not None,
        str(cfg.paths.qemu_img) if cfg.paths.qemu_img else "not found",
    )

    ovmf = cfg.paths.ovmf
    row(
        "OVMF",
        ovmf is not None,
        f"{ovmf.code} / {ovmf.vars}" if ovmf else "install the ovmf package",
    )
    if ovmf is not None:
        row(
            "Secure Boot OVMF",
            ovmf.secure_boot_available,
            "available" if ovmf.secure_boot_available else "missing (win11 profile unavailable)",
            warn=not ovmf.secure_boot_available,
        )

    viewer = shutil.which("remote-viewer") or shutil.which("spicy")
    row("SPICE viewer", viewer is not None, viewer or "install virt-viewer")
    swtpm = shutil.which("swtpm")
    row("swtpm", swtpm is not None, swtpm or "install swtpm (win11 only)", warn=True)

    bases = list_bases(cfg)
    row("base images", bool(bases), f"{len(bases)} installed" if bases else "none")
    sessions = list_sessions(cfg)
    running = sum(1 for session in sessions if session.is_running())
    row("sessions", True, f"{len(sessions)} total, {running} running")

    free = shutil.disk_usage(cfg.root).free
    row("disk free", free > 40 * 1024**3, human_bytes(free))

    if os.geteuid() == 0:
        row("user", False, "running as root — run svm as your desktop user", warn=True)

    console.print(table)

    if cfg.paths.qemu_bin is None or not is_patched(cfg):
        console.print("\n[bold]Next:[/] sudo ./bootstrap.sh")
    if not bases:
        console.print(
            "[bold]Next:[/] put a Windows ISO in images/iso/ and run "
            "[cyan]svm base create win10 --iso images/iso/Win10.iso[/]"
        )


@app.command()
def build() -> None:
    """Build patched QEMU into runtime/qemu (run bootstrap.sh first for deps)."""
    cfg = _cfg()
    script = cfg.root / "scripts" / "build-qemu.sh"
    if not script.is_file():
        _fail(f"missing {script}")
    result = subprocess.run(["bash", str(script)], cwd=cfg.root)
    if result.returncode != 0:
        _fail("QEMU build failed; see the output above")


@app.command()
def info() -> None:
    """Show paths, host details and installed bases."""
    cfg = _cfg()
    bases = list_bases(cfg)
    sessions = list_sessions(cfg)
    lines = [
        f"[bold]root[/]        {cfg.root}",
        f"[bold]QEMU[/]        {cfg.paths.qemu_bin or 'missing'}",
        f"[bold]qemu-img[/]    {cfg.paths.qemu_img or 'missing'}",
        f"[bold]OVMF[/]        {cfg.paths.ovmf.code if cfg.paths.ovmf else 'missing'}",
        f"[bold]bases[/]       {', '.join(str(b.get('name', '?')) for b in bases) or 'none'}",
        f"[bold]sessions[/]    {len(sessions)}",
        f"[bold]profiles[/]    {', '.join(cfg.profile_names())}",
        f"[bold]default[/]     {cfg.default_profile}",
    ]
    console.print(Panel("\n".join(lines), title="svm", box=box.ROUNDED))


@app.command()
def version() -> None:
    """Print versions and whether the local QEMU is patched."""
    cfg = _cfg()
    patched = is_patched(cfg) if cfg.paths.qemu_bin else False
    console.print(f"svm {__version__}")
    console.print(f"qemu {qemu_version(cfg) if cfg.paths.qemu_bin else 'missing'}")
    console.print(f"patched {'yes' if patched else 'no'}")


@app.command()
def paths() -> None:
    """Print every path svm uses."""
    cfg = _cfg()
    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("Name", style="dim")
    table.add_column("Path")
    for name in (
        "root",
        "config",
        "profiles",
        "images",
        "bases",
        "isos",
        "vms",
        "runtime",
        "logs",
        "build",
        "qemu_prefix",
        "qemu_bin",
        "qemu_img",
    ):
        table.add_row(name, str(getattr(cfg.paths, name)))
    console.print(table)


@app.command()
def new(
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Profile name."),
    base: Optional[str] = typer.Option(None, "--base", "-b", help="Base image name."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; write nothing."),
) -> None:
    """Create a new disposable session (overlay disk + fresh identity)."""
    cfg = _cfg()
    profile_name = profile or cfg.default_profile
    try:
        profile_data = cfg.profile(profile_name)
        base_name = _base_name(cfg, profile_name, base)
        session = new_session(cfg, profile_name, base_name, dry_run=dry_run)
    except (StateError, KeyError, ValueError) as exc:
        _fail(str(exc))

    console.print(f"[bold]session[/] {session.id}")
    console.print(_session_identity_table(session.data["identity"]))
    if dry_run:
        console.print("[yellow]dry run:[/] no files written")
    else:
        console.print(f"[dim]disk:[/] {session.disk}")
        console.print(f"[bold]next:[/] svm run {session.id}")


@app.command()
def run(
    session_id: Optional[str] = typer.Argument(None, help="Session id or unique prefix."),
    fresh: bool = typer.Option(False, "--fresh", help="Create a new session first."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p"),
    base: Optional[str] = typer.Option(None, "--base", "-b"),
    keep: bool = typer.Option(False, "--keep", help="Keep the session after it exits."),
    persist: bool = typer.Option(
        False, "--persist", help="Persistent VM: keep it and its state across stop/start (same as --keep)."
    ),
    ephemeral: bool = typer.Option(
        False, "--ephemeral", help="Delete the session after the viewer closes."
    ),
    headless: bool = typer.Option(False, "--headless", help="Do not launch a viewer."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the QEMU command."),
    viewer: Optional[str] = typer.Option(None, "--viewer", help="Viewer executable."),
    no_sandbox: bool = typer.Option(False, "--no-sandbox", help="Disable QEMU seccomp."),
    debug: bool = typer.Option(False, "--debug", help="Print the command and guest errors."),
) -> None:
    """Boot a session. The profile decides whether it is kept after exit."""
    cfg = _cfg()
    try:
        if fresh:
            profile_name = profile or cfg.default_profile
            base_name = _base_name(cfg, profile_name, base)
            session = new_session(cfg, profile_name, base_name, dry_run=dry_run)
        elif session_id:
            session = resolve_session(cfg, session_id)
        else:
            _fail("pass a session id or use --fresh")
    except (StateError, KeyError, ValueError) as exc:
        _fail(str(exc))

    if session.kind == "run":
        try:
            load_base(cfg, str(session.data.get("base", "")))
        except StateError as exc:
            _fail(str(exc))

    if session.is_running():
        _fail(f"session {session.id} is already running; use: svm attach {session.id}")

    if dry_run:
        profile_data = cfg.profile(str(session.data["profile"]))
        command = build_command(
            cfg,
            session,
            no_sandbox=no_sandbox,
            debug=debug,
            write_config=False,
        )
        console.print("[bold]command[/]")
        console.print(shlex.join(command))
        console.print("\n[bold]hardware.cfg[/]")
        console.print(
            render_hardware_config(cfg, session, profile_data),
            markup=False,
            highlight=False,
        )
        return

    auto_destroy = bool(
        cfg.profile(str(session.data["profile"])).get("general", {}).get("auto_destroy", True)
    )
    if ephemeral:
        auto_destroy = True
    elif keep or persist:
        auto_destroy = False

    console.print(f"[bold]booting[/] {session.id} [dim]({session.data['profile']})[/]")
    try:
        code, started = _run_qemu(
            cfg,
            session,
            headless=headless,
            no_sandbox=no_sandbox,
            debug=debug,
            viewer_override=viewer,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/]")
        code, started = 130, True
    except StateError as exc:
        _fail(str(exc))

    if code != 0 or not started:
        console.print("[red]QEMU did not exit cleanly[/]")
        console.print(tail(session.log, 40) or "(no log output)")
        console.print(f"[yellow]kept for inspection:[/] {session.dir}")
        return

    if auto_destroy:
        _wipe_session(session)
        console.print("[green]session wiped[/]")
    else:
        console.print(f"[green]kept[/] svm run {session.id}")


@app.command("list")
def list_command(
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """List sessions."""
    cfg = _cfg()
    sessions = list_sessions(cfg)
    if as_json:
        payload = [
            {
                "id": session.id,
                "profile": str(session.data.get("profile", "?")),
                "base": str(session.data.get("base", "?")),
                "status": "running" if session.is_running() else "stopped",
                "created_at": str(session.data.get("created_at", "")),
                "size_bytes": session.size_bytes(),
            }
            for session in sessions
        ]
        typer.echo(json.dumps(payload, indent=2))
        return
    if not sessions:
        console.print("[dim]no sessions[/]")
        return
    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("ID")
    table.add_column("Profile")
    table.add_column("Base")
    table.add_column("Status")
    table.add_column("Age", justify="right")
    table.add_column("Size", justify="right")
    for session in sessions:
        running = session.is_running()
        status = "[green]running[/]" if running else "[dim]stopped[/]"
        table.add_row(
            session.id,
            str(session.data.get("profile", "?")),
            str(session.data.get("base", "?")),
            status,
            session.age(),
            human_bytes(session.size_bytes()),
        )
    console.print(table)


@app.command()
def attach(
    session_id: str = typer.Argument(..., help="Session id or unique prefix."),
    viewer: Optional[str] = typer.Option(None, "--viewer", help="Viewer executable."),
) -> None:
    """Open a SPICE viewer for an already-running session."""
    cfg = _cfg()
    try:
        session = resolve_session(cfg, session_id)
    except StateError as exc:
        _fail(str(exc))
    if not session.is_running():
        _fail(f"session {session.id} is not running")
    if viewer:
        command = [viewer, f"spice+unix://{session.spice}"]
    else:
        try:
            command = viewer_command(cfg, session)
        except StateError as exc:
            _fail(str(exc))
    subprocess.run(command, check=False)


@app.command()
def stop(session_id: str = typer.Argument(..., help="Session id or unique prefix.")) -> None:
    """Ask the guest to power down, then stop QEMU if needed."""
    cfg = _cfg()
    try:
        session = resolve_session(cfg, session_id)
    except StateError as exc:
        _fail(str(exc))
    stop_session(session)
    _stop_swtpm(session)
    session.data["status"] = "stopped"
    session.save()
    console.print(f"[green]stopped[/] {session.id}")


@app.command()
def destroy(
    session_id: str = typer.Argument(..., help="Session id or unique prefix."),
    force: bool = typer.Option(False, "--force", help="Stop and delete without asking."),
) -> None:
    """Delete a session directory and everything in it."""
    cfg = _cfg()
    try:
        session = resolve_session(cfg, session_id)
    except StateError as exc:
        _fail(str(exc))
    if session.is_running() and not force:
        _fail(f"session {session.id} is running; use --force")
    if not force:
        if not typer.confirm(f"Delete {session.id} ({human_bytes(session.size_bytes())})?"):
            raise typer.Exit(0)
    _wipe_session(session)
    console.print(f"[green]deleted[/] {session.id}")


@app.command()
def nuke(
    force: bool = typer.Option(False, "--force", help="Do not ask for confirmation."),
) -> None:
    """Delete every session (base images are untouched)."""
    cfg = _cfg()
    sessions = list_sessions(cfg)
    if not sessions:
        console.print("[dim]no sessions[/]")
        return
    if not force:
        if not typer.confirm(f"Delete all {len(sessions)} sessions?"):
            raise typer.Exit(0)
    for session in sessions:
        _wipe_session(session)
    console.print(f"[green]deleted[/] {len(sessions)} sessions")


@app.command()
def gc(
    days: int = typer.Option(7, "--days", min=0, help="Delete stopped sessions older than N days."),
) -> None:
    """Garbage-collect old stopped sessions and abandoned install sessions."""
    cfg = _cfg()
    cutoff = time.time() - days * 86400
    removed = 0
    for session in list_sessions(cfg):
        if session.is_running():
            continue
        if session.created_timestamp() < cutoff:
            _wipe_session(session)
            removed += 1
    if cfg.paths.vms.is_dir():
        for path in cfg.paths.vms.glob("_install-*"):
            try:
                old = path.is_dir() and path.stat().st_mtime < time.time() - 86400
            except OSError:
                old = False
            if not old:
                continue
            if (path / "session.json").is_file():
                try:
                    session = load_session(cfg, path.name)
                    if session.is_running():
                        continue
                    _wipe_session(session)
                    removed += 1
                    continue
                except (StateError, OSError):
                    pass
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    console.print(f"[green]removed[/] {removed} directories")


@app.command()
def identity(session_id: str = typer.Argument(..., help="Session id or unique prefix.")) -> None:
    """Show the generated hardware identity for a session."""
    cfg = _cfg()
    try:
        session = resolve_session(cfg, session_id)
    except StateError as exc:
        _fail(str(exc))
    console.print(_session_identity_table(session.data["identity"]))
    console.print("\n[dim]raw JSON[/]")
    console.print(json.dumps(session.data["identity"], indent=2), markup=False)


# Base images -------------------------------------------------------------


@base_app.command("list")
def base_list() -> None:
    """List installed base images."""
    cfg = _cfg()
    bases = list_bases(cfg)
    if not bases:
        console.print("[dim]no base images[/]")
        return
    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("Name")
    table.add_column("Profile")
    table.add_column("OS")
    table.add_column("Created")
    table.add_column("Size", justify="right")
    for base in bases:
        table.add_row(
            str(base.get("name", "?")),
            str(base.get("profile", "?")),
            str(base.get("os", "?")),
            str(base.get("created_at", "?"))[:19],
            human_bytes(base_size_bytes(base)),
        )
    console.print(table)


@base_app.command("create")
def base_create(
    name: str = typer.Argument(..., help="Base name, e.g. win10."),
    iso: Path = typer.Option(
        ...,
        "--iso",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Installer ISO (Windows or Linux).",
    ),
    profile: Optional[str] = typer.Option(None, "--profile", "-p"),
    driver_iso: Optional[Path] = typer.Option(
        None,
        "--driver-iso",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Optional driver ISO, attached as a second CD-ROM.",
    ),
    size_gb: Optional[int] = typer.Option(
        None,
        "--size-gb",
        min=8,
        help="Disk capacity in decimal GB (the number on the SSD label).",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing base."),
    headless: bool = typer.Option(False, "--headless", help="Do not launch a viewer."),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Seal the base without asking after the installer exits.",
    ),
) -> None:
    """Install an OS once into a persistent base image.

    The installer runs against a real disk (not an overlay). Shut the guest
    down from inside the OS when installation is complete; svm then asks you
    to confirm before sealing the base. Every future session clones it.
    """
    cfg = _cfg()
    try:
        base_json(cfg, name)
    except StateError as exc:
        _fail(str(exc))
    profile_name = profile or cfg.default_profile
    try:
        profile_data = cfg.profile(profile_name)
    except KeyError as exc:
        _fail(str(exc))

    base_dir = cfg.paths.bases
    disk = base_dir / f"{name}.qcow2"
    variables = base_dir / f"{name}.vars.fd"
    meta = base_dir / f"{name}.json"
    tpm_dir = base_dir / f"{name}.tpm"

    if meta.exists() and not force:
        _fail(f"base '{name}' already exists; use --force to replace it")

    # Check prerequisites before --force deletes an existing base.
    if cfg.paths.qemu_img is None:
        _fail("qemu-img not found. Run: sudo ./bootstrap.sh")
    if cfg.paths.ovmf is None:
        _fail("OVMF not found. Run: sudo ./bootstrap.sh")
    if not iso.is_file():
        _fail(f"ISO not found: {iso}")

    in_use = _running_sessions_for_base(cfg, name)
    if in_use:
        _fail(
            f"base '{name}' is in use by running session(s): "
            f"{', '.join(in_use)}; stop them first"
        )

    if force:
        for path in (disk, variables, meta):
            path.unlink(missing_ok=True)
        shutil.rmtree(tpm_dir, ignore_errors=True)

    resources = profile_data.get("resources", {})
    hardware = profile_data.get("hardware", {})
    size = int(size_gb or resources.get("disk_gb", 250))
    secure_boot = bool(hardware.get("secure_boot", False))
    tpm = bool(hardware.get("tpm", False))
    ovmf = cfg.paths.ovmf
    if secure_boot:
        if not ovmf.secure_boot_available:
            _fail("Secure Boot OVMF is missing; install the ovmf package")
        template = ovmf.vars_sb
    else:
        template = ovmf.vars
    assert template is not None

    console.print(f"[bold]creating base[/] {name} ({size} GB, profile {profile_name})")
    # Decimal GB, not qemu-img's "G" suffix (GiB). This keeps the emulated
    # SSD label, its real capacity, and the guest's lsblk output consistent.
    size_bytes = size * 1_000_000_000
    result = subprocess.run(
        [str(cfg.paths.qemu_img), "create", "-f", "qcow2", str(disk), str(size_bytes)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _fail(f"qemu-img create failed: {result.stderr.strip()}")

    shutil.copy2(template, variables)
    os.chmod(variables, 0o600)
    if tpm:
        tpm_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(tpm_dir, 0o700)

    try:
        session = new_install_session(
            cfg,
            profile_name,
            name,
            disk,
            variables,
            tpm_dir=tpm_dir if tpm else None,
            disk_gb=size,
        )
    except (StateError, ValueError) as exc:
        for path in (disk, variables):
            path.unlink(missing_ok=True)
        shutil.rmtree(tpm_dir, ignore_errors=True)
        _fail(str(exc))

    try:
        code, started = _run_qemu(
            cfg,
            session,
            install_iso=iso,
            driver_iso=driver_iso,
            headless=headless,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/]")
        code, started = 130, True
    except StateError as exc:
        _fail(str(exc))

    if code != 0 or not started:
        console.print("[red]installation did not finish cleanly[/]")
        console.print(tail(session.log, 40) or "(no log output)")
        console.print(f"[yellow]kept for inspection:[/] {session.dir}")
        return

    if not yes:
        if not typer.confirm(
            f"Installation in '{name}' finished. Seal this base?",
            default=False,
        ):
            console.print("[yellow]not sealed[/]")
            console.print(f"install session kept: {session.dir}")
            console.print(
                f"re-run with [cyan]svm base create {name} --iso {iso} --force[/] "
                "to start over"
            )
            return

    save_base(
        cfg,
        {
            "name": name,
            "profile": profile_name,
            "os": str(profile_data.get("os", "windows")),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "disk": str(disk),
            "vars": str(variables),
            "tpm": str(tpm_dir) if tpm else None,
            "secure_boot": secure_boot,
            "size_gb": size,
        },
    )
    os.chmod(disk, 0o444)
    os.chmod(variables, 0o444)
    _wipe_session(session)

    console.print(f"[green]base ready[/] {name}")
    console.print(
        f"[bold]next:[/] svm run --fresh --profile {profile_name} --base {name}"
    )


@base_app.command("delete")
def base_delete(
    name: str = typer.Argument(..., help="Base name."),
    force: bool = typer.Option(False, "--force", help="Do not ask for confirmation."),
) -> None:
    """Delete a base image and its NVRAM/TPM state."""
    cfg = _cfg()
    try:
        base = load_base(cfg, name, require_files=False)
    except StateError as exc:
        _fail(str(exc))
    in_use = _running_sessions_for_base(cfg, name)
    if in_use:
        _fail(
            f"base '{name}' is in use by running session(s): "
            f"{', '.join(in_use)}; stop them first"
        )
    if not force:
        if not typer.confirm(f"Delete base '{name}' ({human_bytes(base_size_bytes(base))})?"):
            raise typer.Exit(0)
    delete_base(cfg, name)
    console.print(f"[green]deleted base[/] {name}")

#!/usr/bin/env python3
"""
scw-backup — rclone-based offsite backup to SCW bulk storage.

Display model: instead of a full-screen dashboard, this uses rich's
Progress widget the way package managers like pacman show downloads —
one live-updating bar per item being transferred; once it finishes, the
bar is replaced by a single static "done" line that scrolls into normal
terminal history, and the next item's bar takes its place.

rclone is driven with --use-json-log so each periodic stats snapshot can
be parsed with json.loads() and used to update the active bar's
percentage and "currently transferring" filename. Output is captured by
us directly (not via rclone's own --log-file, which would otherwise
swallow the very stream we need to read) and persisted to our own log
file line-for-line.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(os.environ.get("SCWBAK_CONFIG", str(Path.home() / ".scwbak" / "config.yml")))
LOCKFILE = Path("/tmp/scw-backup.lock")
LOGDIR = Path.home() / ".scwbak" / "logs"

REQUIRED_COMMANDS = ["rclone", "git", "find"]

BUZM_ART = [
    "██████╗ ██╗   ██╗███████╗███╗   ███╗",
    "██╔══██╗██║   ██║╚══███╔╝████╗ ████║",
    "██████╔╝██║   ██║  ███╔╝ ██╔████╔██║",
    "██╔══██╗██║   ██║ ███╔╝  ██║╚██╔╝██║",
    "██████╔╝╚██████╔╝███████╗ ██║ ╚═╝ ██║",
    "╚═════╝  ╚═════╝ ╚══════╝ ╚═╝     ╚═╝",
]

console = Console()


def human_size(n: Optional[float]) -> str:
    n = n or 0
    if n < 1024:
        return f"{n:.0f} B"
    if n < 1024**2:
        return f"{n/1024:.1f} KiB"
    if n < 1024**3:
        return f"{n/1024**2:.1f} MiB"
    if n < 1024**4:
        return f"{n/1024**3:.2f} GiB"
    return f"{n/1024**4:.2f} TiB"


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

@dataclass
class DirEntry:
    source: Path
    dest_name: str
    mode: str
    gitignore: bool


@dataclass
class Library:
    name: str
    entries: list[DirEntry]


def parse_path_field(raw: str) -> tuple[Path, str]:
    """
    "path" may optionally carry a custom destination name after a colon,
    e.g. "/home/user/Videos/OBS/:OBS". Without a colon, the destination
    name falls back to the source directory's own basename.
    """
    raw = raw.strip()
    before, sep, after = raw.rpartition(":")
    if sep:
        custom = after.strip()
        path_str = before.rstrip("/") or "/"
        if custom:
            return Path(path_str), custom
        raw = path_str
    clean = raw.rstrip("/") or "/"
    return Path(clean), Path(clean).name


def load_config(path: Path) -> tuple[dict, list[Library]]:
    raw = yaml.safe_load(path.read_text()) or {}
    run_cfg = raw.get("run") or {}
    libs_cfg = raw.get("libraries") or {}

    libraries: list[Library] = []
    for lib_name, lib_body in libs_cfg.items():
        lib_body = lib_body or {}
        lib_mode_default = lib_body.get("mode", "copy")
        lib_gitignore_default = bool(lib_body.get("gitignore", False))

        entries: list[DirEntry] = []
        for raw_entry in lib_body.get("dirs") or []:
            path_field = raw_entry.get("path", "")
            if not path_field:
                continue
            source, dest_name = parse_path_field(path_field)
            mode = raw_entry.get("mode", lib_mode_default)
            gitignore = bool(raw_entry.get("gitignore", lib_gitignore_default))
            entries.append(DirEntry(source=source, dest_name=dest_name, mode=mode, gitignore=gitignore))

        libraries.append(Library(name=lib_name, entries=entries))

    return run_cfg, libraries


def count_files_excluding(root: Path, exclude: list[Path], cap: Optional[int]) -> int:
    """Recursively count files under root, pruning any excluded subtrees.
    Stops early once the count exceeds cap (when cap is set), so a huge
    directory with a low cap doesn't require a full walk."""
    exclude_set = {str(p) for p in exclude}
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) not in exclude_set]
        count += len(filenames)
        if cap is not None and count > cap:
            return count
    return count


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class Logger:
    def __init__(self, logfile: Path):
        self.logfile = logfile
        self.logfile.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.logfile, "a", buffering=1)

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log(self, msg: str, echo: bool = True) -> None:
        line = f"[{self._timestamp()}] {msg}"
        self._fh.write(line + "\n")
        if echo:
            console.print(line)

    def log_file_only(self, msg: str) -> None:
        self.log(msg, echo=False)

    def raw(self, line: str) -> None:
        self._fh.write(f"[{self._timestamp()}] {line}\n")

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------------------
# rclone invocation
#
# --log-file is deliberately never passed to rclone: it redirects rclone's
# *entire* logger sink (including the --use-json-log stats stream) to that
# file instead of stdout/stderr, which is exactly the stream we need to
# read to drive the live progress bar. Instead we capture combined
# stdout+stderr ourselves and persist every raw line via Logger.raw().
# ---------------------------------------------------------------------------

def build_rclone_args(dry_run: bool, verbose: bool, interactive: bool) -> list[str]:
    args: list[str] = []
    if dry_run:
        args.append("--dry-run")
    if verbose:
        args.append("-v")
    else:
        args.append("--log-level=NOTICE")
    if interactive:
        args += ["--use-json-log", "--stats=1s"]
    else:
        args += ["--stats=1m"]
    args += ["--retries=10", "--low-level-retries=20"]
    return args


def run_rclone_with_progress(command: list[str], progress: Progress, task_id, base_desc: str,
                              logger: Logger) -> int:
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        logger.raw(line)
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        stats = obj.get("stats")
        if not isinstance(stats, dict):
            continue

        done = stats.get("bytes", 0) or 0
        total = stats.get("totalBytes", 0) or 0
        pct = (done / total * 100) if total else 0

        transferring = stats.get("transferring") or []
        current = transferring[0].get("name") if transferring else None
        desc = f"{base_desc} \u203a {Path(current).name}" if current else base_desc

        progress.update(task_id, completed=min(pct, 100), description=desc)
    proc.wait()
    progress.update(task_id, completed=100, description=base_desc)
    return proc.returncode


def run_rclone_plain(command: list[str], logger: Logger) -> int:
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line:
            logger.raw(line)
    proc.wait()
    return proc.returncode


def execute_transfer(command: list[str], base_desc: str, progress: Optional[Progress],
                      interactive: bool, logger: Logger) -> int:
    if interactive and progress is not None:
        task_id = progress.add_task(base_desc, total=100)
        rc = run_rclone_with_progress(command, progress, task_id, base_desc, logger)
        progress.remove_task(task_id)
    else:
        console.print(f"\u2192 {base_desc}")
        rc = run_rclone_plain(command, logger)

    if rc == 0:
        console.print(f"[green]\u2713[/green] {base_desc}")
    else:
        console.print(f"[red]\u2717[/red] {base_desc} [dim](exit {rc})[/dim]")
    return rc


# ---------------------------------------------------------------------------
# Git-aware backup
# ---------------------------------------------------------------------------

def find_git_roots(path: Path) -> list[Path]:
    if (path / ".git").is_dir():
        return [path]
    roots = []
    result = subprocess.run(
        ["find", str(path), "-type", "d", "-name", ".git", "-prune", "-print"],
        capture_output=True, text=True, check=False,
    )
    for gitdir in result.stdout.splitlines():
        if gitdir:
            roots.append(Path(gitdir).parent)
    return roots


def backup_git_repo(repo: Path, destination: str, common_args: list[str], list_max_files: Optional[int],
                     progress: Optional[Progress], interactive: bool, logger: Logger) -> tuple[int, bool]:
    """Returns (returncode, skipped)."""
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        logger.log(f"ERROR: Failed to enumerate Git files: {repo}")
        return 1, False

    entries = [e for e in result.stdout.split(b"\x00") if e]
    base_desc = f"{destination.rsplit('/', 1)[-1]} (git: {repo.name})"

    if not entries:
        return 0, False

    if list_max_files is not None and len(entries) > list_max_files:
        console.print(f"[yellow]\u26a0[/yellow] {base_desc} \u2014 {len(entries)} files exceeds "
                       f"listmaxfiles ({list_max_files}), skipping")
        logger.log_file_only(f"SKIPPED (listmaxfiles): {repo} has {len(entries)} tracked files")
        return 0, True

    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tf.write(result.stdout)
        include_file = tf.name

    try:
        command = [
            "rclone", "copy", f"{repo}/", f"{destination}/",
            f"--files-from0={include_file}", "--no-traverse",
        ] + common_args
        rc = execute_transfer(command, base_desc, progress, interactive, logger)
    finally:
        os.unlink(include_file)

    return rc, False


def backup_normal(source: Path, destination: str, mode: str, common_args: list[str],
                   base_desc: str, progress: Optional[Progress], interactive: bool, logger: Logger) -> int:
    verb = "copy" if mode == "copy" else "move"
    command = ["rclone", verb, f"{source}/", f"{destination}/"] + common_args
    return execute_transfer(command, base_desc, progress, interactive, logger)


def backup_non_git_contents(source_root: Path, destination: str, git_roots: list[Path],
                             common_args: list[str], base_desc: str, progress: Optional[Progress],
                             interactive: bool, logger: Logger) -> int:
    command = ["rclone", "copy", f"{source_root}/", f"{destination}/"]
    for repo in git_roots:
        try:
            relative = repo.relative_to(source_root)
        except ValueError:
            continue
        if str(relative) not in (".", ""):
            command.append(f"--exclude=/{relative}/**")
    command += common_args
    return execute_transfer(command, base_desc, progress, interactive, logger)


# ---------------------------------------------------------------------------
# Archive mode
#
# When run.archive is enabled, each library is compressed whole into one
# dated .7z (every configured dir, gitignore/mode/listmaxfiles are all
# irrelevant here — everything just gets zipped) and that single file is
# uploaded instead of syncing files directly. Compression itself has no
# live progress (7z's own output isn't reliably machine-parseable across
# versions), so only the upload step gets a progress bar.
# ---------------------------------------------------------------------------

def build_archive(library: Library, remote: str, common_args: list[str], staging_dir: Path,
                   progress: Optional[Progress], interactive: bool, logger: Logger) -> int:
    date_str = datetime.now().strftime("%Y-%m-%d")
    archive_path = staging_dir / f"{library.name}-{date_str}.7z"
    base_desc = f"{library.name} (archive)"

    console.print(f"[cyan]\u2026[/cyan] {base_desc} \u2014 compressing")

    any_source = False
    for entry in library.entries:
        if not entry.source.exists():
            console.print(f"[yellow]\u26a0[/yellow] {base_desc} \u2014 source not found, skipping: {entry.source}")
            logger.log_file_only(f"ERROR: Source does not exist: {entry.source}")
            continue

        any_source = True
        cmd = ["7z", "a", "-y", str(archive_path), str(entry.source)]
        logger.raw(f"7z: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        for line in (result.stdout or "").splitlines():
            logger.raw(line)
        if result.returncode != 0:
            console.print(f"[red]\u2717[/red] {base_desc} \u2014 7z failed on {entry.source}")
            logger.log_file_only(f"ERROR: 7z failed on {entry.source}: {result.stderr.strip()}")
            return 1

    if not any_source or not archive_path.exists():
        console.print(f"[yellow]\u26a0[/yellow] {base_desc} \u2014 nothing to archive")
        return 0

    destination = f"{remote}/{library.name}"
    command = ["rclone", "copy", str(archive_path), f"{destination}/"] + common_args
    rc = execute_transfer(command, base_desc, progress, interactive, logger)

    try:
        archive_path.unlink()
    except OSError:
        pass

    return rc


# ---------------------------------------------------------------------------
# Summary banner
# ---------------------------------------------------------------------------

def print_summary(run_cfg: dict, libraries: list[Library], dry_run: bool, remote: str,
                   list_max_files: Optional[int]) -> None:
    console.print()
    for line in BUZM_ART:
        console.print(Text(line, style="cyan"))
    console.print()

    total_dirs = sum(len(lib.entries) for lib in libraries)

    console.print(f"[bold]Destination[/bold]   {remote}")
    console.print(f"[bold]Mode[/bold]          {run_cfg.get('mode', 'rclone')}")
    console.print(f"[bold]Run[/bold]           {'[yellow]DRY RUN[/yellow]' if dry_run else '[green]LIVE[/green]'}")
    if run_cfg.get("archive"):
        console.print("[bold]Archive[/bold]       [yellow]enabled \u2014 each library is zipped whole into a "
                       "dated .7z and uploaded[/yellow]")
    else:
        console.print("[bold]Archive[/bold]       disabled")
    if list_max_files is not None:
        console.print(f"[bold]Max files[/bold]     {list_max_files} per directory")
    console.print(f"[bold]Libraries[/bold]     {len(libraries)} ({total_dirs} total dirs)")
    for lib in libraries:
        names = ", ".join(e.dest_name for e in lib.entries) if lib.entries else "(none)"
        console.print(f"  [magenta]{lib.name}[/magenta] \u2014 {len(lib.entries)} dir(s): {names}")
    console.print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="scw-backup",
        description="rclone-based offsite backup to SCW bulk storage.",
    )
    p.add_argument("-n", "--dry-run", action="store_true",
                    help="Show what would be transferred without changing anything")
    p.add_argument("-v", "--verbose", action="store_true",
                    help="Show rclone verbose output")
    p.add_argument("--plain", "--cli", dest="plain", action="store_true",
                    help="Force plain line-by-line output (no live progress bars)")
    return p.parse_args()


def check_required_commands() -> None:
    missing = [c for c in REQUIRED_COMMANDS if shutil.which(c) is None]
    if missing:
        for c in missing:
            print(f"ERROR: Required command not found: {c}", file=sys.stderr)
        sys.exit(1)


def acquire_lock():
    fh = open(LOCKFILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN):
            console.print("[bold red]Another scw-backup process is already running.[/bold red]")
            sys.exit(1)
        raise
    return fh


def main() -> int:
    args = parse_args()

    if not CONFIG_PATH.is_file():
        print(f"ERROR: Config not found: {CONFIG_PATH}", file=sys.stderr)
        return 1

    check_required_commands()
    _lock_fh = acquire_lock()

    run_cfg, libraries = load_config(CONFIG_PATH)

    run_mode = run_cfg.get("mode", "rclone")
    if run_mode != "rclone":
        console.print(f"[bold red]ERROR:[/bold red] run.mode '{run_mode}' isn't supported yet "
                       f"(only 'rclone' is implemented).")
        return 1

    remote = os.environ.get("SCWBAK_REMOTE") or run_cfg.get("destination")
    if not remote:
        console.print("[bold red]ERROR:[/bold red] No destination set (run.destination in config, "
                       "or SCWBAK_REMOTE env var).")
        return 1

    list_max_files = run_cfg.get("listmaxfiles")
    if list_max_files is not None:
        list_max_files = int(list_max_files)

    archive_mode = bool(run_cfg.get("archive"))
    if archive_mode and shutil.which("7z") is None:
        console.print("[bold red]ERROR:[/bold red] run.archive is enabled but the '7z' command was not found.")
        return 1

    if not libraries:
        console.print("[yellow]WARNING:[/yellow] No libraries found in config.")
        return 0

    interactive = console.is_terminal and not args.plain

    LOGDIR.mkdir(parents=True, exist_ok=True)
    logfile = LOGDIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    logger = Logger(logfile)

    def handle_sigterm(signum, frame):
        sys.exit(143)
    signal.signal(signal.SIGTERM, handle_sigterm)

    logger.log_file_only("========================================")
    logger.log_file_only("SCW backup started")
    logger.log_file_only(f"Config: {CONFIG_PATH}")
    logger.log_file_only(f"Destination: {remote}")
    if args.dry_run:
        logger.log_file_only("MODE: DRY RUN")

    print_summary(run_cfg, libraries, args.dry_run, remote, list_max_files)

    common_args = build_rclone_args(args.dry_run, args.verbose, interactive)

    processed = 0
    failures = 0

    columns = [
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(bar_width=30),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    ]
    progress_cm = Progress(*columns, console=console, transient=False) if interactive else contextlib.nullcontext()

    with progress_cm as progress:
        if archive_mode:
            staging_dir = Path(tempfile.mkdtemp(prefix="scwbak-archive-"))
            try:
                for lib in libraries:
                    if not lib.entries:
                        logger.log_file_only(f"WARNING: Library '{lib.name}' has no dirs; skipping.")
                        continue

                    console.rule(f"[bold magenta]{lib.name}[/bold magenta]", style="dim")

                    rc = build_archive(lib, remote, common_args, staging_dir, progress, interactive, logger)
                    if rc != 0:
                        failures += 1
                    processed += 1
            finally:
                shutil.rmtree(staging_dir, ignore_errors=True)
        else:
            for lib in libraries:
                if not lib.entries:
                    logger.log_file_only(f"WARNING: Library '{lib.name}' has no dirs; skipping.")
                    continue

                console.rule(f"[bold magenta]{lib.name}[/bold magenta]", style="dim")

                for entry in lib.entries:
                    base_desc = f"{lib.name}/{entry.dest_name}"

                    if not entry.source.exists():
                        console.print(f"[red]\u2717[/red] {base_desc} \u2014 source not found: {entry.source}")
                        logger.log_file_only(f"ERROR: Source does not exist: {entry.source}")
                        failures += 1
                        continue

                    if entry.mode not in ("copy", "move"):
                        console.print(f"[red]\u2717[/red] {base_desc} \u2014 invalid mode '{entry.mode}'")
                        failures += 1
                        continue

                    destination = f"{remote}/{lib.name}/{entry.dest_name}"
                    logger.log_file_only(f"[{entry.mode}] {entry.source} -> {destination} "
                                          f"(gitignore={entry.gitignore})")

                    if entry.gitignore:
                        if entry.mode == "move":
                            console.print(f"[red]\u2717[/red] {base_desc} \u2014 gitignore+move unsupported")
                            failures += 1
                            continue

                        git_roots = find_git_roots(entry.source)

                        if not git_roots:
                            count = count_files_excluding(entry.source, [], list_max_files)
                            if list_max_files is not None and count > list_max_files:
                                console.print(f"[yellow]\u26a0[/yellow] {base_desc} \u2014 {count} files exceeds "
                                              f"listmaxfiles ({list_max_files}), skipping")
                                logger.log_file_only(f"SKIPPED (listmaxfiles): {entry.source} has {count} files")
                            else:
                                rc = backup_normal(entry.source, destination, entry.mode, common_args,
                                                    base_desc, progress, interactive, logger)
                                if rc != 0:
                                    failures += 1
                        else:
                            if entry.source not in git_roots or len(git_roots) > 1:
                                count = count_files_excluding(entry.source, git_roots, list_max_files)
                                if list_max_files is not None and count > list_max_files:
                                    console.print(f"[yellow]\u26a0[/yellow] {base_desc} (misc) \u2014 {count} files "
                                                  f"exceeds listmaxfiles ({list_max_files}), skipping")
                                    logger.log_file_only(f"SKIPPED (listmaxfiles): {entry.source} misc content "
                                                          f"has {count} files")
                                else:
                                    rc = backup_non_git_contents(entry.source, destination, git_roots, common_args,
                                                                  f"{base_desc} (misc)", progress, interactive, logger)
                                    if rc != 0:
                                        failures += 1

                            for repo in git_roots:
                                repo_destination = f"{destination}/{repo.name}"
                                rc, skipped = backup_git_repo(repo, repo_destination, common_args, list_max_files,
                                                               progress, interactive, logger)
                                if rc != 0 and not skipped:
                                    failures += 1
                    else:
                        count = count_files_excluding(entry.source, [], list_max_files)
                        if list_max_files is not None and count > list_max_files:
                            console.print(f"[yellow]\u26a0[/yellow] {base_desc} \u2014 {count} files exceeds "
                                          f"listmaxfiles ({list_max_files}), skipping")
                            logger.log_file_only(f"SKIPPED (listmaxfiles): {entry.source} has {count} files")
                        else:
                            rc = backup_normal(entry.source, destination, entry.mode, common_args,
                                                base_desc, progress, interactive, logger)
                            if rc != 0:
                                failures += 1

                    processed += 1

    console.print()
    console.print(f"[bold]Processed:[/bold] {processed}   [bold]Failures:[/bold] {failures}")
    console.print(f"[dim]Log:[/dim] {logfile}")

    if failures > 0:
        logger.log_file_only("SCW backup finished WITH ERRORS.")
        logger.close()
        return 1
    else:
        logger.log_file_only("SCW backup completed successfully.")
        logger.close()
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
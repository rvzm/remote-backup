#!/usr/bin/env python3
"""
buzm — rclone and rsync offsite backup to remote storage.

Includes options for SCW Bulk remote (rclone), FTP, SFTP, rsync, and local mounts.

Supports Git-aware backups (only tracked files, ignoring .gitignored content) and optional
per-library compression into dated .7z archives.

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
import re
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
from urllib.parse import urlparse

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
from rich.markup import escape
from rich.text import Text

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

CONFIG_NAME = "config.yml"
EXAMPLE_CONFIG_NAME = "example.config.yml"
HOME_DIR = Path.home() / ".buzm"
LOCKFILE = Path("/tmp/buzm.lock")
LOGDIR = HOME_DIR / "logs"

# The transfer engine is implied by the destination type; there is no separate
# key for it. "fs" drives coreutils directly for removable-media backups.
ENGINE_BY_TYPE = {
    "scw": "rclone",
    "ftp": "rclone",
    "sftp": "rclone",
    "rsync": "rsync",
    "local": "fs",
}
IMPLEMENTED_TYPES = ("scw", "local", "ftp", "sftp", "rsync")
AUTH_METHODS = ("password", "sshkey", "public")

# Which URL schemes each type accepts. Types absent from this map take a bare
# destination (an rclone remote for scw, a filesystem path for local).
SCHEMES_BY_TYPE = {
    "sftp": ("sftp",),
    "ftp": ("ftp", "ftps"),
    "rsync": ("rsync", "ssh"),
}
DEFAULT_PORTS = {"sftp": 22, "ftp": 21, "ftps": 21, "rsync": 873, "ssh": 22}

# Needed no matter which engine runs; each engine adds its own on top.
REQUIRED_COMMANDS = ["git", "find"]
ENGINE_COMMANDS = {
    "rclone": ["rclone"],
    "fs": ["cp", "rm", "mkdir", "xargs", "findmnt"],
    "rsync": ["rsync"],
}

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

class ConfigError(Exception):
    """Raised for anything wrong in the config file; reported without a traceback."""


@dataclass
class Destination:
    """A parsed destination. For scw and local only `raw` and `path` are set."""
    raw: str
    scheme: Optional[str] = None
    user: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    path: str = ""

    @property
    def display(self) -> str:
        return self.raw


@dataclass
class Secret:
    """A resolved credential. `origin` describes where it came from so the value
    itself never has to be shown; repr and str are overridden so it cannot leak
    through a traceback or an accidental interpolation."""
    value: str
    origin: str

    def __repr__(self) -> str:
        return f"Secret(origin={self.origin!r})"

    def __str__(self) -> str:
        return "<secret>"


@dataclass
class RunConfig:
    destination: str
    type: str
    engine: str
    auth_method: str
    auth_param: Optional[str]
    archive: bool
    list_max_files: Optional[int]
    tls: bool = True
    dest: Optional[Destination] = None
    secret: Optional[Secret] = None


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


def config_search_paths(explicit: Optional[str]) -> list[Path]:
    """First hit wins. An explicit --config or $BUZM_CONFIG is used on its own, so
    a typo fails loudly rather than silently falling through to another config."""
    if explicit:
        return [Path(explicit).expanduser()]
    env = os.environ.get("BUZM_CONFIG")
    if env:
        return [Path(env).expanduser()]
    return [
        Path.cwd() / CONFIG_NAME,
        Path(__file__).resolve().parent / CONFIG_NAME,
        HOME_DIR / CONFIG_NAME,
    ]


def resolve_config_path(explicit: Optional[str]) -> Path:
    candidates = config_search_paths(explicit)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    looked = "\n".join(f"         {c}" for c in candidates)
    raise ConfigError(
        f"No config file found. Looked in:\n{looked}\n"
        f"       Copy {EXAMPLE_CONFIG_NAME} to {CONFIG_NAME} and edit it to get started."
    )


def parse_auth(raw) -> tuple[str, Optional[str]]:
    """auth is "method" or "method:parameter", e.g. "sshkey:~/.ssh/id_ed25519".
    Validated here so a typo surfaces now; nothing consumes it until the types
    that need credentials (ftp, sftp, rsync) are implemented."""
    if raw is None:
        return "public", None
    if not isinstance(raw, str):
        raise ConfigError(f"run.auth must be a string, got {type(raw).__name__}.")
    method, _, param = raw.partition(":")
    method = method.strip().lower()
    param = param.strip() or None
    if method not in AUTH_METHODS:
        raise ConfigError(f"run.auth method '{method}' is not valid. "
                          f"Choose one of: {', '.join(AUTH_METHODS)}.")
    return method, param


def parse_destination(destination: str, dest_type: str) -> Destination:
    """scw and local take a bare destination (an rclone remote, a filesystem
    path). The network types take a URL, whose scheme must agree with the type —
    a mismatch is a config error rather than a silent reinterpretation.

    For rsync the scheme is load-bearing: rsync:// is daemon mode and ssh:// is
    transport over ssh. They need different auth and different commands, and the
    bare user@host:path form cannot tell them apart."""
    schemes = SCHEMES_BY_TYPE.get(dest_type)
    if schemes is None:
        return Destination(raw=destination, path=destination)

    # Check for "://" rather than trusting urlparse's scheme: it happily reads
    # "host:/path" as scheme "host", which would produce a baffling error.
    if "://" not in destination:
        raise ConfigError(
            f"run.destination for type '{dest_type}' must be a URL, e.g.\n"
            f"       {example_destination(dest_type)}"
        )
    parsed = urlparse(destination)
    if parsed.scheme not in schemes:
        raise ConfigError(
            f"run.destination uses scheme '{parsed.scheme}://' but run.type is "
            f"'{dest_type}', which expects {' or '.join(s + '://' for s in schemes)}."
        )
    if parsed.password:
        raise ConfigError(
            "run.destination must not carry a password. Put it in run.auth instead "
            "(auth: password, password:@file, or password:<literal>) so it stays out "
            "of logs and process arguments."
        )
    if not parsed.hostname:
        raise ConfigError(f"run.destination is missing a host: {destination}")

    return Destination(
        raw=destination,
        scheme=parsed.scheme,
        user=parsed.username or None,
        host=parsed.hostname,
        port=parsed.port or DEFAULT_PORTS.get(parsed.scheme),
        path=parsed.path or "/",
    )


def example_destination(dest_type: str) -> str:
    return {
        "sftp": "sftp://user@host:22/home/user/backup",
        "ftp": "ftp://user@host/backup   (or ftps:// to force TLS)",
        "rsync": "rsync://host/module/path   (daemon)  or  ssh://user@host/path",
    }.get(dest_type, "")


def warn_bad_permissions(path: Path, what: str) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        console.print(f"[yellow]WARNING:[/yellow] {what} {escape(str(path))} is readable by "
                      f"other users. Fix with: chmod 600 {escape(str(path))}")


def warn_if_backed_up(path: Path, libraries: list[Library], what: str) -> None:
    """buzm backs up home directories. A credential sitting inside one of the
    configured sources would be copied to the remote in cleartext by buzm itself,
    which is worth saying out loud rather than leaving to be discovered."""
    try:
        resolved = path.resolve()
    except OSError:
        return
    for lib in libraries:
        for entry in lib.entries:
            try:
                resolved.relative_to(entry.source.expanduser().resolve())
            except (ValueError, OSError):
                continue
            console.print(f"[yellow]WARNING:[/yellow] {what} {escape(str(path))} is inside a "
                          f"backed-up directory ({escape(str(entry.source))}) and will be sent "
                          f"to the remote in cleartext.")
            return


def resolve_secret(method: str, param: Optional[str], config_path: Path,
                    libraries: list[Library]) -> Optional[Secret]:
    """Only 'password' produces a secret. 'sshkey' carries a key *path*, which is
    not itself sensitive, and 'public' needs nothing."""
    if method != "password":
        return None

    if param is None:
        value = os.environ.get("BUZM_PASSWORD")
        if not value:
            raise ConfigError(
                "auth: password needs a password. Set $BUZM_PASSWORD, or use\n"
                "       auth: password:@/path/to/file, or auth: password:<literal>."
            )
        return Secret(value, "$BUZM_PASSWORD")

    if param.startswith("@"):
        path = Path(param[1:]).expanduser()
        if not path.is_file():
            raise ConfigError(f"auth password file not found: {path}")
        warn_bad_permissions(path, "password file")
        warn_if_backed_up(path, libraries, "password file")
        lines = path.read_text().splitlines()
        if not lines or not lines[0].strip():
            raise ConfigError(f"auth password file is empty: {path}")
        return Secret(lines[0].strip(), f"file {path}")

    warn_bad_permissions(config_path, "config file")
    warn_if_backed_up(config_path, libraries, "config file with an inline password")
    return Secret(param, "config (inline)")


def rclone_obscure(plaintext: str) -> str:
    """rclone's --*-pass options want its obscured form. Feed the plaintext on
    stdin so it never becomes a command-line argument."""
    result = subprocess.run(["rclone", "obscure", "-"], input=plaintext, text=True,
                             capture_output=True, check=False)
    if result.returncode != 0:
        raise ConfigError(f"rclone obscure failed: {result.stderr.strip()}")
    return result.stdout.strip()


def redact(text: str, secrets: list[str]) -> str:
    for value in secrets:
        if value:
            text = text.replace(value, "<redacted>")
    return text


def parse_run_config(run_cfg: dict, config_path: Path, libraries: list[Library]) -> RunConfig:
    if "mode" in run_cfg:
        console.print("[yellow]WARNING:[/yellow] run.mode is no longer used \u2014 the engine is "
                      "implied by run.type. Remove it from your config.")

    raw_type = run_cfg.get("type")
    if raw_type is None:
        raise ConfigError(f"run.type is required. Choose one of: {', '.join(ENGINE_BY_TYPE)}.")
    dest_type = str(raw_type).strip().lower()
    if dest_type not in ENGINE_BY_TYPE:
        raise ConfigError(f"run.type '{dest_type}' is not a known type. "
                          f"Choose one of: {', '.join(ENGINE_BY_TYPE)}.")
    if dest_type not in IMPLEMENTED_TYPES:
        raise ConfigError(
            f"run.type '{dest_type}' is recognized but not implemented yet.\n"
            f"       Supported today: {', '.join(IMPLEMENTED_TYPES)}."
        )

    destination = os.environ.get("BUZM_REMOTE") or run_cfg.get("destination")
    if not destination:
        raise ConfigError("No destination set (run.destination in config, or the "
                          "BUZM_REMOTE env var).")

    auth_method, auth_param = parse_auth(run_cfg.get("auth"))

    list_max_files = run_cfg.get("listmaxfiles")
    if list_max_files is not None:
        try:
            list_max_files = int(list_max_files)
        except (TypeError, ValueError):
            raise ConfigError(f"run.listmaxfiles must be a whole number, got {list_max_files!r}.")

    dest = parse_destination(str(destination), dest_type)

    # ftps:// is sugar for tls: true; an explicit tls: false alongside it is a
    # contradiction rather than something to silently resolve.
    tls = bool(run_cfg.get("tls", True))
    if dest.scheme == "ftps" and "tls" in run_cfg and not tls:
        raise ConfigError("run.destination uses ftps:// but run.tls is false. Pick one.")
    if dest.scheme == "ftps":
        tls = True

    secret = resolve_secret(auth_method, auth_param, config_path, libraries)

    if dest_type == "rsync" and dest.scheme == "ssh" and auth_method == "password":
        raise ConfigError(
            "auth: password is not supported for rsync over ssh \u2014 it would need sshpass "
            "and would put the password on a command line.\n"
            "       Use auth: sshkey (agent) or auth: sshkey:/path/to/key, or switch the "
            "destination to an rsync:// daemon URL."
        )

    return RunConfig(
        destination=str(destination),
        type=dest_type,
        engine=ENGINE_BY_TYPE[dest_type],
        auth_method=auth_method,
        auth_param=auth_param,
        archive=bool(run_cfg.get("archive")),
        list_max_files=list_max_files,
        tls=tls,
        dest=dest,
        secret=secret,
    )


def walk_files(root: Path, exclude: list[Path]):
    """Yield every file under root as a path relative to root, pruning the
    excluded subtrees. Lazy on purpose: callers that only need a count can stop
    early, and the fs engine needs the paths themselves."""
    exclude_set = {str(p) for p in exclude}
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str):
        dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) not in exclude_set]
        rel_dir = os.path.relpath(dirpath, root_str)
        prefix = "" if rel_dir == "." else rel_dir + os.sep
        for name in filenames:
            yield prefix + name


def count_files_excluding(root: Path, exclude: list[Path], cap: Optional[int]) -> int:
    """Count files under root, pruning any excluded subtrees. Stops early once
    the count exceeds cap (when cap is set), so a huge directory with a low cap
    doesn't require a full walk."""
    count = 0
    for _ in walk_files(root, exclude):
        count += 1
        if cap is not None and count > cap:
            return count
    return count


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class Logger:
    def __init__(self, logfile: Path, secrets: Optional[list[str]] = None):
        self.logfile = logfile
        self.logfile.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.logfile, "a", buffering=1)
        # Every credential in play, so no path into the log can carry one even if
        # a transfer tool echoes it back at us.
        self.secrets = [v for v in (secrets or []) if v]

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log(self, msg: str, echo: bool = True) -> None:
        line = f"[{self._timestamp()}] {redact(msg, self.secrets)}"
        self._fh.write(line + "\n")
        if echo:
            console.print(line)

    def log_file_only(self, msg: str) -> None:
        self.log(msg, echo=False)

    def raw(self, line: str) -> None:
        self._fh.write(f"[{self._timestamp()}] {redact(line, self.secrets)}\n")

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

@dataclass
class Transfer:
    """One engine-agnostic unit of work. `execute_plan` runs it; the per-engine
    builders below are the only code that knows what the command looks like."""
    command: list[str] = field(default_factory=list)
    cwd: Optional[Path] = None
    env: dict = field(default_factory=dict)
    progress: Optional[str] = None          # "json" | "lines" | "percent"
    line_total: Optional[int] = None
    post: list = field(default_factory=list)      # [(command, cwd)] run only after success
    cleanup: list = field(default_factory=list)   # temp files to remove afterwards
    skip: Optional[str] = None              # nothing to do; report and move on
    note: Optional[str] = None              # dry-run description, for engines with no --dry-run
    error: Optional[str] = None             # could not be built


def build_common_args(run: RunConfig, dry_run: bool, verbose: bool,
                       interactive: bool) -> tuple[list[str], dict]:
    """Flags and environment shared by every transfer this run makes. Credentials
    go in the environment, never in the argument list, because /proc/*/cmdline is
    readable by every user on the machine."""
    env: dict = {}

    if run.engine == "rclone":
        args: list[str] = []
        if dry_run:
            args.append("--dry-run")
        args.append("-v" if verbose else "--log-level=NOTICE")
        args += ["--use-json-log", "--stats=1s"] if interactive else ["--stats=1m"]
        args += ["--retries=10", "--low-level-retries=20"]
        args += rclone_connection_args(run, env)
        return args, env

    if run.engine == "rsync":
        args = ["-a", "--mkpath"]
        if dry_run:
            args.append("--dry-run")
        if verbose:
            args.append("-v")
        if interactive:
            # progress2 emits a carriage-return-terminated stream; useful for the bar
            # but pure noise in the log, so it is only asked for when a bar exists.
            args.append("--info=progress2")
        args += rsync_connection_args(run, env)
        return args, env

    return [], env      # fs builds its own commands wholesale


def rclone_connection_args(run: RunConfig, env: dict) -> list[str]:
    """scw uses a remote already configured with `rclone config`; ftp and sftp are
    configured here, on the fly. Host, user and port are not secret and stay on the
    command line where they show up in logs for debugging."""
    if run.type not in ("ftp", "sftp"):
        return []

    d = run.dest
    kind = run.type
    args = [f"--{kind}-host={d.host}"]
    if d.user:
        args.append(f"--{kind}-user={d.user}")
    if d.port:
        args.append(f"--{kind}-port={d.port}")

    if kind == "sftp":
        # rclone's sftp backend does no host-key verification unless told to, which
        # would silently trust any server answering on that address. $BUZM_KNOWN_HOSTS
        # covers setups that keep the file somewhere other than the usual place.
        known = os.environ.get("BUZM_KNOWN_HOSTS") or str(Path.home() / ".ssh" / "known_hosts")
        args.append(f"--sftp-known-hosts-file={Path(known).expanduser()}")
        if run.auth_method == "sshkey":
            if run.auth_param:
                args.append(f"--sftp-key-file={Path(run.auth_param).expanduser()}")
            else:
                args.append("--sftp-key-use-agent")
    elif run.tls:
        args.append("--ftp-explicit-tls")

    if run.secret is not None:
        env[f"RCLONE_{kind.upper()}_PASS"] = rclone_obscure(run.secret.value)
    return args


def rsync_connection_args(run: RunConfig, env: dict) -> list[str]:
    d = run.dest
    if d.scheme == "ssh":
        ssh = ["ssh", "-o", "BatchMode=yes"]     # fail rather than hang on a prompt
        if d.port and d.port != 22:
            ssh += ["-p", str(d.port)]
        if run.auth_method == "sshkey" and run.auth_param:
            ssh += ["-i", str(Path(run.auth_param).expanduser())]
        return ["-e", " ".join(ssh)]

    if run.secret is not None:
        env["RSYNC_PASSWORD"] = run.secret.value
    return []


def engine_remote_base(run: RunConfig) -> str:
    """The destination prefix libraries hang off. Everything downstream keeps
    appending /<library>/<dir> to this, exactly as it did when scw was the only
    destination."""
    d = run.dest
    if run.type in ("ftp", "sftp"):
        return f":{run.type}:{d.path.rstrip('/')}"
    if run.type == "rsync":
        if d.scheme == "ssh":
            return f"{d.user + '@' if d.user else ''}{d.host}:{d.path.rstrip('/')}"
        return run.destination.rstrip("/")       # rsync:// goes to rsync verbatim
    return run.destination


def write_null_list(rel_paths: list[str]) -> str:
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tf.write(b"".join(p.encode() + b"\x00" for p in rel_paths))
        return tf.name


def relative_excludes(source: Path, excludes: list[Path]) -> list[str]:
    rels = []
    for path in excludes or []:
        try:
            rel = path.relative_to(source)
        except ValueError:
            continue
        if str(rel) not in (".", ""):
            rels.append(str(rel))
    return rels


def build_transfer(run: RunConfig, kind: str, source: Path, destination: str, mode: str,
                    common_args: list[str], env: dict, dry_run: bool,
                    rel_paths: Optional[list[str]] = None,
                    excludes: Optional[list[Path]] = None) -> Transfer:
    """kind is "tree", "tree_excluding", "file_list" or "single_file"."""
    if run.engine == "fs":
        return _fs_transfer(run, kind, source, destination, mode, dry_run, rel_paths, excludes)
    if run.engine == "rsync":
        return _rsync_transfer(run, kind, source, destination, mode, common_args, env,
                                rel_paths, excludes)
    return _rclone_transfer(run, kind, source, destination, mode, common_args, env,
                             rel_paths, excludes)


def _rclone_transfer(run, kind, source, destination, mode, common_args, env,
                      rel_paths, excludes) -> Transfer:
    cleanup: list = []
    if kind == "file_list":
        if not rel_paths:
            return Transfer(skip="nothing to copy")
        listfile = write_null_list(rel_paths)
        cleanup.append(listfile)
        command = ["rclone", "copy", f"{source}/", f"{destination}/",
                   f"--files-from0={listfile}", "--no-traverse"] + common_args
    elif kind == "single_file":
        command = ["rclone", "copy", str(source), f"{destination}/"] + common_args
    else:
        verb = "copy" if mode == "copy" else "move"
        command = ["rclone", verb, f"{source}/", f"{destination}/"]
        command += [f"--exclude=/{rel}/**" for rel in relative_excludes(source, excludes)]
        command += common_args
    return Transfer(command=command, env=env, progress="json", cleanup=cleanup)


def _rsync_transfer(run, kind, source, destination, mode, common_args, env,
                     rel_paths, excludes) -> Transfer:
    cleanup: list = []
    post: list = []

    if kind == "file_list":
        if not rel_paths:
            return Transfer(skip="nothing to copy")
        listfile = write_null_list(rel_paths)
        cleanup.append(listfile)
        command = (["rsync"] + common_args +
                   ["--from0", f"--files-from={listfile}", f"{source}/", f"{destination}/"])
    elif kind == "single_file":
        command = ["rsync"] + common_args + [str(source), f"{destination}/"]
    else:
        command = ["rsync"] + common_args
        command += [f"--exclude=/{rel}/**" for rel in relative_excludes(source, excludes)]
        if mode == "move":
            # rsync unlinks each source file only once it has transferred, giving
            # the same "a failed transfer costs no data" property the fs engine has.
            command.append("--remove-source-files")
            post.append((["find", str(source), "-mindepth", "1", "-type", "d",
                          "-empty", "-delete"], None))
        command += [f"{source}/", f"{destination}/"]

    return Transfer(command=command, env=env, progress="percent", post=post, cleanup=cleanup)


def _fs_transfer(run, kind, source, destination, mode, dry_run, rel_paths, excludes) -> Transfer:
    """coreutils, for removable media. Everything reduces to a NUL-delimited list of
    paths relative to a source directory, fed to cp through xargs."""
    cwd = source
    if kind == "single_file":
        cwd, rel_paths = source.parent, [source.name]
    elif rel_paths is None:
        rel_paths = list(walk_files(source, excludes or []))

    if not rel_paths:
        return Transfer(skip="nothing to copy")

    dest = Path(destination).expanduser()
    if dry_run:
        # cp has no --dry-run, so report rather than execute.
        return Transfer(note=f"would {mode} {len(rel_paths)} file(s) to {dest}")

    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return Transfer(error=f"cannot create {dest}: {e}")

    listfile = write_null_list(rel_paths)
    # --reflink=auto makes same-filesystem copies near-instant on btrfs/xfs and falls
    # back silently on exFAT/ext4; -u leaves files the destination already has current
    # alone; --parents rebuilds the tree; xargs keeps a long list under ARG_MAX.
    command = ["xargs", "-0", "-a", listfile,
               "cp", "-a", "--reflink=auto", "-u", "--parents", "-v", "-t", str(dest), "--"]

    post: list = []
    if mode == "move":
        post = [(["xargs", "-0", "-a", listfile, "rm", "-f", "--"], cwd),
                (["find", str(source), "-mindepth", "1", "-type", "d", "-empty", "-delete"], None)]

    return Transfer(command=command, cwd=cwd, progress="lines", line_total=len(rel_paths),
                    post=post, cleanup=[listfile])


# ---------------------------------------------------------------------------
# Running a Transfer
#
# --log-file is deliberately never passed to rclone: it redirects rclone's
# *entire* logger sink (including the --use-json-log stats stream) to that
# file instead of stdout/stderr, which is exactly the stream we need to
# read to drive the live progress bar. Instead we capture combined
# stdout+stderr ourselves and persist every raw line via Logger.raw().
# ---------------------------------------------------------------------------

def _popen(t: Transfer, env: Optional[dict]):
    return subprocess.Popen(t.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, cwd=str(t.cwd) if t.cwd else None, env=env)


def run_json_progress(t: Transfer, progress: Progress, task_id, base_desc: str,
                       logger: Logger, env: Optional[dict]) -> int:
    """rclone's --use-json-log stats are an authoritative snapshot each tick: an
    item simply disappears from transferring[] once done, so there is no completion
    event to track."""
    proc = _popen(t, env)
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


def run_line_progress(t: Transfer, progress: Progress, task_id, base_desc: str,
                       logger: Logger, env: Optional[dict]) -> int:
    """coreutils has no stats stream, so drive the bar off the one line `cp -v`
    prints per file, counted against a total we already walked for. Only quoted
    lines are files \u2014 cp also announces created directories unquoted, and those
    would inflate the count."""
    proc = _popen(t, env)
    assert proc.stdout is not None
    total = t.line_total or 0
    seen = 0
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        logger.raw(line)
        if not line.startswith("'"):
            continue
        seen += 1
        name = line.split("'")[1] if line.count("'") >= 2 else None
        desc = f"{base_desc} \u203a {Path(name).name}" if name else base_desc
        progress.update(task_id, completed=min(seen / total * 100, 100) if total else 100,
                        description=desc)
    proc.wait()
    progress.update(task_id, completed=100, description=base_desc)
    return proc.returncode


PERCENT_RE = re.compile(r"(\d+)%")


def run_percent_progress(t: Transfer, progress: Progress, task_id, base_desc: str,
                          logger: Logger, env: Optional[dict]) -> int:
    """rsync --info=progress2 writes an overall percentage on carriage-return
    terminated lines; Python's universal newlines already turns those into ordinary
    lines for us."""
    proc = _popen(t, env)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n").strip()
        if not line:
            continue
        match = PERCENT_RE.search(line)
        if match:
            progress.update(task_id, completed=min(int(match.group(1)), 100),
                            description=base_desc)
        else:
            logger.raw(line)
    proc.wait()
    progress.update(task_id, completed=100, description=base_desc)
    return proc.returncode


PROGRESS_RUNNERS = {
    "json": run_json_progress,
    "lines": run_line_progress,
    "percent": run_percent_progress,
}


def run_plain(command: list[str], logger: Logger, cwd: Optional[Path] = None,
               env: Optional[dict] = None) -> int:
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, cwd=str(cwd) if cwd else None, env=env)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line:
            logger.raw(line)
    proc.wait()
    return proc.returncode


def execute_plan(t: Transfer, base_desc: str, progress: Optional[Progress],
                  interactive: bool, logger: Logger, dry_run: bool) -> int:
    if t.error:
        console.print(f"[red]\u2717[/red] {escape(base_desc)} \u2014 {escape(t.error)}")
        logger.log_file_only(f"ERROR: {base_desc}: {t.error}")
        return 1
    if t.skip:
        console.print(f"[green]\u2713[/green] {escape(base_desc)} [dim]({t.skip})[/dim]")
        return 0
    if t.note:
        console.print(f"[yellow]DRY[/yellow] {escape(base_desc)} \u2014 {escape(t.note)}")
        logger.log_file_only(f"DRY RUN {base_desc}: {t.note}")
        return 0

    env = {**os.environ, **t.env} if t.env else None
    try:
        if interactive and progress is not None and t.progress:
            task_id = progress.add_task(base_desc, total=100)
            rc = PROGRESS_RUNNERS[t.progress](t, progress, task_id, base_desc, logger, env)
            progress.remove_task(task_id)
        else:
            console.print(f"\u2192 {escape(base_desc)}")
            rc = run_plain(t.command, logger, cwd=t.cwd, env=env)

        # Post steps are the destructive half of a move, so they run only after the
        # transfer itself has exited clean, and never during a dry run.
        if rc == 0 and not dry_run:
            for command, cwd in t.post:
                prc = run_plain(command, logger, cwd=cwd, env=env)
                if prc != 0:
                    console.print(f"[red]\u2717[/red] {escape(base_desc)} \u2014 transferred, but "
                                   f"clearing the source failed [dim](exit {prc})[/dim]")
                    logger.log_file_only(f"ERROR: post-transfer cleanup failed for {base_desc} "
                                          f"(exit {prc})")
                    return prc

        if rc == 0:
            console.print(f"[green]\u2713[/green] {escape(base_desc)}")
        else:
            console.print(f"[red]\u2717[/red] {escape(base_desc)} [dim](exit {rc})[/dim]")
        return rc
    finally:
        for path in t.cleanup:
            with contextlib.suppress(OSError):
                os.unlink(path)


# ---------------------------------------------------------------------------
# fs engine (type: local)
#
# local targets removable storage, so it drives coreutils rather than rclone.
# Every path funnels through one NUL-delimited list of paths relative to the
# source \u2014 exactly the shape `git ls-files -z` already produces \u2014 so the plain,
# excluded and Git-aware cases all share a single copy implementation.
# ---------------------------------------------------------------------------

@dataclass
class MountInfo:
    source: str
    fstype: str
    avail: str
    target: str


def resolve_mount(path: Path) -> Optional[MountInfo]:
    result = subprocess.run(
        ["findmnt", "-no", "SOURCE,FSTYPE,AVAIL,TARGET", "--target", str(path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    fields = result.stdout.strip().split(None, 3)
    if len(fields) != 4:
        return None
    return MountInfo(*fields)


def verify_local_destination(destination: str) -> MountInfo:
    """An unmounted removable drive leaves its mount point behind as an ordinary
    directory on the system disk, so an unguarded backup would quietly fill that
    disk instead. Require the destination to live on a volume that is neither the
    root filesystem nor the one holding $HOME.

    Testing for "/" alone is not enough: under a btrfs subvolume layout (or any
    setup with a separate /home partition) a mistyped path below $HOME resolves to
    its own mount and would sail straight through the check.

    findmnt fails on a path that does not exist, so probe upwards to the nearest
    existing ancestor \u2014 the destination directory itself is usually created by
    the first run."""
    dest = Path(destination).expanduser()
    probe = dest
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent

    mount = resolve_mount(probe)
    if mount is None:
        raise ConfigError(f"Could not determine the mount point for local destination {dest}.")

    system = {m.target for m in (resolve_mount(Path("/")), resolve_mount(Path.home())) if m}
    if mount.target in system:
        raise ConfigError(
            f"local destination {dest} is on the system volume (mounted at {mount.target}), "
            f"not a separate backup volume.\n"
            "       If a removable drive belongs here, mount it and re-run \u2014 writing to "
            "the system disk would fill it instead."
        )
    return mount


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


def backup_git_repo(repo: Path, destination: str, run: RunConfig, common_args: list[str],
                     env: dict, list_max_files: Optional[int], progress: Optional[Progress],
                     interactive: bool, logger: Logger, dry_run: bool) -> tuple[int, bool]:
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
        console.print(f"[yellow]\u26a0[/yellow] {escape(base_desc)} \u2014 {len(entries)} files exceeds "
                       f"listmaxfiles ({list_max_files}), skipping")
        logger.log_file_only(f"SKIPPED (listmaxfiles): {repo} has {len(entries)} tracked files")
        return 0, True

    # ls-files can name a file staged for deletion but already gone from the
    # worktree; every engine would fail on it, so keep only what is on disk.
    rel_paths = [e.decode() for e in entries]
    rel_paths = [r for r in rel_paths if (repo / r).exists()]

    transfer = build_transfer(run, "file_list", repo, destination, "copy", common_args, env,
                              dry_run, rel_paths=rel_paths)
    return execute_plan(transfer, base_desc, progress, interactive, logger, dry_run), False


def backup_normal(source: Path, destination: str, mode: str, run: RunConfig,
                   common_args: list[str], env: dict, base_desc: str,
                   progress: Optional[Progress], interactive: bool, logger: Logger,
                   dry_run: bool) -> int:
    transfer = build_transfer(run, "tree", source, destination, mode, common_args, env, dry_run)
    return execute_plan(transfer, base_desc, progress, interactive, logger, dry_run)


def backup_non_git_contents(source_root: Path, destination: str, git_roots: list[Path],
                             run: RunConfig, common_args: list[str], env: dict, base_desc: str,
                             progress: Optional[Progress], interactive: bool, logger: Logger,
                             dry_run: bool) -> int:
    transfer = build_transfer(run, "tree_excluding", source_root, destination, "copy",
                              common_args, env, dry_run, excludes=git_roots)
    return execute_plan(transfer, base_desc, progress, interactive, logger, dry_run)


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

def build_archive(library: Library, remote: str, run: RunConfig, common_args: list[str],
                   env: dict, staging_dir: Path, progress: Optional[Progress],
                   interactive: bool, logger: Logger, dry_run: bool) -> int:
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
    transfer = build_transfer(run, "single_file", archive_path, destination, "copy",
                              common_args, env, dry_run)
    rc = execute_plan(transfer, base_desc, progress, interactive, logger, dry_run)

    try:
        archive_path.unlink()
    except OSError:
        pass

    return rc


# ---------------------------------------------------------------------------
# Summary banner
# ---------------------------------------------------------------------------

def print_summary(run: RunConfig, libraries: list[Library], dry_run: bool,
                   mount: "Optional[MountInfo]", config_path: Path) -> None:
    console.print()
    for line in BUZM_ART:
        console.print(Text(line, style="cyan"))
    console.print()

    total_dirs = sum(len(lib.entries) for lib in libraries)

    console.print(f"[bold]Config[/bold]        {escape(str(config_path))}")
    console.print(f"[bold]Destination[/bold]   {escape(run.destination)}")
    console.print(f"[bold]Type[/bold]          {run.type} (engine: {run.engine})")
    auth_line = run.auth_method
    if run.secret is not None:
        auth_line += f" (from {escape(run.secret.origin)})"
    elif run.auth_method == "sshkey":
        auth_line += f" ({escape(run.auth_param)})" if run.auth_param else " (ssh-agent)"
    console.print(f"[bold]Auth[/bold]          {auth_line}")
    if run.type == "ftp":
        console.print(f"[bold]TLS[/bold]           "
                       f"{'FTPS' if run.tls else '[red]disabled (cleartext)[/red]'}")
    if mount is not None:
        console.print(f"[bold]Mount[/bold]         {escape(mount.source)}  {escape(mount.fstype)}  "
                       f"({escape(mount.avail)} free)  at {escape(mount.target)}")
    console.print(f"[bold]Run[/bold]           {'[yellow]DRY RUN[/yellow]' if dry_run else '[green]LIVE[/green]'}")
    if run.archive:
        console.print("[bold]Archive[/bold]       [yellow]enabled \u2014 each library is zipped whole into a "
                       "dated .7z and uploaded[/yellow]")
    else:
        console.print("[bold]Archive[/bold]       disabled")
    if run.list_max_files is not None:
        console.print(f"[bold]Max files[/bold]     {run.list_max_files} per directory")
    console.print(f"[bold]Libraries[/bold]     {len(libraries)} ({total_dirs} total dirs)")
    for lib in libraries:
        names = ", ".join(e.dest_name for e in lib.entries) if lib.entries else "(none)"
        console.print(f"  [magenta]{escape(lib.name)}[/magenta] \u2014 {len(lib.entries)} "
                       f"dir(s): {escape(names)}")
    console.print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="buzm",
        description="buzm \u2014 Backup Utility, by rvzm. Configurable backup to remote or "
                     "removable storage.",
    )
    p.add_argument("-c", "--config", metavar="PATH",
                    help="Config file to use (default: first config.yml found in the current "
                         "directory, beside buzm.py, then ~/.buzm/)")
    p.add_argument("-n", "--dry-run", action="store_true",
                    help="Show what would be transferred without changing anything")
    p.add_argument("-v", "--verbose", action="store_true",
                    help="Show rclone verbose output")
    p.add_argument("--plain", "--cli", dest="plain", action="store_true",
                    help="Force plain line-by-line output (no live progress bars)")
    return p.parse_args()


def check_required_commands(engine: str, extra: Optional[list[str]] = None) -> None:
    needed = REQUIRED_COMMANDS + (extra if extra is not None else ENGINE_COMMANDS.get(engine, []))
    missing = [c for c in needed if shutil.which(c) is None]
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
            console.print("[bold red]Another buzm process is already running.[/bold red]")
            sys.exit(1)
        raise
    return fh


def main() -> int:
    args = parse_args()

    try:
        config_path = resolve_config_path(args.config)
        raw_run_cfg, libraries = load_config(config_path)
        run = parse_run_config(raw_run_cfg, config_path, libraries)
        # Checked before the lock and before any transfer: a local destination on
        # an unmounted drive must never be written to.
        mount = verify_local_destination(run.destination) if run.engine == "fs" else None
    except ConfigError as e:
        console.print(f"[bold red]ERROR:[/bold red] {e}")
        return 1
    except (OSError, yaml.YAMLError) as e:
        console.print(f"[bold red]ERROR:[/bold red] Could not read config: {e}")
        return 1

    needed = list(ENGINE_COMMANDS.get(run.engine, []))
    if run.type == "rsync" and run.dest and run.dest.scheme == "ssh":
        needed.append("ssh")
    check_required_commands(run.engine, needed)
    _lock_fh = acquire_lock()

    remote = engine_remote_base(run)
    list_max_files = run.list_max_files
    archive_mode = run.archive

    if archive_mode and shutil.which("7z") is None:
        console.print("[bold red]ERROR:[/bold red] run.archive is enabled but the '7z' command was not found.")
        return 1

    if not libraries:
        console.print("[yellow]WARNING:[/yellow] No libraries found in config.")
        return 0

    interactive = console.is_terminal and not args.plain

    LOGDIR.mkdir(parents=True, exist_ok=True)
    logfile = LOGDIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    logger = Logger(logfile, secrets=[run.secret.value] if run.secret else None)

    def handle_sigterm(signum, frame):
        sys.exit(143)
    signal.signal(signal.SIGTERM, handle_sigterm)

    logger.log_file_only("========================================")
    logger.log_file_only("buzm run started")
    logger.log_file_only(f"Config: {config_path}")
    logger.log_file_only(f"Destination: {run.destination} (type={run.type}, engine={run.engine})")
    if run.secret is not None:
        logger.log_file_only(f"Auth: {run.auth_method} (from {run.secret.origin})")
    if args.dry_run:
        logger.log_file_only("MODE: DRY RUN")

    print_summary(run, libraries, args.dry_run, mount, config_path)

    if run.type == "ftp" and not run.tls:
        console.print(f"[yellow]WARNING:[/yellow] type 'ftp' with tls: false sends credentials "
                      f"and file contents to {escape(str(run.dest.host))} in cleartext. Use "
                      f"sftp, or tls: true if the server supports it.")


    try:
        common_args, engine_env = build_common_args(run, args.dry_run, args.verbose, interactive)
    except ConfigError as e:
        console.print(f"[bold red]ERROR:[/bold red] {e}")
        logger.close()
        return 1

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
            staging_dir = Path(tempfile.mkdtemp(prefix="buzm-archive-"))
            try:
                for lib in libraries:
                    if not lib.entries:
                        logger.log_file_only(f"WARNING: Library '{lib.name}' has no dirs; skipping.")
                        continue

                    console.rule(f"[bold magenta]{lib.name}[/bold magenta]", style="dim")

                    rc = build_archive(lib, remote, run, common_args, engine_env, staging_dir,
                                        progress, interactive, logger, args.dry_run)
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
                                rc = backup_normal(entry.source, destination, entry.mode, run,
                                                    common_args, engine_env, base_desc, progress,
                                                    interactive, logger, args.dry_run)
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
                                    rc = backup_non_git_contents(entry.source, destination, git_roots,
                                                                  run, common_args, engine_env,
                                                                  f"{base_desc} (misc)", progress,
                                                                  interactive, logger, args.dry_run)
                                    if rc != 0:
                                        failures += 1

                            for repo in git_roots:
                                repo_destination = f"{destination}/{repo.name}"
                                rc, skipped = backup_git_repo(repo, repo_destination, run,
                                                               common_args, engine_env,
                                                               list_max_files, progress,
                                                               interactive, logger, args.dry_run)
                                if rc != 0 and not skipped:
                                    failures += 1
                    else:
                        count = count_files_excluding(entry.source, [], list_max_files)
                        if list_max_files is not None and count > list_max_files:
                            console.print(f"[yellow]\u26a0[/yellow] {base_desc} \u2014 {count} files exceeds "
                                          f"listmaxfiles ({list_max_files}), skipping")
                            logger.log_file_only(f"SKIPPED (listmaxfiles): {entry.source} has {count} files")
                        else:
                            rc = backup_normal(entry.source, destination, entry.mode, run,
                                                common_args, engine_env, base_desc, progress,
                                                interactive, logger, args.dry_run)
                            if rc != 0:
                                failures += 1

                    processed += 1

    console.print()
    console.print(f"[bold]Processed:[/bold] {processed}   [bold]Failures:[/bold] {failures}")
    console.print(f"[dim]Log:[/dim] {logfile}")

    if failures > 0:
        logger.log_file_only("buzm run finished WITH ERRORS.")
        logger.close()
        return 1
    else:
        logger.log_file_only("buzm run completed successfully.")
        logger.close()
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
# buzm

**B**ackup **U**tility, by rv**zm** — automated, configurable backups to remote or removable storage.

<!-- badges: license · python version · release -->
![buzm](https://img.shields.io/badge/buzm-0.1-green)
![Python](https://img.shields.io/badge/python-^3.10-green)
![rsync](https://img.shields.io/badge/rsync-^3.5.0-green)
![rclone](https://img.shields.io/badge/rclone-^1.75.0-green)

![python-deps](https://img.shields.io/badge/python--dep-rich_pyyaml-blue)
![system-deps](https://img.shields.io/badge/system--dep-find_cp_rm_mkdir_xargs_findmnt_7z-blue)
![system-remote](https://img.shields.io/badge/system--remote-git_rclone_rsync_ftp_sftp_ssh-blue)


![GitHub License](https://img.shields.io/github/license/rvzm/remote-backup)
![GitHub commit activity](https://img.shields.io/github/commit-activity/w/rvzm/remote-backup)
![GitHub Issues](https://img.shields.io/github/issues/rvzm/remote-backup)



---

## What it is

buzm backs up a set of directories you describe once in a YAML file. Group them into
libraries, say where the backup goes, and run one command. It drives an existing transfer
tool rather than reimplementing one, so transfers are resumable, retried, and verifiable.

| | |
|---|---|
| **Configuration-driven** | One `config.yml` describes every directory, grouped into libraries |
| **Multiple destinations** | rclone remotes today, removable drives via coreutils, rsync planned |
| **Git-aware** | Back up only what Git tracks, skipping build output and ignored files |
| **Copy or move** | Per library or per directory — move clears the source once the copy lands |
| **Archive mode** | Optionally compress each library into a dated `.7z` before sending |
| **Live progress** | One progress bar per transfer, finished items scroll into history |
| **Logged** | Every run appends to a dated log with the raw output of every command |

> _<!-- expand: what problem this solves for you, why not just cron+rsync -->_

---

## Requirements

| Requirement | Needed for |
|---|---|
| Python 3.10+ | Everything |
| [`rich`](https://github.com/Textualize/rich) | Terminal output and progress bars |
| [`pyyaml`](https://pyyaml.org/) | Reading `config.yml` |
| `git`, `find` | Discovering repositories, git-aware backups |
| `rclone` | `type: scw`, `ftp`, `sftp` |
| `rsync` | `type: rsync` |
| `ssh` | `type: rsync` with an `ssh://` destination |
| `cp`, `rm`, `mkdir`, `xargs`, `findmnt` | `type: local` (standard on any Linux system) |
| `7z` | Only when `archive: true` |

Missing commands are reported at startup, and only the ones your configured `type` needs
are checked.

---

## Install

```bash
git clone <repo-url> buzm && cd buzm
pip install rich pyyaml

cp example.config.yml config.yml
$EDITOR config.yml
```

`config.yml` is yours and is gitignored; `example.config.yml` is the committed template and
holds only placeholders. Optionally put buzm on your `PATH`:

```bash
ln -s "$PWD/buzm.py" ~/.local/bin/buzm
```

> _<!-- expand: packaging / pipx / AUR once that exists -->_

---

## Usage

```bash
buzm --dry-run        # show what would happen, change nothing
buzm                  # run it
```

| Flag | Effect |
|---|---|
| `-n`, `--dry-run` | Report every planned transfer without moving, writing, or deleting |
| `-v`, `--verbose` | Verbose output from the underlying transfer tool |
| `--plain`, `--cli` | Line-by-line output instead of live progress bars — use in cron and scripts |
| `-c PATH`, `--config PATH` | Use a specific config file |
| `-h`, `--help` | Usage |

Only one buzm may run at a time; a second invocation exits immediately rather than
competing with the first.

> _<!-- expand: cron / systemd timer examples -->_

---

## Configuration

`config.yml` has two sections: `run:` for global settings and `libraries:` for what to back
up. buzm searches for it in this order, first hit wins:

| Order | Location |
|---|---|
| 1 | `--config PATH` |
| 2 | `$BUZM_CONFIG` |
| 3 | `./config.yml` |
| 4 | `config.yml` beside `buzm.py` |
| 5 | `~/.buzm/config.yml` |

### `run:`

| Key | Values | Default | Meaning |
|---|---|---|---|
| `destination` | string | — | Where backups go. Form depends on `type`. Overridable with `$BUZM_REMOTE` |
| `type` | `scw` `local` `ftp` `sftp` `rsync` | required | What the destination is; also decides the engine |
| `auth` | `password` `sshkey` `public` | `public` | How to authenticate — see [Authentication](#authentication) |
| `tls` | `true` `false` | `true` | FTP only. `false` sends everything in cleartext |
| `archive` | `true` `false` | `false` | Compress each library into a dated `.7z` and send that instead |
| `listmaxfiles` | integer | none | Skip and log any directory holding more files than this |

### `libraries:`

Each key under `libraries:` is a library name and becomes a folder at the destination.

| Key | Values | Default | Meaning |
|---|---|---|---|
| `mode` | `copy` `move` | `copy` | `move` clears the source once the copy succeeds |
| `gitignore` | `true` `false` | `false` | Back up only files Git tracks |
| `dirs` | list | — | The directories in this library |

Each `dirs` entry takes a `path`, plus optional `mode` and `gitignore` that override the
library defaults for that one directory.

```yaml
libraries:
  dev:
    mode: copy
    gitignore: true
    dirs:
      - path: /home/user/Projects/
      - path: /home/user/devbak/
        mode: move
        gitignore: false      # per-directory overrides
```

### Renaming at the destination

A `:CustomName` suffix on a path renames the folder at the destination. Without one it
keeps the source directory's own name.

```yaml
- path: /home/user/Videos/OBS/:OBS     # lands at <destination>/videos/OBS/
- path: /home/user/Pictures/           # lands at <destination>/photos/Pictures/
```

Files land at `<destination>/<library>/<dir name>/`.

---

## Destinations

The engine follows from `type` — there is no separate setting for it.

| `type` | Engine | Destination looks like |
|---|---|---|
| `scw` | rclone | `scw:data/laptop` |
| `local` | fs (coreutils) | `/run/media/user/BACKUP` |
| `sftp` | rclone | `sftp://user@host:22/srv/backup` |
| `ftp` | rclone | `ftp://user@host/backup` (or `ftps://`) |
| `rsync` | rsync | `rsync://host/module/path`, or `ssh://user@host/path` |

`scw` and `local` take a bare remote or path. The network types take a URL whose scheme must
match the `type` — a mismatch is a config error, not a silent reinterpretation. For `rsync`
the scheme picks the transport: `rsync://` is an rsync daemon, `ssh://` is rsync over ssh.

Do not put a password in the destination URL; buzm refuses it. Use `auth:` instead, which
keeps it out of process arguments and logs.

## Authentication

`auth:` names a method, optionally followed by a colon and a parameter.

| `auth:` | Where the credential comes from |
|---|---|
| `public` | Nothing needed (default) |
| `sshkey` | The ssh-agent |
| `sshkey:~/.ssh/id_ed25519` | That private key |
| `password` | `$BUZM_PASSWORD` |
| `password:@~/.buzm/ftp.pass` | First line of that file |
| `password:hunter2` | The literal, straight from the config |

Passwords are handed to `rclone` and `rsync` through the environment, never as command-line
arguments — `/proc/*/cmdline` is readable by every user on the machine. They are stripped
from anything buzm writes to its log, and the run summary shows only where a credential came
from, never its value.

Two shapes get a warning rather than a refusal:

- A **literal password in the config**, or a password file others can read (`chmod 600` it).
- A secret stored **inside a directory buzm backs up** — it would be copied to the remote in
  cleartext along with everything else. An env var, or a file outside the backup set, avoids
  this.

`type: rsync` over `ssh://` cannot use a password: that would need `sshpass` and would put
the password on a command line. Use a key or the agent.

### Host keys

For `sftp`, the server must already be in `~/.ssh/known_hosts` — an unknown host key is
refused, not trusted on first sight. Add one with
`ssh-keyscan -p PORT host >> ~/.ssh/known_hosts`, or point `$BUZM_KNOWN_HOSTS` at a
different file. `rsync` over `ssh://` uses ssh's own checking, in batch mode so it fails
rather than waiting at a prompt.

---

### `local` and removable drives

`local` uses `cp` and `mv` directly rather than rclone, with copy-on-write reflinks where
the filesystem supports them and unchanged files skipped, so repeat backups to the same
drive are cheap.

Because an unmounted drive leaves its mount point behind as an ordinary empty directory,
buzm refuses to run when the destination sits on the system volume — otherwise a backup
started with the drive unplugged would quietly fill the system disk. Mount the drive first.

> _<!-- expand: your own drive setup, labels, udev automount -->_

---

## How it works

### Git-aware backups (`gitignore: true`)

buzm finds every Git repository under the path and backs up only the files Git tracks or
would track — `git ls-files --cached --others --exclude-standard`. Ignored content such as
`node_modules/`, build output, and virtualenvs is skipped, as is `.git/` itself. Anything
in the directory that is *not* inside a repository is backed up normally.

`gitignore: true` cannot be combined with `mode: move`.

### Archive mode (`archive: true`)

Compresses each library whole into a dated `<library>-<date>.7z` and sends that single
file. This is a global switch: when on, `mode`, `gitignore`, and `listmaxfiles` do not
apply, and everything on disk is archived as-is. Compression shows no progress bar; the
transfer does.

### `listmaxfiles`

A guard against sweeping up a directory you did not mean to. Any directory holding more
files than the cap is skipped and logged rather than transferred. Omit it for no cap.

---

## Files and locations

| What | Where |
|---|---|
| Config | `./config.yml`, or `~/.buzm/config.yml` — see the search order above |
| Logs | `~/.buzm/logs/<YYYY-MM-DD>.log` |
| Lock file | `/tmp/buzm.lock` |
| `$BUZM_CONFIG` | Overrides the config search entirely |
| `$BUZM_REMOTE` | Overrides `run.destination` |
| `$BUZM_PASSWORD` | Password for `auth: password` |
| `$BUZM_KNOWN_HOSTS` | known_hosts file for `sftp` (default `~/.ssh/known_hosts`) |

Logs hold every line the underlying tools emitted, plus buzm's own status lines. They are
appended per day and never rotated automatically.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Rebrand to buzm, `type:`-driven config, `local` engine | Done |
| 2 | `rsync`, `ftp` and `sftp` engines, credential handling for `auth:` | Done |
| 3 | Packaging (`pip install buzm`), scheduling helpers | Next |

> _<!-- expand: anything else you want on the list -->_

---

## History

buzm began as a personal Bash script for pushing directories to a locally-mounted SCW bulk
store, then was rewritten in Python. The original [`legacy\scw-backup.py`](legacy\scw-backup.py) as a
milestone. SCW is now just one `type:` among several.

---

## License

buzm is licensed under the [GNU General Public License v3.0](LICENSE).

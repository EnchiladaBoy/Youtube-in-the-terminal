#!/usr/bin/env python3
"""Install yt-ascii from a stable Git tag, main, or a local checkout.

This file is intentionally standard-library-only so it can be streamed into a
stock Python interpreter. Application dependencies live in a private virtual
environment; the installer never invokes sudo or modifies global site-packages.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile


MIN_PYTHON = (3, 10)
REPOSITORY = "EnchiladaBoy/Youtube-in-the-terminal"
RAW_MAIN = f"https://raw.githubusercontent.com/{REPOSITORY}/main"
ARCHIVE_ROOT = f"https://github.com/{REPOSITORY}/archive"
STABLE_FILE = "STABLE_VERSION"
EDGE_BUILD_FILE = "EDGE_BUILD"
MANAGED_MARKER = "yt-ascii managed installer"
ROOT_MARKER = ".yt-ascii-managed"
ROOT_MARKER_CONTENT = "yt-ascii managed installer v1\n"
CURRENT_FILE = "current"
STATE_FILE = "state.json"
PATH_START = "# >>> yt-ascii installer >>>"
PATH_END = "# <<< yt-ascii installer <<<"
LOCK_SUFFIX = ".install.lock"
TAG_RE = re.compile(
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
EDGE_BUILD_RE = re.compile(r"[1-9][0-9]*\Z")
CHANNELS = frozenset({"stable", "edge", "pinned", "source"})
MIN_INSTALLABLE_TAG = (0, 3, 0)
MAX_DOWNLOAD = 50 * 1024 * 1024
MAX_EXTRACTED = 100 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5000
WINDOWS_RESERVED_CHARS = frozenset('<>:"/\\|?*') | frozenset(
    chr(codepoint) for codepoint in range(32)
)
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
    *(f"LPT{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
}


class InstallerError(RuntimeError):
    """An expected installation failure with a user-facing message."""


def log(message):
    print(f"yt-ascii installer: {message}", flush=True)


def warn(message):
    print(f"yt-ascii installer: warning: {message}", file=sys.stderr, flush=True)


def validate_tag(value):
    if not TAG_RE.fullmatch(value or ""):
        raise InstallerError("version must be an exact stable tag such as v0.3.0")
    version = tuple(int(part) for part in value[1:].split("."))
    if version < MIN_INSTALLABLE_TAG:
        raise InstallerError("versions before v0.3.0 do not support the source installer")
    return value


def parse_stable_tag(raw, label=STABLE_FILE):
    try:
        if isinstance(raw, bytes):
            text = raw.decode("ascii")
        elif isinstance(raw, str):
            text = raw
            text.encode("ascii")
        else:
            raise TypeError
    except (TypeError, UnicodeError) as exc:
        raise InstallerError(f"{label} must contain one ASCII version tag") from exc
    if len(text.encode("ascii")) > 128:
        raise InstallerError(f"{label} exceeded the 128-byte safety limit")
    if text.endswith("\r\n"):
        value = text[:-2]
    elif text.endswith("\n"):
        value = text[:-1]
    else:
        value = text
    if "\r" in value or "\n" in value:
        raise InstallerError(f"{label} must contain exactly one version tag")
    return validate_tag(value)


def tag_version(value):
    validate_tag(value)
    return tuple(int(part) for part in value[1:].split("."))


def parse_edge_build(raw, label=EDGE_BUILD_FILE):
    try:
        if isinstance(raw, bytes):
            text = raw.decode("ascii")
        elif isinstance(raw, str):
            text = raw
            text.encode("ascii")
        else:
            raise TypeError
    except (TypeError, UnicodeError) as exc:
        raise InstallerError(f"{label} must be an ASCII positive integer") from exc
    if len(text.encode("ascii")) > 128:
        raise InstallerError(f"{label} exceeded the 128-byte safety limit")
    if text.endswith("\r\n"):
        value = text[:-2]
    elif text.endswith("\n"):
        value = text[:-1]
    else:
        value = text
    if "\r" in value or "\n" in value or not EDGE_BUILD_RE.fullmatch(value):
        raise InstallerError(f"{label} must contain exactly one positive integer")
    return int(value)


def validate_path(path, label):
    text = str(path)
    if not text or "\x00" in text or "\n" in text or "\r" in text:
        raise InstallerError(f"{label} contains unsupported characters")
    return path


def path_lexists(path):
    return os.path.lexists(str(path))


def valid_root_marker(path):
    if not path_lexists(path) or path.is_symlink():
        return False
    try:
        return path.is_file() and path.read_text(encoding="ascii") == ROOT_MARKER_CONTENT
    except (OSError, UnicodeError):
        return False


def create_root_marker(path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise InstallerError(f"refusing an unrecognized installer marker: {path}") from exc
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as marker:
        marker.write(ROOT_MARKER_CONTENT)


@contextmanager
def installer_lock(root):
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = root.with_name(f".{root.name}{LOCK_SUFFIX}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock, flags, 0o600)
    except FileExistsError as exc:
        raise InstallerError(
            f"another install may be running; if not, remove the stale lock {lock}"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(f"pid={os.getpid()}\n")
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def default_locations():
    home = Path.home()
    root_override = os.environ.get("YTASCII_HOME")
    bin_override = os.environ.get("YTASCII_BIN_DIR")
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        root = Path(root_override) if root_override else local / "Programs" / "yt-ascii"
        bin_dir = Path(bin_override) if bin_override else root / "bin"
    else:
        data_home = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        root = Path(root_override) if root_override else data_home / "yt-ascii"
        bin_dir = Path(bin_override) if bin_override else home / ".local" / "bin"
    return root.expanduser(), bin_dir.expanduser()


def selected_locations(args):
    default_root, default_bin = default_locations()
    root = (args.install_root or default_root).expanduser().resolve()
    if args.bin_dir is not None:
        bin_dir = args.bin_dir.expanduser().resolve()
    elif os.name == "nt" and args.install_root is not None:
        bin_dir = (root / "bin").resolve()
    else:
        bin_dir = default_bin.expanduser().resolve()
    return validate_path(root, "install root"), validate_path(bin_dir, "bin directory")


def fetch_bytes(url, limit=MAX_DOWNLOAD):
    request = urllib.request.Request(url, headers={"User-Agent": "yt-ascii-installer/0.5"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(limit + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise InstallerError(f"download failed for {url}: {exc}") from exc
    if len(data) > limit:
        raise InstallerError(f"download from {url} exceeded {limit // (1024 * 1024)} MiB")
    return data


def local_stable_file():
    script = globals().get("__file__")
    if not script or script == "<stdin>":
        return None
    candidate = Path(script).resolve().with_name(STABLE_FILE)
    return candidate if candidate.is_file() else None


def stable_tag(remote=False):
    local = None if remote else local_stable_file()
    try:
        if local is not None:
            with local.open("rb") as stream:
                raw = stream.read(129)
        else:
            raw = fetch_bytes(f"{RAW_MAIN}/{STABLE_FILE}", limit=128)
    except OSError as exc:
        raise InstallerError(f"could not read a valid {STABLE_FILE}: {exc}") from exc
    return parse_stable_tag(raw)


def edge_build():
    raw = fetch_bytes(f"{RAW_MAIN}/{EDGE_BUILD_FILE}", limit=128)
    return parse_edge_build(raw)


def read_staged_edge_build(app_dir, expected=None):
    try:
        with (app_dir / EDGE_BUILD_FILE).open("rb") as stream:
            raw = stream.read(129)
    except OSError as exc:
        raise InstallerError(f"could not read staged {EDGE_BUILD_FILE}: {exc}") from exc
    build = parse_edge_build(raw, f"staged {EDGE_BUILD_FILE}")
    if expected is not None and build < expected:
        raise InstallerError(
            "downloaded edge archive is older than the update check "
            f"({build} < {expected}); retry the update"
        )
    return build


def archive_url(ref, edge=False):
    if edge:
        return f"{ARCHIVE_ROOT}/refs/heads/main.zip"
    return f"{ARCHIVE_ROOT}/refs/tags/{validate_tag(ref)}.zip"


def is_zip_symlink(info):
    return stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK


def is_windows_reserved_name(name):
    """Match Windows reserved-name rules without requiring Python 3.13+."""
    if not name or name[-1:] in {".", " "}:
        return True
    if WINDOWS_RESERVED_CHARS.intersection(name):
        return True
    device = name.partition(".")[0].rstrip(" ").upper()
    return device in WINDOWS_RESERVED_NAMES


def safe_extract_zip(payload, destination):
    """Extract a GitHub ZIP without trusting member paths or symlinks."""
    archive_path = destination / "source.zip"
    archive_path.write_bytes(payload)
    extracted = destination / "extracted"
    extracted.mkdir()
    total = 0
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallerError("downloaded source archive is not a valid ZIP") from exc
    normalized_names = set()
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_ENTRIES:
            raise InstallerError("source archive has an invalid number of entries")
        for info in infos:
            # ZipInfo normalizes os.sep in ``filename``.  On Windows that can
            # hide a backslash from the archive's original member name, so
            # validate the unmodified spelling and only use the normalized
            # value after it has passed every path check.
            name = info.orig_filename
            path = PurePosixPath(name)
            windows_path = PureWindowsPath(name)
            parts = path.parts
            normalized = "/".join(part.casefold() for part in parts)
            if (
                not name
                or "\x00" in name
                or path.is_absolute()
                or windows_path.is_absolute()
                or bool(windows_path.drive)
                or ".." in path.parts
                or "\\" in name
                or is_zip_symlink(info)
                or bool(info.flag_bits & 0x1)
                or normalized in normalized_names
                or any(is_windows_reserved_name(part) for part in parts)
            ):
                raise InstallerError(f"unsafe path in source archive: {name!r}")
            normalized_names.add(normalized)
            total += info.file_size
            if total > MAX_EXTRACTED:
                raise InstallerError("source archive expands beyond the safety limit")
            target = extracted.joinpath(*path.parts)
            try:
                target.resolve().relative_to(extracted.resolve())
            except ValueError as exc:
                raise InstallerError(f"path escapes the source archive: {name!r}") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise InstallerError(f"could not safely extract {name!r}: {exc}") from exc
    archive_path.unlink(missing_ok=True)
    roots = [entry for entry in extracted.iterdir()]
    if len(roots) != 1 or not roots[0].is_dir():
        raise InstallerError("source archive must contain one top-level directory")
    return roots[0]


def source_ignore(_directory, names):
    ignored = set()
    for name in names:
        if name in {".git", ".venv", "venv", "build", "dist", "__pycache__"}:
            ignored.add(name)
        elif name.endswith((".pyc", ".spec")):
            ignored.add(name)
    return ignored


def stage_source(
    version_dir, source_dir=None, ref=None, edge=False,
    expected_edge_build=None,
):
    app_dir = version_dir / "app"
    if source_dir is not None:
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise InstallerError(f"source directory does not exist: {source}")
        shutil.copytree(source, app_dir, ignore=source_ignore)
    else:
        payload = fetch_bytes(archive_url(ref, edge=edge))
        scratch = version_dir / ".download"
        scratch.mkdir()
        source = safe_extract_zip(payload, scratch)
        shutil.copytree(source, app_dir, ignore=source_ignore)
        shutil.rmtree(scratch)
    required = [
        app_dir / "yt-ascii",
        app_dir / "yt_ascii_renderer.py",
        app_dir / "requirements.txt",
    ]
    # Historical tags remain installable with the source layout they shipped:
    # v0.4 adds styles; v0.5 adds structured frames, backend capabilities,
    # graphical effects, diagnostics, and managed updates.
    # Local source, edge, and unknown refs are always held to the latest layout.
    tag_version = (
        tuple(int(part) for part in ref[1:].split("."))
        if TAG_RE.fullmatch(ref or "")
        else None
    )
    requires_style_module = (
        source_dir is not None
        or edge
        or tag_version is None
        or tag_version >= (0, 4, 0)
    )
    if requires_style_module:
        required.append(app_dir / "yt_ascii_styles.py")
    requires_effect_modules = (
        source_dir is not None
        or edge
        or tag_version is None
        or tag_version >= (0, 5, 0)
    )
    if requires_effect_modules:
        required.extend([
            app_dir / "yt_ascii_backends.py",
            app_dir / "yt_ascii_effects.py",
            app_dir / "yt_ascii_frames.py",
            app_dir / "yt_ascii_diagnostics.py",
        ])
    requires_update_assets = (
        source_dir is not None
        or edge
        or tag_version is None
        or tag_version >= (0, 5, 0)
    )
    if requires_update_assets:
        required.extend([
            app_dir / "install.py",
            app_dir / "yt_ascii_update.py",
            app_dir / EDGE_BUILD_FILE,
        ])
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise InstallerError("source is missing required files: " + ", ".join(missing))
    if edge:
        read_staged_edge_build(app_dir, expected_edge_build)
    return app_dir


def environment_python(venv_dir):
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def run_checked(command, message, env=None):
    try:
        subprocess.run([str(part) for part in command], check=True, env=env)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InstallerError(message) from exc


def build_environment(version_dir, app_dir, ref):
    venv_dir = version_dir / "venv"
    log("creating a private Python environment")
    run_checked(
        [sys.executable, "-m", "venv", venv_dir],
        "could not create a virtual environment; install your platform's Python venv support",
    )
    python = environment_python(venv_dir)
    env = os.environ.copy()
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONUTF8"] = "1"
    log("installing application dependencies")
    run_checked(
        [python, "-m", "pip", "install", "-r", app_dir / "requirements.txt"],
        "dependency installation failed; the previous installation was left unchanged",
        env=env,
    )
    env["YTASCII_INSTALL_REF"] = ref
    log("running the installed self-test")
    run_checked(
        [python, app_dir / "yt-ascii", "--self-test"],
        "the installed self-test failed; the previous installation was left unchanged",
        env=env,
    )
    return python


def managed_launcher(path, expected_content=None):
    if not path_lexists(path) or path.is_symlink() or not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
        lines = content.splitlines()[:3]
        has_marker = (
            f"# {MANAGED_MARKER}" in lines
            or f"rem {MANAGED_MARKER}" in lines
        )
        return has_marker and (
            expected_content is None or content == expected_content
        )
    except (OSError, UnicodeError):
        return False


def launcher_collisions(bin_dir, expected_content):
    if os.name == "nt":
        suffixes = ["", ".com", ".exe", ".bat", ".cmd"]
        suffixes.extend(
            suffix for suffix in os.environ.get("PATHEXT", "").split(";")
            if suffix
        )
        candidates = []
        seen = set()
        for suffix in suffixes:
            candidate = bin_dir / f"yt-ascii{suffix.lower()}"
            key = str(candidate).casefold()
            if key not in seen:
                candidates.append(candidate)
                seen.add(key)
    else:
        candidates = [bin_dir / "yt-ascii"]
    collisions = []
    for candidate in candidates:
        if not path_lexists(candidate):
            continue
        if (
            candidate == launcher_path(bin_dir)
            and managed_launcher(candidate, expected_content)
        ):
            continue
        collisions.append(candidate)
    return collisions


def posix_launcher(root):
    # Update protocol v1: these launcher bytes are intentionally immutable.
    # An older bundled installer stages a newer app without rewriting its
    # launcher, so a future protocol must accept and migrate this exact form.
    quoted_root = shlex.quote(str(root))
    return f"""#!/bin/sh
# {MANAGED_MARKER}
YTASCII_ROOT={quoted_root}
if ! {{
    IFS= read -r YTASCII_CURRENT
    IFS= read -r YTASCII_INSTALL_REF
}} < "$YTASCII_ROOT/{CURRENT_FILE}"; then
    echo "yt-ascii: installation state is missing; rerun the installer" >&2
    exit 1
fi
export YTASCII_INSTALL_REF
exec "$YTASCII_CURRENT/venv/bin/python" "$YTASCII_CURRENT/app/yt-ascii" "$@"
"""


def windows_launcher(root, bin_dir=None):
    default_bin = ntpath.normcase(ntpath.normpath(str(root / "bin")))
    selected_bin = (
        ntpath.normcase(ntpath.normpath(str(bin_dir)))
        if bin_dir is not None else None
    )
    if selected_bin == default_bin:
        state_setup = (
            'for %%I in ("%~dp0..") do set "YTASCII_ROOT=%%~fI"\n'
            f'set "YTASCII_STATE=%YTASCII_ROOT%\\{CURRENT_FILE}"'
        )
    else:
        current_literal = str(root / CURRENT_FILE).replace("%", "%%")
        state_setup = f'set "YTASCII_STATE={current_literal}"'
    return rf"""@echo off
rem {MANAGED_MARKER}
setlocal EnableExtensions DisableDelayedExpansion
set "YTASCII_CURRENT="
set "YTASCII_INSTALL_REF="
{state_setup}
for /f "usebackq delims=" %%R in ("%YTASCII_STATE%") do (
  if not defined YTASCII_CURRENT (
    set "YTASCII_CURRENT=%%R"
  ) else if not defined YTASCII_INSTALL_REF (
    set "YTASCII_INSTALL_REF=%%R"
  )
)
if not defined YTASCII_CURRENT (
  echo yt-ascii: installation state is missing; rerun the installer 1>&2
  exit /b 1
)
"%YTASCII_CURRENT%\venv\Scripts\python.exe" "%YTASCII_CURRENT%\app\yt-ascii" %*
"""


def launcher_path(bin_dir):
    return bin_dir / ("yt-ascii.cmd" if os.name == "nt" else "yt-ascii")


def write_atomic(path, content, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write(content)
        effective_mode = mode
        if effective_mode is None and os.name != "nt" and path.exists():
            effective_mode = stat.S_IMODE(path.stat().st_mode)
        if effective_mode is not None:
            temp.chmod(effective_mode)
        os.replace(temp, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temp.unlink(missing_ok=True)


def read_current(root):
    path = root / CURRENT_FILE
    if not path.is_file():
        return None, None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None, None
    if len(lines) < 2:
        return None, None
    current = Path(lines[0])
    ref = lines[1]
    return current, ref


def write_state(
    root, current, previous, ref, launcher, bin_dir, path_files,
    windows_path_added, channel, installed_edge_build,
):
    state = {
        "schema": 1,
        "root": str(root),
        "current": str(current),
        "previous": str(previous) if previous is not None else None,
        "ref": ref,
        "channel": channel,
        "edge_build": installed_edge_build,
        "launcher": str(launcher),
        "bin_dir": str(bin_dir),
        "path_files": [str(path) for path in path_files],
        "windows_path_added": bool(windows_path_added),
    }
    write_atomic(root / STATE_FILE, json.dumps(state, indent=2, sort_keys=True) + "\n")


def read_state(root):
    path = root / STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) and data.get("schema") == 1 else {}


def read_update_state(root):
    """Read bounded, duplicate-free state for a mutating update."""
    path = root / STATE_FILE

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InstallerError(
                    f"installer state contains duplicate key {key!r}"
                )
            result[key] = value
        return result

    try:
        with path.open("rb") as stream:
            payload = stream.read(64 * 1024 + 1)
        if len(payload) > 64 * 1024:
            raise InstallerError("installer state exceeds the safety limit")
        data = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object)
    except InstallerError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise InstallerError(f"could not read valid installer state: {path}") from exc
    if not isinstance(data, dict) or type(data.get("schema")) is not int:
        raise InstallerError(f"could not read valid installer state: {path}")
    if data["schema"] != 1:
        raise InstallerError("unsupported installer state schema")
    return data


def replace_marked_block(text, block, markers=None):
    if markers is None:
        lines = block.splitlines()
        markers = (lines[0], lines[-1]) if len(lines) >= 2 else (PATH_START, PATH_END)
    start_marker, end_marker = markers
    start = text.find(start_marker)
    end = text.find(end_marker, start + len(start_marker)) if start >= 0 else -1
    if start >= 0 and end >= 0:
        end += len(end_marker)
        while end < len(text) and text[end] in "\r\n":
            end += 1
        prefix = text[:start].rstrip("\r\n")
        suffix = text[end:].lstrip("\r\n")
        parts = [part for part in (prefix, block, suffix) if part]
        return "\n\n".join(parts) + ("\n" if parts else "")
    if not block:
        return text
    prefix = text.rstrip("\r\n")
    return (prefix + "\n\n" if prefix else "") + block + "\n"


def profile_for_shell():
    home = Path.home()
    shell = Path(os.environ.get("SHELL", "")).name.lower()
    if shell == "zsh":
        return home / ".zshrc", "posix"
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish", "fish"
    if shell == "bash":
        return home / ".bashrc", "posix"
    return home / ".profile", "posix"


def known_profile_paths():
    home = Path.home()
    return {
        home / ".bashrc",
        home / ".zshrc",
        home / ".profile",
        home / ".config" / "fish" / "config.fish",
    }


def posix_path_markers(bin_dir):
    identity = os.path.normcase(os.path.abspath(str(bin_dir)))
    digest = hashlib.sha256(os.fsencode(identity)).hexdigest()[:16]
    return (
        f"# >>> yt-ascii installer {digest} >>>",
        f"# <<< yt-ascii installer {digest} <<<",
    )


def posix_path_block(bin_dir, style):
    quoted = shlex.quote(str(bin_dir))
    if style == "fish":
        body = f"fish_add_path {quoted}"
    else:
        body = f"export PATH={quoted}:\"$PATH\""
    start, end = posix_path_markers(bin_dir)
    return f"{start}\n{body}\n{end}"


def add_posix_path(bin_dir):
    entries = [Path(item).expanduser() for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    if any(os.path.normcase(str(item)) == os.path.normcase(str(bin_dir)) for item in entries):
        return []
    profile, style = profile_for_shell()
    profile.parent.mkdir(parents=True, exist_ok=True)
    if profile.is_symlink():
        raise InstallerError(
            f"refusing to replace symlinked shell profile {profile}; add {bin_dir} to PATH manually"
        )
    try:
        existing = profile.read_text(encoding="utf-8") if profile.is_file() else ""
    except (OSError, UnicodeError) as exc:
        raise InstallerError(
            f"could not safely update shell profile {profile}: {exc}"
        ) from exc
    updated = replace_marked_block(existing, posix_path_block(bin_dir, style))
    if updated != existing:
        write_atomic(profile, updated)
        return [profile]
    return []


def remove_posix_path(files, bin_dir):
    known = known_profile_paths()
    recorded = {Path(path) for path in files if Path(path) in known}
    candidates = set(recorded)
    candidates.update(known)
    markers = posix_path_markers(bin_dir)
    succeeded = True
    for path in candidates:
        if path.is_symlink():
            warn(f"preserving symlinked shell profile: {path}")
            if path in recorded:
                succeeded = False
            continue
        if not path.is_file():
            continue
        marker_was_present = False
        try:
            existing = path.read_text(encoding="utf-8")
            marker_was_present = markers[0] in existing
            updated = replace_marked_block(existing, "", markers)
            if updated != existing:
                write_atomic(path, updated)
        except (OSError, UnicodeError) as exc:
            warn(f"could not remove PATH marker from {path}: {exc}")
            if path in recorded or marker_was_present:
                succeeded = False
    return succeeded


def windows_user_path():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            try:
                value, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                value = ""
                kind = winreg.REG_EXPAND_SZ
        return value, kind
    except OSError as exc:
        raise InstallerError(f"could not read the Windows user PATH: {exc}") from exc


def set_windows_user_path(value, kind=None):
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(
                key, "Path", 0,
                winreg.REG_EXPAND_SZ if kind is None else kind,
                value,
            )
        try:
            import ctypes
            result = ctypes.c_ulong()
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000,
                ctypes.byref(result),
            )
        except (AttributeError, OSError):
            pass
    except OSError as exc:
        raise InstallerError(f"could not update the Windows user PATH: {exc}") from exc


def normalized_windows_path(value):
    return ntpath.normcase(ntpath.normpath(os.path.expandvars(value.strip().strip('"'))))


def add_windows_path(bin_dir):
    current, kind = windows_user_path()
    entries = [entry for entry in current.split(";") if entry.strip()]
    wanted = normalized_windows_path(str(bin_dir))
    if any(normalized_windows_path(entry) == wanted for entry in entries):
        return False
    entries.append(str(bin_dir))
    set_windows_user_path(";".join(entries), kind)
    return True


def remove_windows_path(bin_dir):
    try:
        current, kind = windows_user_path()
        wanted = normalized_windows_path(str(bin_dir))
        entries = [
            entry for entry in current.split(";")
            if entry.strip() and normalized_windows_path(entry) != wanted
        ]
        updated = ";".join(entries)
        if updated != current:
            set_windows_user_path(updated, kind)
    except InstallerError as exc:
        warn(str(exc))
        return False
    return True


def configure_path(bin_dir, disabled):
    if disabled:
        return [], False
    if os.name == "nt":
        return [], add_windows_path(bin_dir)
    return add_posix_path(bin_dir), False


def cleanup_versions(versions_dir, keep):
    keep_resolved = {path.resolve() for path in keep if path is not None}
    versions_resolved = versions_dir.resolve()
    for entry in versions_dir.iterdir():
        try:
            resolved = entry.resolve()
            if resolved in keep_resolved:
                continue
            if resolved.parent != versions_resolved or entry.is_symlink() or not entry.is_dir():
                warn(f"preserving unexpected versions entry: {entry}")
                continue
            shutil.rmtree(entry)
        except OSError as exc:
            warn(f"could not remove superseded version {entry}: {exc}")


def infer_channel(state, ref):
    """Read additive channel metadata while safely classifying legacy state."""
    if "channel" not in state:
        if ref == "edge":
            return "edge"
        if ref == "source":
            return "source"
        return "pinned"
    channel = state["channel"]
    if not isinstance(channel, str) or channel not in CHANNELS:
        raise InstallerError("installer state contains an invalid update channel")
    return channel


def validate_update_state(root, bin_dir):
    """Return trusted managed-install metadata for an in-place update."""
    marker = root / ROOT_MARKER
    if not valid_root_marker(marker):
        raise InstallerError(
            f"no valid managed installation found at {root}; rerun the installer"
        )
    versions_dir = root / "versions"
    if (
        not path_lexists(versions_dir)
        or versions_dir.is_symlink()
        or not versions_dir.is_dir()
    ):
        raise InstallerError(f"refusing an unexpected versions directory: {versions_dir}")
    current_path = root / CURRENT_FILE
    if (
        not path_lexists(current_path)
        or current_path.is_symlink()
        or not current_path.is_file()
    ):
        raise InstallerError(f"could not read a valid current pointer: {current_path}")
    try:
        with current_path.open("rb") as stream:
            current_payload = stream.read(4097)
        current_text = current_payload.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise InstallerError(f"could not read a valid current pointer: {exc}") from exc
    lines = current_text.splitlines()
    if (
        len(current_payload) > 4096
        or len(lines) != 2
        or current_text != f"{lines[0]}\n{lines[1]}\n"
        or not lines[1]
    ):
        raise InstallerError(f"could not read a valid current pointer: {current_path}")
    current, ref = Path(lines[0]), lines[1]
    if not current.is_absolute():
        raise InstallerError("the active generation path is not absolute")
    state_path = root / STATE_FILE
    if (
        not path_lexists(state_path)
        or state_path.is_symlink()
        or not state_path.is_file()
    ):
        raise InstallerError(f"could not read valid installer state: {state_path}")
    state = read_update_state(root)
    if state.get("root") != str(root):
        raise InstallerError("the existing installation has no valid ownership metadata")
    if state.get("current") != str(current) or state.get("ref") != ref:
        raise InstallerError("installer state does not match the active generation")
    try:
        current_resolved = current.resolve(strict=True)
        versions_resolved = versions_dir.resolve(strict=True)
    except OSError as exc:
        raise InstallerError(f"the active generation is missing or unreadable: {exc}") from exc
    if (
        current.is_symlink()
        or not current.is_dir()
        or current != current_resolved
        or current.parent != versions_resolved
        or current_resolved.parent != versions_resolved
    ):
        raise InstallerError("the active generation is outside the managed versions directory")
    if state.get("bin_dir") != str(bin_dir):
        raise InstallerError(
            "the selected bin directory does not match this installation; "
            "pass its recorded --bin-dir"
        )
    launcher = launcher_path(bin_dir)
    expected_content = (
        windows_launcher(root, bin_dir)
        if os.name == "nt" else posix_launcher(root)
    )
    if state.get("launcher") != str(launcher) or not managed_launcher(
        launcher, expected_content
    ):
        raise InstallerError("the managed launcher is missing, changed, or belongs to another root")
    channel = infer_channel(state, ref)
    explicit_channel = "channel" in state
    if explicit_channel and "edge_build" not in state:
        raise InstallerError("installer state is missing edge build metadata")
    channel_matches_ref = (
        (channel in {"stable", "pinned"} and TAG_RE.fullmatch(ref or ""))
        or (channel == "edge" and ref == "edge")
        or (channel == "source" and ref == "source")
    )
    if not channel_matches_ref:
        raise InstallerError("installer state channel does not match its installed ref")
    recorded_build = state.get("edge_build")
    if channel == "edge" and explicit_channel:
        if type(recorded_build) is not int or recorded_build <= 0:
            raise InstallerError("installer state contains an invalid edge build")
    elif channel != "edge" and recorded_build is not None:
        raise InstallerError("non-edge installer state contains edge build metadata")
    return state, current, ref, channel


def installed_edge_build(state, current):
    value = state.get("edge_build")
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InstallerError("installer state contains an invalid edge build")
        return value
    marker = current / "app" / EDGE_BUILD_FILE
    if not path_lexists(marker):
        # Pre-feature edge installs have no build marker. They are refreshed
        # once, then gain authoritative metadata from the staged archive.
        return 0
    if marker.is_symlink() or not marker.is_file():
        raise InstallerError(f"installed {EDGE_BUILD_FILE} is unsafe")
    try:
        with marker.open("rb") as stream:
            raw = stream.read(129)
        return parse_edge_build(raw, f"installed {EDGE_BUILD_FILE}")
    except OSError as exc:
        raise InstallerError(f"could not read installed {EDGE_BUILD_FILE}: {exc}") from exc


def resolve_update(root, bin_dir):
    state, current, ref, channel = validate_update_state(root, bin_dir)
    if channel == "pinned":
        raise InstallerError(
            f"{ref} is pinned; reinstall with --version NEW_TAG to move the "
            "pin; reinstall without --version for stable, or with --edge"
        )
    if channel == "source":
        raise InstallerError(
            "local source installations do not have an update channel; "
            "rerun the installer with --source-dir"
        )
    if channel == "stable":
        installed = tag_version(ref)
        target_ref = stable_tag(remote=True)
        target = tag_version(target_ref)
        if target < installed:
            raise InstallerError(
                f"refusing to downgrade stable from {ref} to {target_ref}"
            )
        if target == installed:
            log(f"already up to date ({ref})")
            return None
        return target_ref, False, "stable", None

    current_build = installed_edge_build(state, current)
    target_build = edge_build()
    if target_build < current_build:
        raise InstallerError(
            "refusing to downgrade edge from build "
            f"{current_build} to {target_build}"
        )
    if target_build == current_build:
        log(f"already up to date (edge build {current_build})")
        return None
    return "edge", True, "edge", target_build


def install(args):
    if sys.version_info < MIN_PYTHON:
        raise InstallerError("Python 3.10 or newer is required")
    if args.source_dir and (args.edge or args.version):
        raise InstallerError("--source-dir cannot be combined with --edge or --version")

    root, bin_dir = selected_locations(args)
    if args.update:
        selection = resolve_update(root, bin_dir)
        if selection is None:
            return
        ref, edge, channel, expected_edge_build = selection
    elif args.source_dir:
        ref = "source"
        edge = False
        channel = "source"
        expected_edge_build = None
    elif args.edge:
        ref = "edge"
        edge = True
        channel = "edge"
        expected_edge_build = edge_build()
    elif args.version:
        ref = validate_tag(args.version)
        edge = False
        channel = "pinned"
        expected_edge_build = None
    else:
        ref = stable_tag()
        edge = False
        channel = "stable"
        expected_edge_build = None

    launcher = launcher_path(bin_dir)
    launcher_content = (
        windows_launcher(root, bin_dir)
        if os.name == "nt" else posix_launcher(root)
    )
    collisions = launcher_collisions(bin_dir, launcher_content)
    if collisions:
        joined = ", ".join(str(path) for path in collisions)
        raise InstallerError(f"refusing launcher collision: {joined}")

    if args.source_dir:
        source = Path(args.source_dir).expanduser().resolve()
        if (
            root == source
            or root.is_relative_to(source)
            or source.is_relative_to(root)
            or bin_dir.is_relative_to(source)
        ):
            raise InstallerError("installer destinations and --source-dir must not overlap")

    root.mkdir(parents=True, exist_ok=True)
    marker = root / ROOT_MARKER
    entries = list(root.iterdir())
    if path_lexists(marker):
        if not valid_root_marker(marker):
            raise InstallerError(f"refusing an unrecognized installer marker: {marker}")
        new_managed_root = False
    else:
        if entries:
            raise InstallerError(f"refusing to use a non-empty unmanaged install root: {root}")
        create_root_marker(marker)
        new_managed_root = True
    versions_dir = root / "versions"
    if versions_dir.is_symlink():
        raise InstallerError(f"refusing a symlinked versions directory: {versions_dir}")
    versions_dir.mkdir(exist_ok=True)

    current_path = root / CURRENT_FILE
    if path_lexists(current_path) and (
        current_path.is_symlink() or not current_path.is_file()
    ):
        raise InstallerError(f"refusing an unexpected current pointer: {current_path}")
    old_current, old_ref = read_current(root)
    if path_lexists(current_path) and old_current is None:
        raise InstallerError(f"could not read a valid current pointer: {current_path}")

    state_path = root / STATE_FILE
    old_state_content = None
    if path_lexists(state_path):
        if state_path.is_symlink() or not state_path.is_file():
            raise InstallerError(f"refusing an unexpected installer state: {state_path}")
        try:
            old_state_content = state_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise InstallerError(f"could not read installer state {state_path}: {exc}") from exc
    old_state = read_state(root)
    if old_state_content is not None and not old_state:
        raise InstallerError(f"could not read valid installer state: {state_path}")
    state_valid = old_state.get("root") == str(root)
    if old_current is not None and not state_valid:
        raise InstallerError("the existing installation has no valid ownership metadata")
    if state_valid and isinstance(old_state.get("bin_dir"), str):
        if old_state["bin_dir"] != str(bin_dir):
            raise InstallerError(
                "moving an existing launcher's bin directory is not supported; "
                "uninstall first, then reinstall with the new location"
            )
    old_windows_path_owned = (
        state_valid
        and old_state.get("bin_dir") == str(bin_dir)
        and old_state.get("windows_path_added") is True
    )
    old_path_files = (
        list(old_state.get("path_files", []))
        if state_valid
        and isinstance(old_state.get("path_files", []), list)
        and all(isinstance(path, str) for path in old_state.get("path_files", []))
        else []
    )
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "-", ref) + "-"
    version_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=versions_dir))
    launcher_existed = path_lexists(launcher)
    launcher_created = False
    pointer_changed = False
    state_write_attempted = False
    added_path_files = []
    path_files = list(old_path_files)
    windows_path_added = False
    activated = False
    try:
        app_dir = stage_source(
            version_dir,
            source_dir=args.source_dir,
            ref=ref if ref != "source" else None,
            edge=edge,
            expected_edge_build=expected_edge_build,
        )
        staged_edge_build = (
            read_staged_edge_build(app_dir, expected_edge_build)
            if edge else None
        )
        build_environment(version_dir, app_dir, ref)

        bin_dir.mkdir(parents=True, exist_ok=True)
        if not launcher_existed:
            write_atomic(
                launcher,
                launcher_content,
                None if os.name == "nt" else 0o755,
            )
            launcher_created = True
        elif os.name != "nt":
            launcher_mode = stat.S_IMODE(launcher.stat().st_mode)
            if not launcher_mode & stat.S_IXUSR:
                launcher.chmod(launcher_mode | stat.S_IXUSR)
        write_atomic(current_path, f"{version_dir}\n{ref}\n")
        pointer_changed = True
        try:
            added_path_files, windows_path_added = configure_path(
                bin_dir, args.no_modify_path or args.update
            )
            path_files = list(dict.fromkeys(
                str(path) for path in (*old_path_files, *added_path_files)
            ))
        except (InstallerError, OSError) as exc:
            warn(f"installed successfully but PATH was not changed: {exc}")
        try:
            windows_path_added = old_windows_path_owned or windows_path_added
            state_write_attempted = True
            write_state(
                root, version_dir, old_current, ref, launcher, bin_dir,
                path_files, windows_path_added, channel, staged_edge_build,
            )
        except (OSError, UnicodeError) as exc:
            raise InstallerError(
                f"could not record installer ownership metadata: {exc}"
            ) from exc
        activated = True
        cleanup_versions(versions_dir, {version_dir, old_current})
    finally:
        if not activated:
            path_cleanup_ok = True
            if windows_path_added and not old_windows_path_owned:
                path_cleanup_ok = remove_windows_path(bin_dir)
            if added_path_files:
                path_cleanup_ok = (
                    remove_posix_path(added_path_files, bin_dir) and path_cleanup_ok
                )

            rollback_ok = path_cleanup_ok
            if pointer_changed and path_cleanup_ok:
                try:
                    if old_current is None:
                        current_path.unlink(missing_ok=True)
                    else:
                        write_atomic(current_path, f"{old_current}\n{old_ref}\n")
                except (OSError, UnicodeError) as exc:
                    rollback_ok = False
                    warn(f"could not restore the previous current pointer: {exc}")
            if state_write_attempted and path_cleanup_ok:
                try:
                    if old_state_content is None:
                        state_path.unlink(missing_ok=True)
                    else:
                        write_atomic(state_path, old_state_content)
                except (OSError, UnicodeError) as exc:
                    rollback_ok = False
                    warn(f"could not restore previous installer metadata: {exc}")
            elif not path_cleanup_ok:
                # Preserve a runnable generation and its ownership data when a
                # PATH mutation cannot be undone. A later uninstall can retry
                # the cleanup instead of losing track of it.
                try:
                    write_state(
                        root, version_dir, old_current, ref, launcher, bin_dir,
                        path_files, windows_path_added, channel,
                        staged_edge_build,
                    )
                except (OSError, UnicodeError) as exc:
                    warn(f"could not record PATH cleanup recovery metadata: {exc}")
                warn("PATH cleanup failed; preserving the managed installation")

            if rollback_ok:
                shutil.rmtree(version_dir, ignore_errors=True)
            else:
                warn(f"preserving staged version after incomplete rollback: {version_dir}")
            if (
                rollback_ok
                and launcher_created
                and managed_launcher(launcher, launcher_content)
            ):
                launcher.unlink(missing_ok=True)
            if rollback_ok and old_current is None and new_managed_root:
                try:
                    versions_dir.rmdir()
                    marker.unlink(missing_ok=True)
                    root.rmdir()
                except OSError:
                    pass

    log(f"installed {ref}")
    log(f"launcher: {launcher}")
    if args.update:
        log("PATH settings were preserved")
    elif args.no_modify_path:
        log("PATH was left unchanged; use the launcher path above")
    elif str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        log("open a new terminal for the PATH change to take effect")


def uninstall(args):
    root, bin_dir = selected_locations(args)
    marker = root / ROOT_MARKER
    if not valid_root_marker(marker):
        if root.exists() or path_lexists(marker):
            warn(f"preserving unrecognized install root: {root}")
        else:
            log("no managed installation found")
        return
    state = read_state(root)
    state_valid = state.get("root") == str(root)
    if args.bin_dir is None and state_valid and isinstance(state.get("bin_dir"), str):
        managed_bin_dir = Path(state["bin_dir"])
    else:
        managed_bin_dir = bin_dir
    expected_launcher = launcher_path(managed_bin_dir)
    expected_launcher_content = (
        windows_launcher(root, managed_bin_dir)
        if os.name == "nt" else posix_launcher(root)
    )
    launcher = expected_launcher
    if state_valid and state.get("launcher") == str(expected_launcher):
        launcher = Path(state["launcher"])
    path_files = (
        state.get("path_files", [])
        if state_valid and isinstance(state.get("path_files", []), list)
        else []
    )

    if valid_root_marker(marker):
        versions = root / "versions"
        if path_lexists(versions):
            if versions.is_symlink() or not versions.is_dir():
                raise InstallerError(
                    f"refusing to remove an unexpected versions path: {versions}"
                )
            try:
                shutil.rmtree(versions)
            except OSError as exc:
                raise InstallerError(
                    f"could not remove managed versions under {versions}: {exc}"
                ) from exc

        # Only discard ownership metadata after PATH cleanup succeeds. A failed
        # registry/profile edit can then be retried with the same uninstaller.
        path_cleanup_ok = True
        if os.name == "nt":
            if state_valid and state.get("windows_path_added") is True:
                path_cleanup_ok = remove_windows_path(managed_bin_dir)
        else:
            path_cleanup_ok = remove_posix_path(path_files, managed_bin_dir)
        if not path_cleanup_ok:
            raise InstallerError(
                "could not remove the managed PATH entry; ownership metadata "
                "was retained so uninstall can be retried"
            )

        if path_lexists(launcher):
            if managed_launcher(launcher, expected_launcher_content):
                launcher.unlink()
            else:
                warn(f"preserving unmanaged launcher: {launcher}")
        for name in (CURRENT_FILE, STATE_FILE, ROOT_MARKER):
            try:
                (root / name).unlink()
            except FileNotFoundError:
                pass
        if managed_bin_dir == root / "bin":
            try:
                managed_bin_dir.rmdir()
            except OSError:
                pass
        try:
            root.rmdir()
        except OSError:
            warn(f"preserved unrecognized files under {root}")
    elif root.exists():
        warn(f"preserving unmanaged install root: {root}")
    log("uninstalled managed files")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Install yt-ascii into a private, user-owned Python environment."
    )
    channel = parser.add_mutually_exclusive_group()
    channel.add_argument("--edge", action="store_true", help="install the main branch")
    channel.add_argument("--version", metavar="vX.Y.Z", help="install an exact stable tag")
    parser.add_argument("--no-modify-path", action="store_true",
                        help="do not add the launcher directory to the user PATH")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove installer-managed files and PATH entries")
    parser.add_argument("--update", action="store_true",
                        help="update an existing managed installation on its current channel")
    parser.add_argument("--source-dir", type=Path, metavar="DIR",
                        help="install a local checkout (for development and CI)")
    parser.add_argument("--install-root", type=Path, metavar="DIR",
                        help=argparse.SUPPRESS)
    parser.add_argument("--bin-dir", type=Path, metavar="DIR",
                        help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    selectors = bool(args.edge or args.version or args.source_dir)
    if args.uninstall and (selectors or args.update):
        raise InstallerError("--uninstall cannot be combined with an update or source selection")
    if args.update and selectors:
        raise InstallerError("--update cannot be combined with a source selection")
    root, _bin_dir = selected_locations(args)
    with installer_lock(root):
        if args.uninstall:
            uninstall(args)
        else:
            install(args)


if __name__ == "__main__":
    try:
        main()
    except (InstallerError, OSError, UnicodeError) as exc:
        print(f"yt-ascii installer: error: {exc}", file=sys.stderr)
        raise SystemExit(1)

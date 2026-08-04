"""Secure, standard-library-only update discovery for managed yt-ascii installs.

The player imports this module before its third-party dependencies.  It is
therefore deliberately limited to the Python standard library and treats the
installer's on-disk ownership metadata as the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import ntpath
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import threading
import urllib.error
import urllib.request


REPOSITORY = "EnchiladaBoy/Youtube-in-the-terminal"
RAW_MAIN = f"https://raw.githubusercontent.com/{REPOSITORY}/main"
STABLE_URL = f"{RAW_MAIN}/STABLE_VERSION"
EDGE_BUILD_URL = f"{RAW_MAIN}/EDGE_BUILD"
USER_AGENT = "yt-ascii-updater/0.5"
MAX_MARKER_BYTES = 128

ROOT_MARKER = ".yt-ascii-managed"
ROOT_MARKER_CONTENT = "yt-ascii managed installer v1\n"
CURRENT_FILE = "current"
STATE_FILE = "state.json"
MANAGED_MARKER = "yt-ascii managed installer"
CHANNELS = frozenset({"stable", "edge", "pinned", "source"})
TAG_RE = re.compile(
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
EDGE_BUILD_RE = re.compile(r"[1-9][0-9]*\Z")


class UpdateError(RuntimeError):
    """An update operation could not be performed safely."""


@dataclass(frozen=True)
class ManagedInstall:
    """Validated paths and channel metadata for one managed generation."""

    module_file: Path
    root: Path
    generation: Path
    app_dir: Path
    current_ref: str
    channel: str
    edge_build: int | None
    bin_dir: Path
    launcher: Path
    python: Path
    installer: Path


@dataclass(frozen=True)
class UpdateStatus:
    """The user-facing result of an update check."""

    channel: str
    current: str
    available: str | None
    display: str
    update_available: bool
    supported: bool = True
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _require_regular(path: Path, label: str) -> None:
    if not _lexists(path) or path.is_symlink() or not path.is_file():
        raise UpdateError(f"invalid managed installation: {label} is missing or unsafe")


def _require_directory(path: Path, label: str) -> None:
    if not _lexists(path) or path.is_symlink() or not path.is_dir():
        raise UpdateError(f"invalid managed installation: {label} is missing or unsafe")


def _read_bounded(path: Path, label: str, limit: int, encoding: str) -> str:
    _require_regular(path, label)
    try:
        with path.open("rb") as stream:
            payload = stream.read(limit + 1)
    except OSError as exc:
        raise UpdateError(f"could not read managed {label}: {exc}") from exc
    if len(payload) > limit:
        raise UpdateError(f"invalid managed installation: {label} is too large")
    try:
        return payload.decode(encoding)
    except UnicodeError as exc:
        raise UpdateError(f"invalid managed installation: {label} encoding") from exc


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError(f"invalid managed installer state: duplicate key {key!r}")
        result[key] = value
    return result


def _parse_state(path: Path) -> dict:
    raw = _read_bounded(path, "installer state", 64 * 1024, "utf-8")
    try:
        state = json.loads(raw, object_pairs_hook=_json_object)
    except UpdateError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise UpdateError("invalid managed installer state") from exc
    if not isinstance(state, dict) or type(state.get("schema")) is not int:
        raise UpdateError("invalid managed installer state schema")
    if state["schema"] != 1:
        raise UpdateError("unsupported managed installer state schema")
    return state


def _parse_tag(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or TAG_RE.fullmatch(value) is None:
        raise UpdateError("invalid stable version tag")
    return tuple(int(part) for part in value[1:].split("."))


def _marker_line(payload: bytes | str, label: str) -> str:
    if isinstance(payload, bytes):
        try:
            text = payload.decode("ascii")
        except UnicodeError as exc:
            raise UpdateError(f"invalid {label}: expected ASCII") from exc
    elif isinstance(payload, str):
        text = payload
        try:
            text.encode("ascii")
        except UnicodeError as exc:
            raise UpdateError(f"invalid {label}: expected ASCII") from exc
    else:
        raise UpdateError(f"invalid {label}: expected bytes")
    if len(text.encode("ascii")) > MAX_MARKER_BYTES:
        raise UpdateError(f"invalid {label}: response exceeded {MAX_MARKER_BYTES} bytes")
    if text.endswith("\r\n"):
        line = text[:-2]
    elif text.endswith("\n"):
        line = text[:-1]
    else:
        line = text
    if "\n" in line or "\r" in line:
        raise UpdateError(f"invalid {label}: expected exactly one line")
    return line


def parse_stable_tag(payload: bytes | str) -> str:
    """Parse one exact ``vX.Y.Z`` marker line."""

    value = _marker_line(payload, "STABLE_VERSION")
    _parse_tag(value)
    return value


def parse_edge_build(payload: bytes | str) -> int:
    """Parse one canonical, positive decimal edge build marker."""

    value = _marker_line(payload, "EDGE_BUILD")
    if EDGE_BUILD_RE.fullmatch(value) is None:
        raise UpdateError("invalid EDGE_BUILD: expected a positive integer")
    return int(value)


def _state_edge_build(value, *, required: bool) -> int | None:
    if value is None and not required:
        return None
    if type(value) is not int or value <= 0:
        raise UpdateError("invalid managed installer edge build")
    return value


def _posix_launcher(root: Path) -> str:
    # Update protocol v1: keep in byte-for-byte sync with install.py. An old
    # installer leaves this launcher in place while staging a new updater.
    # Future protocols must accept this form long enough to migrate it.
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


def _windows_launcher(root: Path, bin_dir: Path) -> str:
    default_bin = ntpath.normcase(ntpath.normpath(str(root / "bin")))
    selected_bin = ntpath.normcase(ntpath.normpath(str(bin_dir)))
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


def _expected_launcher(root: Path, bin_dir: Path) -> tuple[Path, str]:
    if os.name == "nt":
        return bin_dir / "yt-ascii.cmd", _windows_launcher(root, bin_dir)
    return bin_dir / "yt-ascii", _posix_launcher(root)


def _validate_channel(state: dict, ref: str) -> tuple[str, int | None]:
    explicit = "channel" in state
    if explicit:
        channel = state["channel"]
        if not isinstance(channel, str) or channel not in CHANNELS:
            raise UpdateError("invalid managed installer channel")
        if "edge_build" not in state:
            raise UpdateError("managed installer state is missing edge_build")
    elif ref == "edge":
        channel = "edge"
    elif ref == "source":
        channel = "source"
    elif TAG_RE.fullmatch(ref or "") is not None:
        channel = "pinned"
    else:
        raise UpdateError("invalid managed installer source reference")

    if channel == "edge":
        if ref != "edge":
            raise UpdateError("managed channel and source reference do not match")
        # A pre-channel edge installation has no build metadata.  Its first
        # explicit update safely refreshes it and writes the current build.
        edge_build = _state_edge_build(
            state.get("edge_build"), required=explicit
        )
    else:
        expected_ref = "source" if channel == "source" else None
        if expected_ref is not None and ref != expected_ref:
            raise UpdateError("managed channel and source reference do not match")
        if channel in {"stable", "pinned"}:
            _parse_tag(ref)
        if state.get("edge_build") is not None:
            raise UpdateError("non-edge installation has unexpected edge build metadata")
        edge_build = None
    return channel, edge_build


def discover_install(module_file=None) -> ManagedInstall:
    """Discover and fully validate the managed install containing this module.

    Source checkouts and copied modules intentionally fail discovery: updates
    must only mutate an installer-owned root and its currently active
    generation.
    """

    source = __file__ if module_file is None else module_file
    module = Path(os.path.abspath(os.fspath(source)))
    if module.name != "yt_ascii_update.py":
        raise UpdateError("update discovery requires the installed updater module")
    _require_regular(module, "updater module")
    app_dir = module.parent
    generation = app_dir.parent
    versions_dir = generation.parent
    root = versions_dir.parent
    if app_dir.name != "app" or versions_dir.name != "versions":
        raise UpdateError("updates are only available in an installer-managed installation")
    _require_directory(app_dir, "application directory")
    _require_directory(generation, "current generation")
    _require_directory(versions_dir, "versions directory")
    _require_directory(root, "install root")

    marker = _read_bounded(root / ROOT_MARKER, "root marker", 128, "ascii")
    if marker != ROOT_MARKER_CONTENT:
        raise UpdateError("invalid managed installation root marker")

    current_text = _read_bounded(root / CURRENT_FILE, "current pointer", 4096, "utf-8")
    lines = current_text.splitlines()
    if len(lines) != 2 or current_text != f"{lines[0]}\n{lines[1]}\n":
        raise UpdateError("invalid managed current pointer")
    if lines[0] != str(generation):
        raise UpdateError("the running generation is no longer current")
    ref = lines[1]
    if not ref or any(character in ref for character in "\x00\r\n"):
        raise UpdateError("invalid managed source reference")

    state = _parse_state(root / STATE_FILE)
    expected = {
        "root": str(root),
        "current": str(generation),
        "ref": ref,
    }
    for key, value in expected.items():
        if type(state.get(key)) is not str or state[key] != value:
            raise UpdateError(f"managed installer state does not own the current {key}")
    channel, edge_build = _validate_channel(state, ref)

    bin_value = state.get("bin_dir")
    launcher_value = state.get("launcher")
    if type(bin_value) is not str or type(launcher_value) is not str:
        raise UpdateError("managed installer state has invalid launcher ownership")
    bin_dir = Path(bin_value)
    if not bin_dir.is_absolute():
        raise UpdateError("managed installer bin directory is not absolute")
    _require_directory(bin_dir, "launcher directory")
    launcher, launcher_content = _expected_launcher(root, bin_dir)
    if launcher_value != str(launcher):
        raise UpdateError("managed installer state does not own the active launcher")
    actual_launcher = _read_bounded(launcher, "launcher", 64 * 1024, "utf-8")
    if actual_launcher != launcher_content:
        raise UpdateError("managed launcher content was modified")
    if os.name != "nt" and not (launcher.stat().st_mode & stat.S_IXUSR):
        raise UpdateError("managed launcher is not executable")

    installer = app_dir / "install.py"
    _require_regular(installer, "bundled installer")
    python = (
        generation / "venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else generation / "venv" / "bin" / "python"
    )
    if not python.is_file():
        raise UpdateError("invalid managed installation: private Python is missing")

    return ManagedInstall(
        module_file=module,
        root=root,
        generation=generation,
        app_dir=app_dir,
        current_ref=ref,
        channel=channel,
        edge_build=edge_build,
        bin_dir=bin_dir,
        launcher=launcher,
        python=python,
        installer=installer,
    )


def _fetch_marker(url: str, timeout: float, opener) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=timeout) as response:
            payload = response.read(MAX_MARKER_BYTES + 1)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise UpdateError(f"update check failed: {exc}") from exc
    if not isinstance(payload, bytes):
        raise UpdateError("update check failed: server returned non-byte data")
    if len(payload) > MAX_MARKER_BYTES:
        raise UpdateError(
            f"update check failed: response exceeded {MAX_MARKER_BYTES} bytes"
        )
    return payload


def _current_display(install: ManagedInstall) -> str:
    if install.channel == "edge":
        return (
            f"edge build {install.edge_build}"
            if install.edge_build is not None
            else "edge build unknown"
        )
    if install.channel == "source":
        return "source checkout"
    return install.current_ref


def _failed_status(install: ManagedInstall, message: str) -> UpdateStatus:
    return UpdateStatus(
        channel=install.channel,
        current=_current_display(install),
        available=None,
        display=message,
        update_available=False,
        error=message,
    )


def check_for_update(
    install: ManagedInstall, timeout: float = 10.0, opener=None
) -> UpdateStatus:
    """Check the install's own channel and return a non-throwing result."""

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        return _failed_status(install, "update check failed: timeout must be positive")
    current = _current_display(install)
    if install.channel == "pinned":
        return UpdateStatus(
            channel="pinned",
            current=current,
            available=None,
            display=(
                f"{current} is pinned; reinstall with --version NEW_TAG to move "
                "the pin, or choose stable/edge to receive updates"
            ),
            update_available=False,
            supported=False,
        )
    if install.channel == "source":
        return UpdateStatus(
            channel="source",
            current=current,
            available=None,
            display=(
                "local-source installations are updated from their original "
                "checkout with --source-dir"
            ),
            update_available=False,
            supported=False,
        )
    if install.channel not in {"stable", "edge"}:
        return _failed_status(install, "update check failed: unsupported install channel")

    selected_opener = urllib.request.urlopen if opener is None else opener
    try:
        if install.channel == "stable":
            available = parse_stable_tag(
                _fetch_marker(STABLE_URL, timeout, selected_opener)
            )
            current_version = _parse_tag(install.current_ref)
            available_version = _parse_tag(available)
        else:
            build = parse_edge_build(
                _fetch_marker(EDGE_BUILD_URL, timeout, selected_opener)
            )
            available = f"edge build {build}"
            current_version = install.edge_build
            available_version = build
    except UpdateError as exc:
        message = str(exc)
        if not message.startswith("update check failed:"):
            message = f"update check failed: {message}"
        return _failed_status(install, message)
    except Exception as exc:
        return _failed_status(install, f"update check failed: {exc}")

    update_available = current_version is None or available_version > current_version
    if update_available:
        display = f"update available: {current} -> {available}"
    elif available_version == current_version:
        display = f"up to date ({current})"
    else:
        display = f"installed {current} is newer than available {available}"
    return UpdateStatus(
        channel=install.channel,
        current=current,
        available=available,
        display=display,
        update_available=update_available,
    )


class AutomaticCheck:
    """One-shot holder for a best-effort background update check."""

    def __init__(self) -> None:
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._result: UpdateStatus | None = None
        self._consumed = False
        self._thread: threading.Thread | None = None

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def _finish(self, result: UpdateStatus | None) -> None:
        with self._lock:
            self._result = result
        self._done.set()

    def consume(self) -> UpdateStatus | None:
        """Return one actionable notice without waiting, otherwise ``None``."""

        if not self._done.is_set():
            return None
        with self._lock:
            if self._consumed:
                return None
            self._consumed = True
            result = self._result
        if (
            result is None
            or result.error is not None
            or not result.supported
            or not result.update_available
        ):
            return None
        return result


def start_auto_check(
    install: ManagedInstall, timeout: float = 2.0, opener=None
) -> AutomaticCheck:
    """Start a daemon check; unsupported channels are completed silently."""

    check = AutomaticCheck()
    if install.channel not in {"stable", "edge"}:
        check._finish(None)
        return check

    def worker():
        try:
            result = check_for_update(install, timeout=timeout, opener=opener)
        except Exception:
            result = None
        check._finish(result)

    thread = threading.Thread(
        target=worker,
        name="yt-ascii-update-check",
        daemon=True,
    )
    check._thread = thread
    thread.start()
    return check


def delegate_update(install: ManagedInstall, runner=None) -> int:
    """Run the bundled updater with rediscovered, explicit managed paths."""

    if install.channel == "pinned":
        raise UpdateError(
            "pinned installations cannot be updated in place; reinstall with "
            "--version NEW_TAG to move the pin; reinstall without --version "
            "for stable, or with --edge"
        )
    if install.channel == "source":
        raise UpdateError(
            "source installations cannot be updated in place; update the checkout "
            "and reinstall it with --source-dir"
        )
    if install.channel not in {"stable", "edge"}:
        raise UpdateError("unsupported install channel")
    current = discover_install(install.module_file)
    if current != install:
        raise UpdateError("managed installation changed while preparing the update")
    command = [
        str(current.python),
        str(current.installer),
        "--update",
        "--install-root",
        str(current.root),
        "--bin-dir",
        str(current.bin_dir),
    ]
    selected_runner = subprocess.run if runner is None else runner
    try:
        completed = selected_runner(command, check=False)
    except OSError as exc:
        raise UpdateError(f"could not start the bundled installer: {exc}") from exc
    code = getattr(completed, "returncode", None)
    if type(code) is not int:
        raise UpdateError("bundled installer returned no exit status")
    return code

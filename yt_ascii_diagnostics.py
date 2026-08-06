"""Bounded, privacy-preserving playback diagnostics for yt-ascii.

The player imports this module only when a diagnostics report is requested.
It intentionally uses only the Python standard library and never retains raw
media URLs, titles, command lines, effect text, hostnames, usernames, paths, or
FFmpeg stderr.
"""

from __future__ import annotations

import bisect
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import sys
import tempfile
import time


SCHEMA_VERSION = 1
MAX_EVENTS = 512
MAX_METRICS = 64
MAX_EVENT_FIELDS = 12
MAX_MINUTE_SAMPLES = 240
MAX_TRACKED_CHILDREN = 16

# Fixed buckets make collection O(1) in memory for arbitrarily long runs. The
# frame-budget boundaries are represented exactly enough for the 30/60 fps
# diagnostic gates; values above the final bucket use the exact maximum.
_BASE_TIMING_BUCKETS_MS = (
    0.0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 3.0, 4.0,
    5.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 16.667, 20.0, 25.0,
    30.0, 33.333, 40.0, 50.0, 66.667, 100.0, 150.0, 200.0,
    250.0, 333.333, 500.0, 750.0, 1_000.0, 2_000.0, 5_000.0,
    10_000.0,
)
TIMING_BUCKETS_MS = tuple(sorted(set(
    _BASE_TIMING_BUCKETS_MS
    + tuple(1_000.0 * multiplier / fps for fps in range(1, 61)
            for multiplier in (1.0, 1.5, 2.0))
)))

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,63}\Z")
_METRIC_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_FFMPEG_TIME_RE = re.compile(
    rb"bench:\s*utime=([0-9]+(?:\.[0-9]+)?)s\s+"
    rb"stime=([0-9]+(?:\.[0-9]+)?)s\s+"
    rb"rtime=([0-9]+(?:\.[0-9]+)?)s"
)
_FFMPEG_RSS_RE = re.compile(rb"bench:\s*maxrss=([0-9]+)kB")

_CONFIG_TOKEN_FIELDS = frozenset((
    "profile", "style", "effect", "glyph_mode", "render_backend",
    "effective_render_backend", "output_environment", "palette", "reveal",
    "effect_text_sha256",
))
_CONFIG_BOOL_FIELDS = frozenset((
    "color", "audio", "eight_bit", "eight_bit_audio", "scatter", "rain",
))
_CONFIG_NUMBER_FIELDS = frozenset((
    "target_fps", "width", "height", "max_res", "effect_speed",
    "effect_seed", "matched_sink_write_p95_ms",
    "diagnostics_overhead_percent",
))
_SOURCE_TOKEN_FIELDS = frozenset(("kind",))
_SOURCE_BOOL_FIELDS = frozenset(("live",))
_SOURCE_NUMBER_FIELDS = frozenset((
    "width", "height", "duration_seconds", "fps",
))
_ENV_TOKEN_FIELDS = frozenset(("output_environment",))
_ENV_BOOL_FIELDS = frozenset((
    "stdin_tty", "stdout_tty", "tmux", "ci",
))
_EFFECT_TOKENS = frozenset((
    "none", "pixelate", "glitch", "crt", "chromatic-shift", "wave", "trails",
    "prism", "digital-rain", "terminal-hud",
))
_RENDER_BACKEND_TOKENS = frozenset(("chars", "cells", "half-block"))
_CONFIG_TOKEN_CHOICES = {
    "profile": frozenset(("ordinary", "stress")),
    "style": frozenset((
        "classic", "bayer", "posterize", "contour", "edge-glow",
        "ordered-dither", "error-diffusion", "duotone", "two-tone",
        "riso",
    )),
    "effect": _EFFECT_TOKENS,
    "glyph_mode": frozenset(("ascii", "unicode")),
    "render_backend": _RENDER_BACKEND_TOKENS,
    "effective_render_backend": _RENDER_BACKEND_TOKENS,
    "output_environment": frozenset(("sink", "tmux", "attached", "terminal", "unknown")),
    "palette": frozenset(("simple", "dense", "blocks", "binary", "numbers", "symbols", "matrix", "custom")),
    "reveal": frozenset(("none", "scatter", "rain")),
}
_SOURCE_KIND_TOKENS = frozenset(("remote",))
_EVENT_NAMES = frozenset((
    "first_frame", "probe_failure", "audio_mode", "control", "resize",
    "reconnect", "frame_drain_eof", "decoder_eof", "child_before_cleanup",
    "child_before_stop",
))
_EVENT_TOKEN_VALUES = {
    "reason": frozenset((
        "eight_bit", "normal", "disabled", "seek", "pause", "resume",
        "exhausted", "start", "resumed", "palette", "style", "effect",
        "quit",
        "completed", "error",
        "video", "audio",
    )),
    "action": frozenset((
        "seek", "pause", "resume", "palette", "style", "effect", "quit",
        "resize",
    )),
    "outcome": frozenset(("start", "resumed", "completed", "failed", "exhausted")),
    "style": _CONFIG_TOKEN_CHOICES["style"],
    "effect": _EFFECT_TOKENS,
    "palette": _CONFIG_TOKEN_CHOICES["palette"],
    "value": (
        _EFFECT_TOKENS
        | _CONFIG_TOKEN_CHOICES["style"]
        | _CONFIG_TOKEN_CHOICES["palette"]
    ),
}
_EVENT_FIELDS = frozenset((
    "reason", "position_seconds", "from_seconds", "to_seconds",
    "duration_seconds", "count", "width", "height", "palette", "style",
    "effect",
    "paused", "success", "exit_code", "signal", "action", "value",
    "enabled", "eight_bit", "outcome", "attempt", "requested", "completed",
    "received", "expected",
))

_NORMAL_EXITS = frozenset((
    "normal", "completed", "duration", "duration_complete", "user_quit",
    "keyboard_interrupt", "end_of_stream",
))
_EXTERNAL_FAILURE_EXITS = frozenset((
    "probe_error", "source_unavailable", "decoder_unavailable",
    "external_source_unavailable",
))

_PROFILE_GATES = {
    "ordinary": {
        "minimum_fps_factor": 0.98,
        "maximum_drop_percent": 1.0,
        "maximum_p95_lateness_frames": 1.0,
        "maximum_p99_lateness_frames": 1.5,
        "maximum_freeze_ms": 250.0,
        "performance_hard": True,
    },
    "stress": {
        "minimum_fps_factor": 0.95,
        "maximum_drop_percent": 5.0,
        "maximum_p95_lateness_frames": 1.0,
        "maximum_p99_lateness_frames": 2.0,
        "maximum_freeze_ms": 500.0,
        "performance_hard": False,
    },
}


class DiagnosticsReportError(RuntimeError):
    """A requested diagnostics report could not be safely produced."""


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if isinstance(value, int):
        return value
    return number


def _safe_token(value, default="redacted"):
    if isinstance(value, str) and _TOKEN_RE.fullmatch(value):
        return value
    return default


def _sanitize_mapping(values, token_fields, bool_fields, number_fields):
    """Return only explicitly safe fields, silently dropping everything else."""
    if not isinstance(values, dict):
        return {}
    clean = {}
    for key in token_fields:
        if key in values:
            clean[key] = _safe_token(values[key])
    for key in bool_fields:
        if key in values and isinstance(values[key], bool):
            clean[key] = values[key]
    for key in number_fields:
        if key in values:
            number = _finite_number(values[key])
            if number is not None:
                clean[key] = number
    return clean


def _restrict_token_choices(values, choices):
    for key, allowed in choices.items():
        if key in values and values[key] not in allowed:
            values[key] = "redacted"
    return values


def _isatty(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _diagnosis(code, severity, observed, threshold, remediation):
    return {
        "code": code,
        "severity": severity,
        "observed": observed,
        "threshold": threshold,
        "remediation": remediation,
    }


def _histogram_milliseconds_as_percent(histogram):
    raw = histogram.as_dict()
    return {
        "count": raw["count"],
        "mean_percent": raw["mean_ms"],
        "minimum_percent": raw["minimum_ms"],
        "p50_percent": raw["p50_ms"],
        "p95_percent": raw["p95_ms"],
        "p99_percent": raw["p99_ms"],
        "maximum_percent": raw["maximum_ms"],
        "histogram": {
            "buckets": [
                {"le_percent": item["le_ms"], "count": item["count"]}
                for item in raw["histogram"]["buckets"]
            ],
            "over_10000_percent": raw["histogram"]["over_10000ms"],
        },
    }


class TimingHistogram:
    """A fixed-bucket timing distribution with bounded-memory quantiles."""

    __slots__ = ("_counts", "_overflow", "count", "total_ms", "minimum_ms", "maximum_ms")

    def __init__(self):
        self._counts = [0] * len(TIMING_BUCKETS_MS)
        self._overflow = 0
        self.count = 0
        self.total_ms = 0.0
        self.minimum_ms = None
        self.maximum_ms = None

    def add_seconds(self, seconds):
        value = _finite_number(seconds)
        if value is None or value < 0:
            raise ValueError("timing samples must be finite and nonnegative")
        milliseconds = float(value) * 1_000.0
        self.count += 1
        self.total_ms += milliseconds
        self.minimum_ms = (
            milliseconds if self.minimum_ms is None
            else min(self.minimum_ms, milliseconds)
        )
        self.maximum_ms = (
            milliseconds if self.maximum_ms is None
            else max(self.maximum_ms, milliseconds)
        )
        index = bisect.bisect_left(TIMING_BUCKETS_MS, milliseconds)
        if index < len(TIMING_BUCKETS_MS):
            self._counts[index] += 1
        else:
            self._overflow += 1

    def quantile_ms(self, quantile):
        if not self.count:
            return None
        q = float(quantile)
        if not 0.0 <= q <= 1.0:
            raise ValueError("quantile must be between zero and one")
        rank = max(1, math.ceil(q * self.count))
        cumulative = 0
        for boundary, count in zip(TIMING_BUCKETS_MS, self._counts):
            cumulative += count
            if cumulative >= rank:
                return boundary
        return self.maximum_ms

    def as_dict(self):
        buckets = [
            {"le_ms": boundary, "count": count}
            for boundary, count in zip(TIMING_BUCKETS_MS, self._counts)
            if count
        ]
        return {
            "count": self.count,
            "mean_ms": self.total_ms / self.count if self.count else None,
            "minimum_ms": self.minimum_ms,
            "p50_ms": self.quantile_ms(0.50),
            "p95_ms": self.quantile_ms(0.95),
            "p99_ms": self.quantile_ms(0.99),
            "maximum_ms": self.maximum_ms,
            "histogram": {
                "buckets": buckets,
                "over_10000ms": self._overflow,
            },
        }


class _SeriesAccumulator:
    """Bounded summary of a 1 Hz value series and its per-minute medians."""

    __slots__ = (
        "count", "first", "last", "minimum", "maximum", "_minute_index",
        "_minute_values", "_minute_medians", "_minute_indices",
    )

    def __init__(self):
        self.count = 0
        self.first = None
        self.last = None
        self.minimum = None
        self.maximum = None
        self._minute_index = None
        self._minute_values = []
        self._minute_medians = deque(maxlen=MAX_MINUTE_SAMPLES)
        self._minute_indices = deque(maxlen=MAX_MINUTE_SAMPLES)

    def _commit_minute(self):
        if self._minute_values:
            self._minute_medians.append(
                float(statistics.median(self._minute_values))
            )
            self._minute_indices.append(self._minute_index)
            self._minute_values.clear()

    def add(self, elapsed_seconds, value):
        number = _finite_number(value)
        if number is None:
            return
        minute = max(0, int(float(elapsed_seconds) // 60.0))
        if self._minute_index is None:
            self._minute_index = minute
        elif minute != self._minute_index:
            self._commit_minute()
            self._minute_index = minute
        number = float(number)
        self._minute_values.append(number)
        self.count += 1
        if self.first is None:
            self.first = number
        self.last = number
        self.minimum = number if self.minimum is None else min(self.minimum, number)
        self.maximum = number if self.maximum is None else max(self.maximum, number)

    def _minute_points(self):
        points = list(zip(self._minute_indices, self._minute_medians))
        if self._minute_values:
            points.append((
                self._minute_index,
                float(statistics.median(self._minute_values)),
            ))
        return points[-MAX_MINUTE_SAMPLES:]

    def as_dict(self):
        points = self._minute_points()
        medians = [value for _minute, value in points]
        slopes = [
            (later_value - earlier_value) / max(1, later_minute - earlier_minute)
            for (earlier_minute, earlier_value), (later_minute, later_value)
            in zip(points, points[1:])
        ]
        return {
            "sample_count": self.count,
            "first": self.first,
            "last": self.last,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "growth": (
                self.last - self.first
                if self.first is not None and self.last is not None else None
            ),
            "maximum_growth": (
                self.maximum - self.first
                if self.first is not None and self.maximum is not None else None
            ),
            "median_per_minute_slope": (
                float(statistics.median(slopes)) if slopes else None
            ),
            "minute_medians": medians,
            "minute_samples_truncated": (
                len(self._minute_medians) == MAX_MINUTE_SAMPLES
            ),
        }


class _ProcessResourceReader:
    """Best-effort current/peak process metrics with capability labels."""

    def __init__(self):
        self._rss = None
        self._fd = None
        self.failures = 0
        self.capabilities = {
            "rss": {"supported": False, "kind": None, "provider": None},
            "fd": {"supported": False, "kind": None, "provider": None},
            "child_rss": {"supported": False, "kind": None, "provider": None},
            "child_fd": {"supported": False, "kind": None, "provider": None},
        }
        if sys.platform.startswith("linux"):
            self._rss = self._linux_rss
            self._fd = self._proc_fd_count
            self.capabilities = {
                "rss": {"supported": True, "kind": "current", "provider": "procfs"},
                "fd": {"supported": True, "kind": "current", "provider": "procfs"},
                "child_rss": {"supported": True, "kind": "current", "provider": "procfs"},
                "child_fd": {"supported": True, "kind": "current", "provider": "procfs"},
            }
        elif sys.platform == "darwin":
            self._rss = self._darwin_peak_rss
            self._fd = self._dev_fd_count
            self.capabilities = {
                "rss": {"supported": True, "kind": "peak", "provider": "getrusage"},
                "fd": {"supported": True, "kind": "current", "provider": "devfd"},
                "child_rss": {"supported": True, "kind": "current", "provider": "libproc"},
                "child_fd": {"supported": True, "kind": "current", "provider": "libproc"},
            }
        elif os.name == "nt":  # pragma: no cover - exercised on Windows CI
            self._rss = self._windows_rss
            self._fd = self._windows_handle_count
            self.capabilities = {
                "rss": {"supported": True, "kind": "current", "provider": "winapi"},
                "fd": {"supported": True, "kind": "handle_count", "provider": "winapi"},
                "child_rss": {"supported": True, "kind": "current", "provider": "winapi"},
                "child_fd": {"supported": True, "kind": "handle_count", "provider": "winapi"},
            }
        else:
            try:
                import resource  # noqa: F401
            except ImportError:
                pass
            else:
                self._rss = self._portable_peak_rss
                self.capabilities["rss"] = {
                    "supported": True, "kind": "peak", "provider": "getrusage",
                }

    @staticmethod
    def _linux_rss():
        with open("/proc/self/statm", "r", encoding="ascii") as stream:
            fields = stream.read(128).split()
        return int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))

    @staticmethod
    def _proc_fd_count():
        return len(os.listdir("/proc/self/fd"))

    @staticmethod
    def _dev_fd_count():
        return len(os.listdir("/dev/fd"))

    @staticmethod
    def _darwin_peak_rss():
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    @staticmethod
    def _portable_peak_rss():
        import resource
        # Most non-Darwin Unix implementations expose KiB here.
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1_024

    @staticmethod
    def _windows_rss():  # pragma: no cover - exercised on Windows CI
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD,
        )
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        if not psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.WorkingSetSize)

    @staticmethod
    def _windows_handle_count():  # pragma: no cover - exercised on Windows CI
        import ctypes
        from ctypes import wintypes
        count = wintypes.DWORD()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        if not kernel32.GetProcessHandleCount(
            handle, ctypes.byref(count)
        ):
            raise OSError("GetProcessHandleCount failed")
        return int(count.value)

    def read(self):
        result = {"rss_bytes": None, "fd_count": None}
        if self._rss is not None:
            try:
                result["rss_bytes"] = self._rss()
            except (OSError, ValueError, IndexError):
                self.failures += 1
        if self._fd is not None:
            try:
                result["fd_count"] = self._fd()
            except (OSError, ValueError, IndexError):
                self.failures += 1
        return result

    def read_child(self, pid):
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/statm", "r", encoding="ascii") as stream:
                fields = stream.read(128).split()
            rss = int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))
            return {"rss_bytes": rss, "fd_count": len(os.listdir(f"/proc/{pid}/fd"))}
        if sys.platform == "darwin":  # pragma: no cover - macOS CI
            return self._darwin_child(pid)
        if os.name == "nt":  # pragma: no cover - Windows CI
            return self._windows_child(pid)
        return {"rss_bytes": None, "fd_count": None}

    @staticmethod
    def _darwin_child(pid):  # pragma: no cover - macOS CI
        import ctypes
        import ctypes.util

        class ProcTaskInfo(ctypes.Structure):
            _fields_ = [
                ("pti_virtual_size", ctypes.c_uint64),
                ("pti_resident_size", ctypes.c_uint64),
                ("pti_total_user", ctypes.c_uint64),
                ("pti_total_system", ctypes.c_uint64),
                ("pti_threads_user", ctypes.c_uint64),
                ("pti_threads_system", ctypes.c_uint64),
                ("pti_policy", ctypes.c_int32),
                ("pti_faults", ctypes.c_int32),
                ("pti_pageins", ctypes.c_int32),
                ("pti_cow_faults", ctypes.c_int32),
                ("pti_messages_sent", ctypes.c_int32),
                ("pti_messages_received", ctypes.c_int32),
                ("pti_syscalls_mach", ctypes.c_int32),
                ("pti_syscalls_unix", ctypes.c_int32),
                ("pti_csw", ctypes.c_int32),
                ("pti_threadnum", ctypes.c_int32),
                ("pti_numrunning", ctypes.c_int32),
                ("pti_priority", ctypes.c_int32),
            ]

        library_name = ctypes.util.find_library("proc")
        if not library_name:
            raise OSError("libproc unavailable")
        libproc = ctypes.CDLL(library_name, use_errno=True)
        libproc.proc_pidinfo.argtypes = (
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
            ctypes.c_void_p, ctypes.c_int,
        )
        libproc.proc_pidinfo.restype = ctypes.c_int
        info = ProcTaskInfo()
        size = libproc.proc_pidinfo(
            int(pid), 4, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        if size != ctypes.sizeof(info):
            raise OSError("proc_pidinfo task query failed")
        # PROC_PIDLISTFDS (1) returns the required byte count when buffer=NULL.
        fd_bytes = libproc.proc_pidinfo(int(pid), 1, 0, None, 0)
        fd_count = max(0, int(fd_bytes) // 8) if fd_bytes >= 0 else None
        return {"rss_bytes": int(info.pti_resident_size), "fd_count": fd_count}

    @staticmethod
    def _windows_child(pid):  # pragma: no cover - Windows CI
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD,
        )
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x0410, False, int(pid))
        if not handle:
            raise OSError("OpenProcess failed")
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                raise OSError("GetProcessMemoryInfo failed")
            count = wintypes.DWORD()
            if not kernel32.GetProcessHandleCount(
                handle, ctypes.byref(count)
            ):
                raise OSError("GetProcessHandleCount failed")
            return {
                "rss_bytes": int(counters.WorkingSetSize),
                "fd_count": int(count.value),
            }
        finally:
            kernel32.CloseHandle(handle)


class FFmpegBenchmarkCapture:
    """Temporary, unlinked FFmpeg stderr capture consumed into numeric totals."""

    __slots__ = ("stream", "consumed")

    def __init__(self):
        self.stream = tempfile.TemporaryFile(mode="w+b")
        self.consumed = False

    def close(self):
        if not self.stream.closed:
            self.stream.close()


def parse_ffmpeg_benchmark(data):
    """Parse only numeric ``bench:`` totals; all other FFmpeg text is ignored."""
    if isinstance(data, str):
        data = data.encode("ascii", errors="ignore")
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("FFmpeg benchmark data must be bytes or text")
    times = list(_FFMPEG_TIME_RE.finditer(data))
    rss_values = [int(value) * 1_024 for value in _FFMPEG_RSS_RE.findall(data)]
    return {
        "sample_count": len(times),
        "user_cpu_seconds": sum(float(match.group(1)) for match in times),
        "system_cpu_seconds": sum(float(match.group(2)) for match in times),
        "real_seconds": sum(float(match.group(3)) for match in times),
        "maximum_rss_bytes": max(rss_values) if rss_values else None,
    }


class PlaybackDiagnostics:
    """Collect and atomically finalize one live playback diagnostics report."""

    def __init__(
        self,
        path,
        warmup_seconds=0.0,
        duration_seconds=None,
        *,
        config=None,
        source=None,
        environment=None,
        profile="ordinary",
        clock=None,
        process_clock=None,
        resource_reader=None,
    ):
        warmup = _finite_number(warmup_seconds)
        if warmup is None or warmup < 0:
            raise ValueError("diagnostics warmup must be finite and nonnegative")
        if duration_seconds is None:
            duration = None
        else:
            duration = _finite_number(duration_seconds)
            if duration is None or duration <= 0:
                raise ValueError("diagnostics duration must be finite and positive")
        if profile not in _PROFILE_GATES:
            raise ValueError(
                "diagnostics profile must be ordinary or stress"
            )

        self.path = Path(path)
        if os.path.lexists(self.path):
            raise DiagnosticsReportError("diagnostics report already exists")
        if not self.path.parent.is_dir():
            raise DiagnosticsReportError("diagnostics report directory does not exist")

        self.warmup_seconds = float(warmup)
        self.duration_seconds = None if duration is None else float(duration)
        self.profile = profile
        self._clock = clock or time.monotonic
        self._process_clock = process_clock or time.process_time
        self._created_at = self._clock()
        self._created_cpu = self._process_clock()
        self._first_frame_at = None
        self._pending_first_frame = False
        self._measurement_start_at = None
        self._finalized_report = None

        self.config = _restrict_token_choices(_sanitize_mapping(
            config, _CONFIG_TOKEN_FIELDS, _CONFIG_BOOL_FIELDS,
            _CONFIG_NUMBER_FIELDS,
        ), _CONFIG_TOKEN_CHOICES)
        self.config["profile"] = profile
        self.source = _sanitize_mapping(
            source, _SOURCE_TOKEN_FIELDS, _SOURCE_BOOL_FIELDS,
            _SOURCE_NUMBER_FIELDS,
        )
        if "kind" in self.source and self.source["kind"] not in _SOURCE_KIND_TOKENS:
            self.source["kind"] = "redacted"
        supplied_environment = _restrict_token_choices(_sanitize_mapping(
            environment, _ENV_TOKEN_FIELDS, _ENV_BOOL_FIELDS, frozenset(),
        ), {"output_environment": _CONFIG_TOKEN_CHOICES["output_environment"]})
        self.environment = {
            "os": _safe_token(platform.system() or "unknown"),
            "python": _safe_token(platform.python_version()),
            "implementation": _safe_token(platform.python_implementation()),
            "architecture": _safe_token(platform.machine() or "unknown"),
            "stdin_tty": _isatty(sys.stdin),
            "stdout_tty": _isatty(sys.stdout),
            "tmux": bool(os.environ.get("TMUX")),
            "ci": bool(os.environ.get("CI")),
        }
        self.environment.update(supplied_environment)

        self._timings = {}
        self._startup_timings = {}
        self._timing_names_overflow = 0
        self._excluded_timing_samples = 0
        self._counts_all = {}
        self._counts_measured = {}
        self._count_names_overflow = 0
        self._events = []
        self._events_total = 0
        self._events_overflow = 0
        self._maximum_drop_burst = 0
        self._cleanup = None
        self._matched_sink_write_p95_ms = self.config.get(
            "matched_sink_write_p95_ms"
        )

        self._resource_reader = resource_reader or _ProcessResourceReader()
        self._resource_failures = 0
        self._last_resource_at = None
        self._last_resource_cpu = None
        self._rss = _SeriesAccumulator()
        self._fd = _SeriesAccumulator()
        self._child_rss = _SeriesAccumulator()
        self._child_fd = _SeriesAccumulator()
        self._children = {}
        self._child_roles_seen = set()
        self._child_registration_overflow = 0
        self._child_sample_failures = 0
        self._cpu_percent = TimingHistogram()

        self._ffmpeg = {
            "captures_created": 0,
            "captures_consumed": 0,
            "benchmark_samples": 0,
            "parse_failures": 0,
            "user_cpu_seconds": 0.0,
            "system_cpu_seconds": 0.0,
            "real_seconds": 0.0,
            "maximum_rss_bytes": None,
        }
        self._sample_resources(self._created_at, force=True)

    @staticmethod
    def _validate_metric_name(name):
        if not isinstance(name, str) or not _METRIC_RE.fullmatch(name):
            raise ValueError("diagnostics metric names must be safe identifiers")
        return name

    def _histogram(self, collection, name):
        name = self._validate_metric_name(name)
        histogram = collection.get(name)
        if histogram is None:
            if len(collection) >= MAX_METRICS:
                self._timing_names_overflow += 1
                return None
            histogram = collection[name] = TimingHistogram()
        return histogram

    def _in_measurement(self, now):
        if self._measurement_start_at is None or now < self._measurement_start_at:
            return False
        return (
            self.duration_seconds is None
            or now <= self._measurement_start_at + self.duration_seconds
        )

    def _increment_dict(self, values, name, amount):
        if name not in values and len(values) >= MAX_METRICS:
            self._count_names_overflow += 1
            return
        values[name] = values.get(name, 0) + amount

    def increment(self, name, amount=1):
        name = self._validate_metric_name(name)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("diagnostics counts must be nonnegative integers")
        now = self._clock()
        self._increment_dict(self._counts_all, name, amount)
        if self._in_measurement(now):
            self._increment_dict(self._counts_measured, name, amount)

    def event(self, name, **fields):
        name = self._validate_metric_name(name)
        if name not in _EVENT_NAMES:
            raise ValueError("unsupported diagnostics event name")
        now = self._clock()
        self._events_total += 1
        if name == "control":
            self._increment_dict(self._counts_all, "control_events", 1)
            if self._in_measurement(now):
                self._increment_dict(self._counts_measured, "control_events", 1)
        if name == "reconnect" and fields.get("outcome") == "start":
            self._increment_dict(self._counts_all, "reconnects", 1)
            if self._in_measurement(now):
                self._increment_dict(self._counts_measured, "reconnects", 1)
        if len(self._events) >= MAX_EVENTS:
            self._events_overflow += 1
            return
        clean = {}
        for key in list(fields)[:MAX_EVENT_FIELDS]:
            if key not in _EVENT_FIELDS:
                continue
            value = fields[key]
            if isinstance(value, bool):
                clean[key] = value
            else:
                number = _finite_number(value)
                if number is not None:
                    clean[key] = number
                elif isinstance(value, str):
                    allowed = _EVENT_TOKEN_VALUES.get(key, frozenset())
                    clean[key] = value if value in allowed else "redacted"
        self._events.append({
            "name": name,
            "elapsed_seconds": max(0.0, now - self._created_at),
            "measured": self._in_measurement(now),
            "fields": clean,
        })

    def set_source_metadata(
        self, *, width=None, height=None, duration_seconds=None, live=None,
        fps=None,
    ):
        updates = {
            "width": width,
            "height": height,
            "duration_seconds": duration_seconds,
            "live": live,
            "fps": fps,
        }
        self.source.update(_sanitize_mapping(
            updates, frozenset(), _SOURCE_BOOL_FIELDS, _SOURCE_NUMBER_FIELDS,
        ))

    def set_output_geometry(self, *, width, height, target_fps):
        """Record resolved cell geometry and select the matching gate profile."""
        values = (width, height, target_fps)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
            for value in values
        ):
            raise ValueError("output geometry and target fps must be positive")
        width = int(width)
        height = int(height)
        target_fps = float(target_fps)
        profile = (
            "stress"
            if target_fps >= 60 and width >= 240 and height >= 68
            else "ordinary"
        )
        self.config.update({
            "width": width,
            "height": height,
            "target_fps": target_fps,
            "profile": profile,
        })
        self.profile = profile
        return profile

    def set_matched_sink_write_p95(self, milliseconds):
        value = _finite_number(milliseconds)
        if value is None or value < 0:
            raise ValueError("matched sink timing must be finite and nonnegative")
        self._matched_sink_write_p95_ms = float(value)

    @contextmanager
    def timer(self, stage, *, startup=False):
        started = self._clock()
        try:
            yield
        finally:
            self.record_timing(stage, max(0.0, self._clock() - started), startup=startup)

    def record_timing(self, stage, seconds, *, startup=False):
        now = self._clock()
        if startup:
            histogram = self._histogram(self._startup_timings, stage)
        elif self._in_measurement(now):
            histogram = self._histogram(self._timings, stage)
        else:
            self._excluded_timing_samples += 1
            return
        if histogram is not None:
            histogram.add_seconds(seconds)

    def mark_first_frame(self):
        if self._first_frame_at is None:
            now = self._clock()
            self._first_frame_at = now
            self._measurement_start_at = now + self.warmup_seconds
            self._pending_first_frame = True
            self.event("first_frame")

    def record_frame(
        self, *, output_bytes=0, dropped=0, lateness_seconds=None,
        loop_seconds=None,
    ):
        if isinstance(output_bytes, bool) or not isinstance(output_bytes, int) or output_bytes < 0:
            raise ValueError("output bytes must be a nonnegative integer")
        if isinstance(dropped, bool) or not isinstance(dropped, int) or dropped < 0:
            raise ValueError("dropped frames must be a nonnegative integer")
        self.mark_first_frame()
        now = self._clock()
        # The first displayed frame is the end of startup and arms the window;
        # its startup latency must not contaminate live pacing percentiles.
        startup_frame = self._pending_first_frame
        self._pending_first_frame = False
        measured = self._in_measurement(now) and not startup_frame
        for name, amount in (
            ("frames_presented", 1), ("frames_dropped", dropped),
            ("output_bytes", output_bytes),
        ):
            self._increment_dict(self._counts_all, name, amount)
            if measured:
                self._increment_dict(self._counts_measured, name, amount)
        if dropped:
            self._maximum_drop_burst = max(self._maximum_drop_burst, dropped)
            self._increment_dict(self._counts_all, "drop_bursts", 1)
            if measured:
                self._increment_dict(self._counts_measured, "drop_bursts", 1)
        if lateness_seconds is not None and not startup_frame:
            self.record_timing("lateness", max(0.0, float(lateness_seconds)))
        if loop_seconds is not None and not startup_frame:
            self.record_timing("loop", max(0.0, float(loop_seconds)))

    def _sample_resources(self, now, *, force=False):
        if (
            not force and self._last_resource_at is not None
            and now - self._last_resource_at < 1.0
        ):
            return
        process_cpu = self._process_clock()
        if self._last_resource_at is not None and now > self._last_resource_at:
            cpu_delta = max(0.0, process_cpu - self._last_resource_cpu)
            cpu_percent = cpu_delta / (now - self._last_resource_at) * 100.0
            # Reuse the fixed distribution implementation; percentages are
            # stored as seconds/1000 so its exported numeric values stay exact.
            self._cpu_percent.add_seconds(cpu_percent / 1_000.0)
        self._last_resource_at = now
        self._last_resource_cpu = process_cpu
        try:
            sample = self._resource_reader.read()
        except (OSError, ValueError, IndexError, AttributeError):
            self._resource_failures += 1
            return
        elapsed = max(0.0, now - self._created_at)
        self._rss.add(elapsed, sample.get("rss_bytes"))
        self._fd.add(elapsed, sample.get("fd_count"))
        child_rss = child_fd = 0.0
        rss_samples = fd_samples = 0
        for pid in tuple(self._children):
            try:
                child = self._resource_reader.read_child(pid)
            except (OSError, ValueError, IndexError, AttributeError):
                self._child_sample_failures += 1
                continue
            rss_value = _finite_number(child.get("rss_bytes"))
            fd_value = _finite_number(child.get("fd_count"))
            if rss_value is not None:
                child_rss += rss_value
                rss_samples += 1
            if fd_value is not None:
                child_fd += fd_value
                fd_samples += 1
        if rss_samples:
            self._child_rss.add(elapsed, child_rss)
        if fd_samples:
            self._child_fd.add(elapsed, child_fd)

    def tick(self):
        now = self._clock()
        self._sample_resources(now)
        return self._should_stop_at(now)

    def sample_resources(self, *, force=False):
        """Sample parent and registered children, optionally bypassing 1 Hz pacing."""
        now = self._clock()
        self._sample_resources(now, force=bool(force))

    def _should_stop_at(self, now):
        return bool(
            self.duration_seconds is not None
            and self._measurement_start_at is not None
            and now + 1e-9 >= self._measurement_start_at + self.duration_seconds
        )

    @property
    def should_stop(self):
        return self._should_stop_at(self._clock())

    def record_cleanup(
        self, seconds, children_reaped=True, terminal_restored=True,
    ):
        value = _finite_number(seconds)
        if value is None or value < 0:
            raise ValueError("cleanup timing must be finite and nonnegative")
        self._cleanup = {
            "seconds": float(value),
            "children_reaped": bool(children_reaped),
            "terminal_restored": bool(terminal_restored),
        }

    def register_child(self, process, role):
        """Register a decoder/audio child for best-effort aggregate sampling."""
        pid = getattr(process, "pid", process)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("diagnostic child PID must be a positive integer")
        role = _safe_token(role)
        if role not in ("video", "audio"):
            raise ValueError("diagnostic child role must be video or audio")
        if pid not in self._children and len(self._children) >= MAX_TRACKED_CHILDREN:
            self._child_registration_overflow += 1
            return
        self._children[pid] = role
        self._child_roles_seen.add(role)

    def unregister_child(self, process):
        pid = getattr(process, "pid", process)
        if isinstance(pid, int):
            self._children.pop(pid, None)

    def new_ffmpeg_capture(self):
        self._ffmpeg["captures_created"] += 1
        return FFmpegBenchmarkCapture()

    def consume_ffmpeg_capture(self, capture):
        if not isinstance(capture, FFmpegBenchmarkCapture):
            raise TypeError("expected an FFmpegBenchmarkCapture")
        if capture.consumed:
            return
        metrics = None
        try:
            capture.stream.flush()
            capture.stream.seek(0)
            # FFmpeg -nostats output is small, but scan in bounded chunks so a
            # malformed source description can never require unbounded RAM.
            pending = b""
            totals = {
                "sample_count": 0,
                "user_cpu_seconds": 0.0,
                "system_cpu_seconds": 0.0,
                "real_seconds": 0.0,
                "maximum_rss_bytes": None,
            }
            while True:
                chunk = capture.stream.read(65_536)
                if not chunk:
                    break
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    # Bench records are short. Discard identifying prefixes
                    # from any malformed giant line before parsing its tail.
                    parsed = parse_ffmpeg_benchmark(line[-4_096:])
                    self._merge_ffmpeg_totals(totals, parsed)
                if len(pending) > 4_096:
                    pending = pending[-4_096:]
            parsed = parse_ffmpeg_benchmark(pending)
            self._merge_ffmpeg_totals(totals, parsed)
            metrics = totals
        except (OSError, ValueError):
            metrics = None
        finally:
            capture.consumed = True
            capture.close()
            self._ffmpeg["captures_consumed"] += 1
        if metrics is None or metrics["sample_count"] == 0:
            self._ffmpeg["parse_failures"] += 1
            return
        self._merge_ffmpeg_totals(self._ffmpeg, metrics, target_samples="benchmark_samples")

    @staticmethod
    def _merge_ffmpeg_totals(target, source, target_samples="sample_count"):
        target[target_samples] = target.get(target_samples, 0) + source["sample_count"]
        for key in ("user_cpu_seconds", "system_cpu_seconds", "real_seconds"):
            target[key] = target.get(key, 0.0) + source[key]
        rss = source["maximum_rss_bytes"]
        if rss is not None:
            previous = target.get("maximum_rss_bytes")
            target["maximum_rss_bytes"] = rss if previous is None else max(previous, rss)

    def _measured_elapsed(self, now):
        if self._measurement_start_at is None:
            return 0.0
        elapsed = max(0.0, now - self._measurement_start_at)
        if self.duration_seconds is not None:
            elapsed = min(elapsed, self.duration_seconds)
        return elapsed

    def _timing_value(self, name, field):
        histogram = self._timings.get(name)
        if histogram is None:
            return None
        return histogram.as_dict().get(field)

    @staticmethod
    def _gate(name, value, threshold, comparison, unit, hard, applicable=True):
        if not applicable or value is None:
            return {
                "name": name,
                "status": "not_applicable" if not applicable else "incomplete",
                "hard": hard,
                "value": value,
                "comparison": comparison,
                "threshold": threshold,
                "unit": unit,
            }
        passed = value >= threshold if comparison == ">=" else value <= threshold
        return {
            "name": name,
            "status": "pass" if passed else "fail",
            "hard": hard,
            "value": value,
            "comparison": comparison,
            "threshold": threshold,
            "unit": unit,
        }

    def _evaluate_gates(self, now, exit_reason):
        rules = _PROFILE_GATES[self.profile]
        elapsed = self._measured_elapsed(now)
        presented = self._counts_measured.get("frames_presented", 0)
        dropped = self._counts_measured.get("frames_dropped", 0)
        fps = presented / elapsed if elapsed > 0 else None
        total_frames = presented + dropped
        drop_percent = dropped / total_frames * 100.0 if total_frames else None
        lateness_p95 = self._timing_value("lateness", "p95_ms")
        lateness_p99 = self._timing_value("lateness", "p99_ms")
        freeze_candidates = (
            self._timing_value("loop", "maximum_ms"),
            self._timing_value("sleep_overshoot", "maximum_ms"),
        )
        freeze_ms = max(
            (value for value in freeze_candidates if value is not None),
            default=None,
        )
        performance_hard = rules["performance_hard"]
        target_fps = self.config.get("target_fps")
        if not isinstance(target_fps, (int, float)) or target_fps <= 0:
            target_fps = (
                30.0 if self.profile == "ordinary" else 60.0
            )
        frame_budget_ms = 1_000.0 / target_fps
        minimum_fps = target_fps * rules["minimum_fps_factor"]
        maximum_p95_lateness = (
            frame_budget_ms * rules["maximum_p95_lateness_frames"]
        )
        maximum_p99_lateness = (
            frame_budget_ms * rules["maximum_p99_lateness_frames"]
        )

        results = [
            self._gate("presented_fps", fps, minimum_fps, ">=", "fps", performance_hard),
            self._gate("dropped_frames", drop_percent, rules["maximum_drop_percent"], "<=", "percent", performance_hard),
            self._gate("p95_lateness", lateness_p95, maximum_p95_lateness, "<=", "ms", performance_hard),
            self._gate("p99_lateness", lateness_p99, maximum_p99_lateness, "<=", "ms", performance_hard),
            self._gate("maximum_freeze", freeze_ms, rules["maximum_freeze_ms"], "<=", "ms", performance_hard),
        ]
        requested_complete = (
            self._first_frame_at is not None
            and (
                self.duration_seconds is None
                or elapsed + 1e-9 >= self.duration_seconds
            )
        )
        results.append({
            "name": "measurement_duration",
            "status": (
                "incomplete" if self._first_frame_at is None
                else ("pass" if requested_complete else "fail")
            ),
            "hard": True,
            "value": elapsed,
            "comparison": ">=",
            "threshold": self.duration_seconds,
            "unit": "seconds",
        })

        lifecycle_ok = exit_reason in _NORMAL_EXITS
        results.append({
            "name": "lifecycle",
            "status": "pass" if lifecycle_ok else "fail",
            "hard": True,
            "value": exit_reason,
            "comparison": "normal_exit",
            "threshold": None,
            "unit": None,
        })
        cleanup_limit = 1.5
        cleanup_ok = self._cleanup is not None and all((
            self._cleanup["seconds"] <= cleanup_limit,
            self._cleanup["children_reaped"], self._cleanup["terminal_restored"],
        ))
        results.append({
            "name": "cleanup",
            "status": "incomplete" if self._cleanup is None else ("pass" if cleanup_ok else "fail"),
            "hard": True,
            "value": None if self._cleanup is None else self._cleanup["seconds"],
            "comparison": "<=_and_restored",
            "threshold": cleanup_limit,
            "unit": "seconds",
        })
        corruption = self._counts_all.get("output_corruption", 0)
        results.append(self._gate(
            "output_integrity", corruption, 0, "<=", "events", True,
        ))

        rss = self._rss.as_dict()
        fd = self._fd.as_dict()
        long_enough = elapsed >= 300.0
        rss_supported = bool(
            self._resource_reader.capabilities.get("rss", {}).get("supported")
            and rss["sample_count"]
        )
        fd_supported = bool(
            self._resource_reader.capabilities.get("fd", {}).get("supported")
            and fd["sample_count"]
        )
        results.append(self._gate(
            "rss_growth", rss["maximum_growth"], 32 * 1_024 * 1_024,
            "<=", "bytes", True, applicable=long_enough and rss_supported,
        ))
        results.append(self._gate(
            "rss_slope", rss["median_per_minute_slope"], 1 * 1_024 * 1_024,
            "<=", "bytes_per_minute", True,
            applicable=long_enough and rss_supported,
        ))
        results.append(self._gate(
            "fd_growth", fd["maximum_growth"], 2, "<=", "descriptors",
            True, applicable=long_enough and fd_supported,
        ))
        overhead = self.config.get("diagnostics_overhead_percent")
        results.append(self._gate(
            "diagnostics_overhead", overhead, 2.0, "<=", "percent", True,
            applicable=overhead is not None,
        ))

        failures = [item["name"] for item in results if item["status"] == "fail"]
        hard_failures = [
            item["name"] for item in results
            if item["hard"] and item["status"] == "fail"
        ]
        incomplete = [
            item["name"] for item in results if item["status"] == "incomplete"
        ]
        return {
            "profile": self.profile,
            "complete": not incomplete,
            "hard_pass": not hard_failures,
            "failures": failures,
            "hard_failures": hard_failures,
            "incomplete": incomplete,
            "results": {item["name"]: item for item in results},
        }

    def _diagnose(self, gates):
        diagnoses = []
        target_fps = self.config.get("target_fps")
        if not isinstance(target_fps, (int, float)) or target_fps <= 0:
            target_fps = (
                30.0 if self.profile == "ordinary" else 60.0
            )
        frame_budget_ms = 1_000.0 / target_fps

        style = self._timing_value("style", "p95_ms") or 0.0
        effect = self._timing_value("effect", "p95_ms") or 0.0
        ansi = self._timing_value("ansi", "p95_ms") or 0.0
        write = self._timing_value("terminal_write", "p95_ms") or 0.0
        render_work = style + effect + ansi
        if render_work > frame_budget_ms / 2.0 and write < frame_budget_ms / 4.0:
            diagnoses.append(_diagnosis(
                "visual_pipeline_cpu", "warning",
                {
                    "style_p95_ms": style,
                    "effect_p95_ms": effect,
                    "ansi_p95_ms": ansi,
                    "visual_pipeline_p95_ms": render_work,
                    "terminal_write_p95_ms": write,
                },
                {"render_half_budget_ms": frame_budget_ms / 2.0,
                 "write_quarter_budget_ms": frame_budget_ms / 4.0},
                "reduce dimensions, frame rate, or style/effect complexity",
            ))

        sink = self._matched_sink_write_p95_ms
        terminal_backpressure = bool(
            sink is not None and write > frame_budget_ms / 4.0
            and write >= 2.0 * sink
        )
        if terminal_backpressure:
            diagnoses.append(_diagnosis(
                "terminal_backpressure", "warning",
                {
                    "terminal_write_p95_ms": write,
                    "matched_sink_write_p95_ms": sink,
                },
                {"write_quarter_budget_ms": frame_budget_ms / 4.0,
                 "minimum_sink_multiple": 2.0},
                "use a faster terminal or reduce output dimensions",
            ))

        decoder_max = self._timing_value("decoder_read", "maximum_ms") or 0.0
        reconnects = self._counts_all.get("reconnects", 0)
        decoder_stall = bool(
            decoder_max > 2.0 * frame_budget_ms or reconnects
        )
        if decoder_stall:
            diagnoses.append(_diagnosis(
                "decoder_or_network_stall", "warning",
                {
                    "decoder_read_maximum_ms": decoder_max,
                    "reconnects": reconnects,
                },
                {"maximum_read_ms": 2.0 * frame_budget_ms,
                 "maximum_reconnects": 0},
                "check source availability, network delivery, and FFmpeg",
            ))

        stage_work = sum(
            self._timing_value(name, "p95_ms") or 0.0
            for name in ("style", "effect", "ansi", "status")
        )
        lateness_failed = any(
            gates["results"][name]["status"] == "fail"
            for name in ("p95_lateness", "p99_lateness")
        )
        severity = "error" if self.profile == "ordinary" else "warning"
        if (
            lateness_failed and stage_work < frame_budget_ms / 2.0
            and not terminal_backpressure and not decoder_stall
        ):
            diagnoses.append(_diagnosis(
                "scheduler_jitter", severity,
                {
                    "stage_work_p95_ms": stage_work,
                },
                {"half_frame_budget_ms": frame_budget_ms / 2.0},
                "reduce competing system load and inspect scheduler latency",
            ))
        overload_failed = any(
            gates["results"][name]["status"] == "fail"
            for name in ("presented_fps", "dropped_frames")
        )
        if overload_failed:
            diagnoses.append(_diagnosis(
                "sustained_overload", severity,
                {
                    "stage_work_p95_ms": stage_work,
                    "stage_work_exceeds_half_budget": (
                        stage_work >= frame_budget_ms / 2.0
                    ),
                },
                {"half_frame_budget_ms": frame_budget_ms / 2.0},
                "reduce frame rate, dimensions, or per-frame processing",
            ))

        memory_failures = [
            name for name in ("rss_growth", "rss_slope", "fd_growth")
            if gates["results"][name]["status"] == "fail"
        ]
        if memory_failures:
            diagnoses.append(_diagnosis(
                "possible_memory_growth", "error",
                {"failed_gate_count": len(memory_failures)},
                {"maximum_failed_gates": 0},
                "inspect long-soak allocations, descriptors, and child cleanup",
            ))
        lifecycle_failures = [
            name for name in (
                "measurement_duration", "lifecycle", "cleanup",
                "output_integrity",
            )
            if gates["results"][name]["status"] == "fail"
        ]
        if lifecycle_failures:
            diagnoses.append(_diagnosis(
                "lifecycle_failure", "error",
                {"failed_gate_count": len(lifecycle_failures)},
                {"maximum_failed_gates": 0},
                "inspect exit, terminal restoration, and child reaping",
            ))
        return diagnoses

    def _build_report(self, now, exit_reason, error_type):
        exit_reason = _safe_token(exit_reason, default="error")
        error_type = None if error_type is None else _safe_token(error_type)
        elapsed = self._measured_elapsed(now)
        requested_complete = (
            self.duration_seconds is None
            or elapsed + 1e-9 >= self.duration_seconds
        )
        measurement_complete = bool(self._first_frame_at is not None and requested_complete)
        gates = self._evaluate_gates(now, exit_reason)
        diagnoses = self._diagnose(gates)
        probe_failed = any(
            event["name"] == "probe_failure" for event in self._events
        )
        if (
            self._first_frame_at is None
            and (exit_reason in _EXTERNAL_FAILURE_EXITS or probe_failed)
        ):
            diagnoses.insert(0, _diagnosis(
                "external_source_unavailable", "error",
                {"first_frame_presented": False},
                {"first_frame_presented": True},
                "provide an available source and retry diagnostics",
            ))
        if (
            not diagnoses and measurement_complete
            and gates["complete"] and gates["hard_pass"]
        ):
            diagnoses.append(_diagnosis(
                "stable", "info", {"hard_failures": 0},
                {"maximum_hard_failures": 0}, "none",
            ))

        rss = self._rss.as_dict()
        fd = self._fd.as_dict()
        child_rss = self._child_rss.as_dict()
        child_fd = self._child_fd.as_dict()
        capabilities = {
            key: dict(value)
            for key, value in self._resource_reader.capabilities.items()
        }
        for key, series in (("rss", rss), ("fd", fd)):
            capabilities[key]["available"] = bool(series["sample_count"])
        for key, series in (("child_rss", child_rss), ("child_fd", child_fd)):
            capabilities.setdefault(
                key, {"supported": False, "kind": None, "provider": None}
            )["available"] = bool(series["sample_count"])
        cpu_stats = _histogram_milliseconds_as_percent(self._cpu_percent)
        cpu_elapsed = max(0.0, now - self._created_at)
        parent_cpu = max(0.0, self._process_clock() - self._created_cpu)
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": {
                "exit_reason": exit_reason,
                "error_type": error_type,
                "measurement_complete": measurement_complete,
                "first_frame_presented": self._first_frame_at is not None,
            },
            "config": dict(self.config),
            "source": dict(self.source),
            "environment": dict(self.environment),
            "window": {
                "warmup_seconds": self.warmup_seconds,
                "requested_duration_seconds": self.duration_seconds,
                "startup_to_first_frame_seconds": (
                    None if self._first_frame_at is None
                    else self._first_frame_at - self._created_at
                ),
                "measured_seconds": elapsed,
            },
            "counts": {
                "all": dict(self._counts_all),
                "measured": dict(self._counts_measured),
                "maximum_drop_burst": self._maximum_drop_burst,
                "metric_names_overflow": self._count_names_overflow,
            },
            "timings_ms": {
                "startup": {
                    name: histogram.as_dict()
                    for name, histogram in sorted(self._startup_timings.items())
                },
                "measured": {
                    name: histogram.as_dict()
                    for name, histogram in sorted(self._timings.items())
                },
                "excluded_samples": self._excluded_timing_samples,
                "metric_names_overflow": self._timing_names_overflow,
            },
            "events": {
                "records": list(self._events),
                "total": self._events_total,
                "overflow": self._events_overflow,
                "limit": MAX_EVENTS,
            },
            "resources": {
                "sampling_interval_seconds": 1.0,
                "capabilities": capabilities,
                "failures": (
                    self._resource_failures
                    + int(getattr(self._resource_reader, "failures", 0))
                ),
                "parent": {
                    "cpu": {
                        "total_seconds": parent_cpu,
                        "wall_seconds": cpu_elapsed,
                        "mean_percent": (
                            parent_cpu / cpu_elapsed * 100.0 if cpu_elapsed else None
                        ),
                        "sample_percent": cpu_stats,
                    },
                    "rss_bytes": rss,
                    "fd_count": fd,
                },
                "children": {
                    "active_at_finalize": len(self._children),
                    "roles_seen": sorted(self._child_roles_seen),
                    "registration_overflow": self._child_registration_overflow,
                    "sample_failures": self._child_sample_failures,
                    "aggregate_rss_bytes": child_rss,
                    "aggregate_fd_count": child_fd,
                },
            },
            "ffmpeg": dict(self._ffmpeg),
            "cleanup": None if self._cleanup is None else dict(self._cleanup),
            "gates": gates,
            "diagnoses": diagnoses,
        }

    def finalize(self, exit_reason="normal", error_type=None):
        """Atomically write the report, raising visibly on any report failure."""
        if self._finalized_report is not None:
            return self._finalized_report
        now = self._clock()
        self._sample_resources(now, force=True)
        report = self._build_report(now, exit_reason, error_type)
        temporary_name = None
        try:
            if os.path.lexists(self.path):
                raise FileExistsError
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=".yt-ascii-diagnostics-", suffix=".tmp", delete=False,
            ) as stream:
                temporary_name = stream.name
                json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            # Publishing with a hard link is an atomic create-if-absent on the
            # same filesystem. Unlike exists()+replace(), it cannot overwrite
            # a report another process creates during finalization.
            os.link(temporary_name, self.path)
            os.unlink(temporary_name)
            temporary_name = None
        except (OSError, TypeError, ValueError) as error:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
            raise DiagnosticsReportError(
                "diagnostics report could not be written "
                f"({type(error).__name__})"
            ) from error
        self._finalized_report = report
        return report

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        reason = "normal" if error_type is None else "error"
        type_name = None if error_type is None else error_type.__name__
        self.finalize(reason, type_name)
        return False

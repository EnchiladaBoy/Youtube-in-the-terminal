#!/usr/bin/env python3
"""Deterministic renderer-pivot qualification microbenchmark.

The benchmark keeps every rendered frame in memory and never writes ANSI
frames to the current terminal. Sink/tmux/attached behavior is covered by the
separate short live-smoke process. Run the release-review profiles from the
repository root:

    python benchmarks/benchmark_renderer.py --profile ordinary --check-budgets
    python benchmarks/benchmark_renderer.py --profile stress --check-budgets

Absolute timings vary by host.  The strict local gates intentionally retain
the pre-pivot budgets so a renderer/effect regression is diagnosed rather than
hidden behind the much larger real-time frame deadline.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from functools import lru_cache
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import statistics
import sys
import time

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yt_ascii_effects import (  # noqa: E402
    DEFAULT_EFFECT_TEXT,
    EFFECT_NAMES,
    EFFECT_SPECS,
    EffectProcessor,
)
from yt_ascii_frames import EffectContext, EffectFrame  # noqa: E402
from yt_ascii_renderer import (  # noqa: E402
    ASCII_PALETTE,
    HALF_BLOCK,
    RENDER_BACKENDS,
    AnsiRenderer,
)
from yt_ascii_styles import (  # noqa: E402
    STYLE_NAMES,
    STYLE_SPECS,
    StyleProcessor,
)


SEED = 20260805
GROUPS = 9
ANSI_CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")

# Existing strict local CPU gates.  These are deliberately not derived from
# the 30/60 fps deadlines: meeting a playback deadline is not permission to
# regress a previously fast NumPy/ANSI stage.
CHAR_OR_CELL_RENDER_BUDGET_MS = 2.0
HALF_BLOCK_RENDER_BUDGET_MS = 3.0
STATELESS_EFFECT_BUDGET_MS = 2.0
STATEFUL_EFFECT_BUDGET_MS = 2.5
STYLE_BUDGET_MS = 2.0
COMPOSED_BUDGET_MS = 6.0
NONE_OVERHEAD_BUDGET_PERCENT = 3.0


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    width: int
    height: int
    fps: int
    default_rounds: int

    @property
    def frame_budget_ms(self):
        return 1000.0 / self.fps


PROFILES = {
    "ordinary": Profile("ordinary", 120, 34, 30, 3),
    "stress": Profile("stress", 240, 68, 60, 2),
}


@dataclass(frozen=True, slots=True)
class ColorPath:
    """Requested CLI path and the backend actually used for composition."""

    name: str
    requested_backend: str
    effective_backend: str
    color: bool
    color_path: str
    fallback_reason: str | None = None

    def metadata(self):
        result = asdict(self)
        result["unicode_dependent"] = RENDER_BACKENDS[
            self.effective_backend
        ].unicode_dependent
        result["source_rows_per_cell"] = RENDER_BACKENDS[
            self.effective_backend
        ].source_rows_per_cell
        return result


def _color_paths():
    paths = []
    for requested in RENDER_BACKENDS:
        paths.append(ColorPath(
            name=f"{requested}/color",
            requested_backend=requested,
            effective_backend=requested,
            color=True,
            color_path="truecolor",
        ))
        effective = "chars" if requested in ("cells", "half-block") else requested
        fallback = (
            "uncolored spaces/half-blocks would be blank; use ASCII chars"
            if effective != requested else None
        )
        suffix = "grayscale" if fallback is None else "grayscale-fallback-chars"
        paths.append(ColorPath(
            name=f"{requested}/{suffix}",
            requested_backend=requested,
            effective_backend=effective,
            color=False,
            color_path="grayscale",
            fallback_reason=fallback,
        ))
    return tuple(paths)


COLOR_PATHS = _color_paths()


def _detected_output_path():
    if not sys.stdout.isatty():
        return "sink"
    return "tmux" if os.environ.get("TMUX") else "attached"


def _fixture(rows, cols, phase):
    """Return a deterministic, high-detail RGB fixture with hard boundaries."""
    yy, xx = np.indices((rows, cols), dtype=np.uint32)
    checker = ((xx // 5 + yy // 3 + phase) & 1) * 53
    red = (xx * 17 + yy * 29 + checker + phase * 31) & 255
    green = (xx * 43 + yy * 11 + (xx ^ yy) * 7 + phase * 47) & 255
    blue = (xx * 5 + yy * 61 + (xx // 9) * 73 + phase * 19) & 255
    frame = np.stack((red, green, blue), axis=2).astype(np.uint8)

    # Sharp, asymmetric regions ensure edge, displacement, quantization, and
    # temporal effects all receive meaningful structure at both profiles.
    inset_y = max(1, rows // 7)
    inset_x = max(1, cols // 8)
    mask = (
        (yy >= inset_y)
        & (yy < rows - inset_y)
        & (xx >= inset_x)
        & (xx < cols - inset_x)
        & (((xx + yy + phase * 3) // max(2, cols // 13)) % 3 == 0)
    )
    frame[mask, 0] = np.uint8((241 + phase * 3) & 255)
    frame[mask, 1] = np.uint8((27 + phase * 17) & 255)
    frame[mask, 2] = np.uint8((173 + phase * 11) & 255)
    return np.ascontiguousarray(frame)


@lru_cache(maxsize=None)
def _fixtures(profile, render_mode):
    backend = RENDER_BACKENDS[render_mode]
    rows = profile.height * backend.source_rows_per_cell
    frames = tuple(_fixture(rows, profile.width, phase) for phase in range(3))
    for frame in frames:
        frame.setflags(write=False)
    return frames


def _context(profile, render_mode, sequence):
    return EffectContext(
        video_time=12.375 + sequence / profile.fps,
        frame_sequence=sequence,
        cell_shape=(profile.height, profile.width),
        render_mode=render_mode,
        advance_state=True,
    )


def _renderer(path, seed=SEED):
    return AnsiRenderer(
        ASCII_PALETTE,
        color=path.color,
        render_mode=path.effective_backend,
        rain_chars="01",
        rng=np.random.default_rng(seed),
    )


def _processor(name):
    return EffectProcessor(
        name,
        glyph_mode="ascii",
        speed=1.0,
        seed=SEED,
        effect_text=DEFAULT_EFFECT_TEXT,
    )


def _effect_frame_size(frame):
    total = frame.rgb.nbytes
    if frame.cells is not None:
        total += frame.cells.glyph_indices.nbytes
        total += len(frame.cells.glyphs.encode("utf-8"))
        if frame.cells.fg_rgb is not None:
            total += frame.cells.fg_rgb.nbytes
    return total


def _effect_frame_digest(frame):
    digest = hashlib.sha256()
    digest.update(frame.rgb.tobytes())
    if frame.cells is None:
        digest.update(b"no-cell-plane")
    else:
        digest.update(frame.cells.glyphs.encode("utf-8"))
        digest.update(frame.cells.glyph_indices.tobytes())
        if frame.cells.fg_rgb is not None:
            digest.update(frame.cells.fg_rgb.tobytes())
    return digest.hexdigest()


def _apply_contract_pipeline(style_name, effect_name, path, profile):
    """Apply one repeatable style/effect/backend contract presentation.

    Styles are static frame treatments and therefore receive only RGB. The
    ``trails`` effect is primed with a separately styled frame so its contract
    exercises real temporal state rather than its unchanged first sample.
    """
    frames = _fixtures(profile, path.effective_backend)
    style = StyleProcessor(style_name)
    processor = _processor(effect_name)
    if effect_name == "trails":
        primed = style.apply(frames[0])
        processor.apply(
            primed, _context(profile, path.effective_backend, 0)
        )
        source = frames[1]
        styled = style.apply(source)
        effected = processor.apply(
            styled, _context(profile, path.effective_backend, 1)
        )
    else:
        source = frames[1]
        styled = style.apply(source)
        effected = processor.apply(
            styled, _context(profile, path.effective_backend, 7)
        )
    output = _renderer(path).render(effected.rgb, effected.cells)
    direct_output = _renderer(path).render(source)
    styled_output = _renderer(path).render(styled)
    return source, styled, effected, output, direct_output, styled_output


def _visible_payload(output):
    return ANSI_CSI.sub(b"", output)


def _validate_output(path, output, profile):
    if not output:
        raise AssertionError(f"{path.name}: renderer returned an empty frame")
    if b"\x00" in output:
        raise AssertionError(f"{path.name}: renderer leaked NUL padding")

    if path.effective_backend in ("chars", "cells"):
        try:
            output.decode("ascii")
        except UnicodeDecodeError as error:
            raise AssertionError(
                f"{path.name}: portable backend emitted non-ASCII bytes"
            ) from error

    if path.effective_backend == "cells":
        if b"\x1b[48;2;" not in output:
            raise AssertionError(
                f"{path.name}: cells output lacks ANSI background color"
            )
        payload = _visible_payload(output)
        if set(payload) - {ord(" "), ord("\n")}:
            raise AssertionError(
                f"{path.name}: cells output contains visible glyphs"
            )
        if payload.count(b" ") != profile.width * profile.height:
            raise AssertionError(
                f"{path.name}: cells output does not contain one space per cell"
            )
        if HALF_BLOCK in output:
            raise AssertionError(f"{path.name}: cells output contains half-blocks")
    elif path.effective_backend == "half-block":
        if HALF_BLOCK not in output:
            raise AssertionError(
                f"{path.name}: half-block output lacks its Unicode glyph"
            )
        if b"\x1b[38;2;" not in output or b"\x1b[48;2;" not in output:
            raise AssertionError(
                f"{path.name}: half-block output lacks foreground/background color"
            )

    if not path.color and (
        b"\x1b[38;2;" in output or b"\x1b[48;2;" in output
    ):
        raise AssertionError(
            f"{path.name}: grayscale fallback emitted truecolor sequences"
        )


def _contract_case(style_name, effect_name, effect_spec, path, profile):
    first = _apply_contract_pipeline(
        style_name, effect_name, path, profile
    )
    second = _apply_contract_pipeline(
        style_name, effect_name, path, profile
    )
    source, styled, effected, output, direct_output, styled_output = first
    _, repeated_style, repeated, repeated_output, _, _ = second

    label = f"{path.name}/{style_name}/{effect_name}"
    if styled.shape != source.shape or styled.dtype != np.uint8:
        raise AssertionError(f"{label}: style changed RGB shape or dtype")
    if not np.array_equal(styled, repeated_style):
        raise AssertionError(f"{label}: deterministic style result changed")
    style_changed = not np.array_equal(styled, source)
    if style_name == "classic":
        if styled is not source:
            raise AssertionError(f"{label}: classic style copied the frame")
    elif not style_changed:
        raise AssertionError(f"{label}: style did not materially alter RGB")

    if effected.rgb.shape != styled.shape or effected.rgb.dtype != np.uint8:
        raise AssertionError(
            f"{label}: effect changed RGB shape or dtype"
        )
    if _effect_frame_digest(effected) != _effect_frame_digest(repeated):
        raise AssertionError(f"{label}: deterministic effect result changed")
    if output != repeated_output:
        raise AssertionError(f"{label}: deterministic renderer output changed")

    if effect_spec.kind == "text":
        if effected.cells is None:
            raise AssertionError(f"{label}: text effect lacks a cell plane")
        if effected.cells.glyph_indices.shape != (
            profile.height, profile.width
        ):
            raise AssertionError(f"{label}: text cell plane has the wrong shape")
    elif effected.cells is not None:
        raise AssertionError(f"{label}: non-text effect returned a cell plane")

    effect_changed_rgb = not np.array_equal(effected.rgb, styled)
    effect_changed_output = output != styled_output
    if effect_spec.kind == "graphical" and not effect_changed_rgb:
        raise AssertionError(f"{label}: graphical effect did not alter RGB")
    if effect_name != "none" and not effect_changed_output:
        raise AssertionError(f"{label}: effect did not alter terminal output")
    if effect_name == "none" and (
        effected.rgb is not styled or effect_changed_output
    ):
        raise AssertionError(f"{label}: none copied or altered styled RGB")

    pipeline_changed_output = output != direct_output
    if style_name == "classic" and effect_name == "none":
        if pipeline_changed_output:
            raise AssertionError(f"{label}: identity pipeline altered output")
    elif not pipeline_changed_output:
        raise AssertionError(f"{label}: pipeline did not alter terminal output")

    _validate_output(path, output, profile)
    return {
        "style_sha256": hashlib.sha256(styled.tobytes()).hexdigest(),
        "effect_sha256": _effect_frame_digest(effected),
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "style_changed_rgb": style_changed,
        "effect_changed_rgb": effect_changed_rgb,
        "effect_changed_output": effect_changed_output,
        "pipeline_changed_output": pipeline_changed_output,
        "has_cell_plane": effected.cells is not None,
        "ascii_output": path.effective_backend in ("chars", "cells"),
        "output_bytes": len(output),
    }


def _measure(function, rounds):
    """Return robust per-call wall/CPU timings from fixed-size groups."""
    function()
    gc.collect()
    wall_groups = []
    cpu_groups = []
    gc.disable()
    try:
        for _ in range(GROUPS):
            wall_started = time.perf_counter_ns()
            cpu_started = time.process_time_ns()
            for _ in range(rounds):
                function()
            cpu_groups.append(
                (time.process_time_ns() - cpu_started) / rounds / 1e6
            )
            wall_groups.append(
                (time.perf_counter_ns() - wall_started) / rounds / 1e6
            )
    finally:
        gc.enable()
    wall_groups.sort()
    cpu_groups.sort()
    return {
        "median_ms": statistics.median(wall_groups),
        "worst_group_ms": wall_groups[-1],
        "cpu_median_ms": statistics.median(cpu_groups),
    }


def _measure_overhead(control, candidate, rounds):
    """Compare two very short paths in alternating, paired timing groups."""
    control()
    candidate()
    gc.collect()
    # These paths are sub-millisecond at ordinary geometry, so a long paired
    # sample is necessary for the 3% regression gate to measure dispatch cost
    # instead of scheduler jitter. Repeated qualification trials showed that
    # 60-call groups could swing by more than four percentage points, while
    # 300-call groups made the required default-path result reproducible.
    comparison_rounds = max(300, rounds)
    control_groups = []
    candidate_groups = []
    ratios = []

    def elapsed(function):
        started = time.perf_counter_ns()
        for _ in range(comparison_rounds):
            function()
        return (time.perf_counter_ns() - started) / comparison_rounds / 1e6

    gc.disable()
    try:
        for group in range(GROUPS):
            if group & 1:
                candidate_ms = elapsed(candidate)
                control_ms = elapsed(control)
            else:
                control_ms = elapsed(control)
                candidate_ms = elapsed(candidate)
            control_groups.append(control_ms)
            candidate_groups.append(candidate_ms)
            ratios.append((candidate_ms / control_ms - 1.0) * 100.0)
    finally:
        gc.enable()
    return {
        "comparison_rounds_per_group": comparison_rounds,
        "direct_renderer_median_ms": statistics.median(control_groups),
        "classic_none_median_ms": statistics.median(candidate_groups),
        "overhead_percent": statistics.median(ratios),
    }


def _renderer_call(path, profile):
    frames = _fixtures(profile, path.effective_backend)
    renderer = _renderer(path, SEED + 1)
    sequence = 0

    def run():
        nonlocal sequence
        source = frames[sequence % len(frames)]
        sequence += 1
        return renderer.render(source)

    return run


def _style_call(name, render_mode, profile):
    frames = _fixtures(profile, render_mode)
    processor = StyleProcessor(name)
    sequence = 0

    def run():
        nonlocal sequence
        source = frames[sequence % len(frames)]
        result = processor.apply(source)
        sequence += 1
        return result

    return run


def _effect_call(name, render_mode, profile):
    frames = _fixtures(profile, render_mode)
    processor = _processor(name)
    sequence = 0

    def run():
        nonlocal sequence
        source = frames[sequence % len(frames)]
        result = processor.apply(source, _context(profile, render_mode, sequence))
        sequence += 1
        return result

    return run


def _composed_call(style_name, effect_name, path, profile):
    frames = _fixtures(profile, path.effective_backend)
    processor = _processor(effect_name)
    style = StyleProcessor(style_name)
    renderer = _renderer(path, SEED + 2)
    sequence = 0

    def run():
        nonlocal sequence
        source = style.apply(frames[sequence % len(frames)])
        result = processor.apply(
            source, _context(profile, path.effective_backend, sequence)
        )
        sequence += 1
        return renderer.render(result.rgb, result.cells)

    return run


def _timed_bytes(function, rounds, fps):
    sample = function()
    timing = _measure(function, rounds)
    size = len(sample)
    return {
        **timing,
        "bytes_per_frame": size,
        "mib_per_second_at_target_fps": size * fps / (1024 * 1024),
        "capacity_fps_from_median": (
            1000.0 / timing["median_ms"] if timing["median_ms"] else None
        ),
    }


def _timed_effect(function, rounds):
    sample = function()
    timing = _measure(function, rounds)
    return {
        **timing,
        "result_footprint_bytes": _effect_frame_size(sample),
    }


def _timed_style(function, rounds):
    sample = function()
    timing = _measure(function, rounds)
    return {
        **timing,
        "result_footprint_bytes": sample.nbytes,
    }


def _failure(category, case, observed, limit, unit="ms"):
    return {
        "category": category,
        "case": case,
        "observed": observed,
        "limit": limit,
        "unit": unit,
        "message": f"{category} {case}: {observed:.3f}{unit} >= {limit:.3f}{unit}",
    }


def _validate_registry():
    if tuple(spec.name for spec in STYLE_SPECS) != tuple(STYLE_NAMES):
        raise RuntimeError("style names and capability specs are out of sync")
    if len(STYLE_NAMES) != len(set(STYLE_NAMES)):
        raise RuntimeError("style registry contains duplicate names")
    if tuple(spec.name for spec in EFFECT_SPECS) != tuple(EFFECT_NAMES):
        raise RuntimeError("effect names and capability specs are out of sync")
    if len(EFFECT_NAMES) != len(set(EFFECT_NAMES)):
        raise RuntimeError("effect registry contains duplicate names")
    backend_names = tuple(RENDER_BACKENDS)
    for spec in EFFECT_SPECS:
        if not spec.compatible_renderers:
            raise RuntimeError(f"effect {spec.name!r} has no compatible renderer")
        unknown = set(spec.compatible_renderers) - set(backend_names)
        if unknown:
            raise RuntimeError(
                f"effect {spec.name!r} names unknown renderers: {sorted(unknown)}"
            )


def run_benchmark(profile, rounds):
    _validate_registry()
    specs = {spec.name: spec for spec in EFFECT_SPECS}
    detected_path = _detected_output_path()
    results = {
        "schema_version": 1,
        "benchmark": "renderer-pivot-qualification",
        "profile": {
            "name": profile.name,
            "terminal_width": profile.width,
            "terminal_height": profile.height,
            "target_fps": profile.fps,
            "frame_budget_ms": profile.frame_budget_ms,
        },
        "rounds_per_group": rounds,
        "groups": GROUPS,
        "execution": {
            "measurement_target": "in-memory ANSI composition",
            "stdout_environment_detected": detected_path,
            "ansi_frames_written_to_stdout": False,
            "covers_terminal_io_or_repaint": False,
            "scope": (
                "deterministic CPU qualification only; ANSI frames remain in "
                "memory and live output paths are separate smoke evidence"
            ),
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "style_registry": [
            {
                "name": spec.name,
                "category": spec.category,
            }
            for spec in STYLE_SPECS
        ],
        "effect_registry": [
            {
                "name": spec.name,
                "kind": spec.kind,
                "category": spec.category,
                "compatible_renderers": list(spec.compatible_renderers),
                "stateful": spec.stateful,
            }
            for spec in EFFECT_SPECS
        ],
        "color_paths": [path.metadata() for path in COLOR_PATHS],
        "renderer_cases": {},
        "isolated_style_cases": {},
        "isolated_effect_cases": {},
        "composed_cases": {},
        "default_pipeline_overhead_cases": {},
        "incompatible_cases": [],
    }

    for path in COLOR_PATHS:
        results["renderer_cases"][path.name] = _timed_bytes(
            _renderer_call(path, profile), rounds, profile.fps
        )

    # Static treatment cost also scales with source geometry, so each style is
    # measured at the RGB field size consumed by each effective backend.
    expected_styles = set()
    for backend in RENDER_BACKENDS:
        style_hashes = {}
        source = _fixtures(profile, backend)[1]
        for style_spec in STYLE_SPECS:
            case_name = f"{backend}/{style_spec.name}"
            expected_styles.add(case_name)
            processor = StyleProcessor(style_spec.name)
            first = processor.apply(source)
            repeated = StyleProcessor(style_spec.name).apply(source)
            if first.shape != source.shape or first.dtype != np.uint8:
                raise AssertionError(
                    f"{case_name}: style changed RGB shape or dtype"
                )
            if not np.array_equal(first, repeated):
                raise AssertionError(
                    f"{case_name}: deterministic style result changed"
                )
            if style_spec.name == "classic":
                if first is not source:
                    raise AssertionError(
                        f"{case_name}: classic style copied the frame"
                    )
            elif np.array_equal(first, source):
                raise AssertionError(
                    f"{case_name}: style did not materially alter RGB"
                )
            digest = hashlib.sha256(first.tobytes()).hexdigest()
            if digest in style_hashes:
                raise AssertionError(
                    f"{case_name}: style RGB duplicates "
                    f"{style_hashes[digest]!r}"
                )
            style_hashes[digest] = style_spec.name
            case = _timed_style(
                _style_call(style_spec.name, backend, profile), rounds
            )
            case["rgb_sha256"] = digest
            case["changed_rgb"] = style_spec.name != "classic"
            results["isolated_style_cases"][case_name] = case

    # Isolated effect cost depends on source geometry, hence one case per
    # declared compatible effective backend. Color composition is downstream.
    expected_isolated = set()
    for backend in RENDER_BACKENDS:
        for spec in EFFECT_SPECS:
            if not spec.supports(backend):
                continue
            case_name = f"{backend}/{spec.name}"
            expected_isolated.add(case_name)
            results["isolated_effect_cases"][case_name] = _timed_effect(
                _effect_call(spec.name, backend, profile), rounds
            )

    expected_composed = set()
    for path in COLOR_PATHS:
        for style_name in STYLE_NAMES:
            for spec in EFFECT_SPECS:
                case_name = f"{path.name}/{style_name}/{spec.name}"
                if not spec.supports(path.effective_backend):
                    results["incompatible_cases"].append({
                        "case": case_name,
                        "style": style_name,
                        "effect": spec.name,
                        "requested_backend": path.requested_backend,
                        "effective_backend": path.effective_backend,
                        "handling": "excluded by EffectSpec compatibility",
                    })
                    continue
                expected_composed.add(case_name)
                contract = _contract_case(
                    style_name, spec.name, spec, path, profile
                )
                case = _timed_bytes(
                    _composed_call(style_name, spec.name, path, profile),
                    rounds,
                    profile.fps,
                )
                case["contract"] = contract
                case["frame_budget_utilization_percent"] = (
                    case["median_ms"] / profile.frame_budget_ms * 100.0
                )
                results["composed_cases"][case_name] = case

    if set(results["isolated_style_cases"]) != expected_styles:
        raise RuntimeError("isolated style/backend coverage is incomplete")
    if set(results["isolated_effect_cases"]) != expected_isolated:
        raise RuntimeError("isolated effect/backend coverage is incomplete")
    if set(results["composed_cases"]) != expected_composed:
        raise RuntimeError(
            "composed style/effect/backend/color coverage is incomplete"
        )

    for path in COLOR_PATHS:
        default_case = results["composed_cases"][
            f"{path.name}/classic/none"
        ]
        comparison = _measure_overhead(
            _renderer_call(path, profile),
            _composed_call("classic", "none", path, profile),
            rounds,
        )
        results["default_pipeline_overhead_cases"][path.name] = comparison
        default_case["overhead_vs_direct_renderer_percent"] = comparison[
            "overhead_percent"
        ]

    failures = []
    strict_cpu_gates_applied = profile.name == "stress"
    if strict_cpu_gates_applied:
        for path in COLOR_PATHS:
            case = results["renderer_cases"][path.name]
            limit = (
                HALF_BLOCK_RENDER_BUDGET_MS
                if path.effective_backend == "half-block"
                else CHAR_OR_CELL_RENDER_BUDGET_MS
            )
            if case["median_ms"] >= limit:
                failures.append(_failure(
                    "renderer", path.name, case["median_ms"], limit
                ))

        for case_name, case in results["isolated_style_cases"].items():
            if case["median_ms"] >= STYLE_BUDGET_MS:
                failures.append(_failure(
                    "isolated-style",
                    case_name,
                    case["median_ms"],
                    STYLE_BUDGET_MS,
                ))

        for case_name, case in results["isolated_effect_cases"].items():
            effect_name = case_name.rsplit("/", 1)[1]
            limit = (
                STATEFUL_EFFECT_BUDGET_MS
                if specs[effect_name].stateful
                else STATELESS_EFFECT_BUDGET_MS
            )
            if case["median_ms"] >= limit:
                failures.append(_failure(
                    "isolated-effect", case_name, case["median_ms"], limit
                ))

    for case_name, case in results["composed_cases"].items():
        if strict_cpu_gates_applied and case["median_ms"] >= COMPOSED_BUDGET_MS:
            failures.append(_failure(
                "composed", case_name, case["median_ms"], COMPOSED_BUDGET_MS
            ))
        if case["median_ms"] >= profile.frame_budget_ms:
            failures.append(_failure(
                "frame-budget",
                case_name,
                case["median_ms"],
                profile.frame_budget_ms,
            ))

    # Preserve the historical default-ASCII guard in both profiles. The
    # half-block stress comparison remains gated as an additional check, but
    # cannot stand in for the faster canonical chars/color path because its
    # larger composition cost can conceal fixed pipeline overhead.
    overhead_gate_paths = ["chars/color"]
    if strict_cpu_gates_applied:
        overhead_gate_paths.append("half-block/color")
    for path_name in overhead_gate_paths:
        overhead = results["default_pipeline_overhead_cases"][path_name][
            "overhead_percent"
        ]
        if overhead > NONE_OVERHEAD_BUDGET_PERCENT:
            failures.append(_failure(
                "classic-none-overhead",
                path_name,
                overhead,
                NONE_OVERHEAD_BUDGET_PERCENT,
                "%",
            ))

    results["budgets"] = {
        "char_or_cell_renderer_ms": CHAR_OR_CELL_RENDER_BUDGET_MS,
        "half_block_renderer_ms": HALF_BLOCK_RENDER_BUDGET_MS,
        "style_ms": STYLE_BUDGET_MS,
        "stateless_effect_ms": STATELESS_EFFECT_BUDGET_MS,
        "stateful_effect_ms": STATEFUL_EFFECT_BUDGET_MS,
        "composed_ms": COMPOSED_BUDGET_MS,
        "none_overhead_percent": NONE_OVERHEAD_BUDGET_PERCENT,
        "profile_frame_budget_ms": profile.frame_budget_ms,
        "strict_cpu_gates_applied": strict_cpu_gates_applied,
        "overhead_gate_paths": overhead_gate_paths,
    }
    results["coverage"] = {
        "retained_styles": len(STYLE_NAMES),
        "retained_effects": len(EFFECT_NAMES),
        "render_backends": len(RENDER_BACKENDS),
        "color_paths": len(COLOR_PATHS),
        "renderer_cases": len(results["renderer_cases"]),
        "default_pipeline_overhead_cases": len(
            results["default_pipeline_overhead_cases"]
        ),
        "isolated_style_cases": len(results["isolated_style_cases"]),
        "isolated_effect_cases": len(results["isolated_effect_cases"]),
        "composed_cases": len(results["composed_cases"]),
        "explicitly_incompatible_cases": len(results["incompatible_cases"]),
        "all_contracts_passed": True,
    }
    results["budget_failures"] = failures
    results["passed"] = not failures
    return results


def _print_case_table(title, cases, *, limit=None):
    print(f"\n{title}")
    print(f"{'case':57} {'median':>10} {'CPU':>10} {'worst grp':>10}")
    items = list(cases.items())
    if limit is not None and len(items) > limit:
        items = sorted(
            items, key=lambda item: item[1]["median_ms"], reverse=True
        )[:limit]
        print(f"(slowest {limit} of {len(cases)}; --json contains all cases)")
    for name, values in items:
        print(
            f"{name:57} {values['median_ms']:9.3f}ms "
            f"{values['cpu_median_ms']:9.3f}ms "
            f"{values['worst_group_ms']:9.3f}ms"
        )


def _print_human(results):
    profile = results["profile"]
    environment = results["environment"]
    execution = results["execution"]
    print(
        "renderer pivot benchmark: "
        f"{profile['name']} {profile['terminal_width']}x"
        f"{profile['terminal_height']}@{profile['target_fps']}, "
        f"{results['rounds_per_group']} rounds x {results['groups']} groups"
    )
    print(
        f"Python {environment['python']}, NumPy {environment['numpy']}, "
        f"{environment['platform']}"
    )
    print(
        f"execution: {execution['measurement_target']} "
        f"(stdout detected {execution['stdout_environment_detected']}; "
        "no terminal frame writes)"
    )
    coverage = results["coverage"]
    print(
        "coverage: "
        f"{coverage['retained_styles']} styles, "
        f"{coverage['retained_effects']} effects, "
        f"{coverage['render_backends']} backends, "
        f"{coverage['color_paths']} color/fallback paths, "
        f"{coverage['composed_cases']} composed cases, "
        f"{coverage['explicitly_incompatible_cases']} explicit exclusions"
    )

    _print_case_table("renderer composition", results["renderer_cases"])
    _print_case_table("isolated styles", results["isolated_style_cases"])
    _print_case_table("isolated effects", results["isolated_effect_cases"])
    _print_case_table(
        "style + effect + renderer", results["composed_cases"], limit=30
    )
    print("\nclassic + none overhead vs direct renderer:")
    for path_name in results["budgets"]["overhead_gate_paths"]:
        comparison = results["default_pipeline_overhead_cases"][path_name]
        print(f"  {path_name}: {comparison['overhead_percent']:+.2f}%")
    if results["budget_failures"]:
        print("budget failures:")
        for failure in results["budget_failures"]:
            print(f"  {failure['message']}")
    elif not results["budgets"]["strict_cpu_gates_applied"]:
        print(
            "all deterministic contracts and ordinary frame budgets passed; "
            "strict stage gates apply to the stress profile"
        )
    else:
        print(
            "all renderer, style, effect, composed, and frame-budget gates "
            "passed"
        )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default="ordinary",
        help="qualification workload (default: ordinary)",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        help="calls per timing group (profile-specific default)",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--check-budgets",
        action="store_true",
        help="exit nonzero if a strict CPU or profile frame gate fails",
    )
    args = parser.parse_args(argv)
    profile = PROFILES[args.profile]
    if args.rounds is None:
        args.rounds = profile.default_rounds
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    return args


def main(argv=None):
    args = parse_args(argv)
    results = run_benchmark(PROFILES[args.profile], args.rounds)
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        _print_human(results)

    if args.check_budgets and results["budget_failures"]:
        if args.json:
            for failure in results["budget_failures"]:
                print(f"budget failure: {failure['message']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

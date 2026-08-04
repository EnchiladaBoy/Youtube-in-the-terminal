#!/usr/bin/env python3
"""Deterministic renderer microbenchmark.

Run from the repository root after installing ``requirements.txt``:

    python benchmarks/benchmark_renderer.py
    python benchmarks/benchmark_renderer.py --width 240 --height 68 --json

Absolute timings vary by machine. Renderer ratios and isolated/composed effect
medians are the useful signals.
"""

from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import platform
import statistics
import sys
import time

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yt_ascii_renderer import AnsiRenderer  # noqa: E402
from yt_ascii_effects import (  # noqa: E402
    DEFAULT_EFFECT_TEXT,
    EFFECT_NAMES,
    EffectProcessor,
)
from yt_ascii_frames import EffectContext, EffectFrame  # noqa: E402
from yt_ascii_styles import STYLE_NAMES, StyleProcessor  # noqa: E402


DENSE = " .'`^\",:;Il!i><~+_-?][}{1)(|/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$"
MATRIX = "0123456789ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ:.=+*<>|/"


def legacy_chars(frame, chars=DENSE, color=True):
    palette = np.array(list(chars), dtype="U1")
    luminance = (
        0.299 * frame[:, :, 0]
        + 0.587 * frame[:, :, 1]
        + 0.114 * frame[:, :, 2]
    ).astype(np.int32)
    cells = palette[(luminance * (palette.size - 1)) // 255]
    suffix = "\x1b[0m\x1b[K" if color else "\x1b[K"
    if color:
        red = frame[:, :, 0].astype("U3")
        green = frame[:, :, 1].astype("U3")
        blue = frame[:, :, 2].astype("U3")
        output = np.char.add("\x1b[38;2;", red)
        output = np.char.add(output, ";")
        output = np.char.add(output, green)
        output = np.char.add(output, ";")
        output = np.char.add(output, blue)
        output = np.char.add(output, "m")
        cells = np.char.add(output, cells)
    return "\n".join("".join(row) + suffix for row in cells).encode("utf-8")


def legacy_half(frame):
    top = frame[0::2]
    bottom = frame[1::2]
    channels = [
        top[:, :, 0].astype("U3"),
        top[:, :, 1].astype("U3"),
        top[:, :, 2].astype("U3"),
        bottom[:, :, 0].astype("U3"),
        bottom[:, :, 1].astype("U3"),
        bottom[:, :, 2].astype("U3"),
    ]
    output = np.char.add("\x1b[38;2;", channels[0])
    output = np.char.add(output, ";")
    output = np.char.add(output, channels[1])
    output = np.char.add(output, ";")
    output = np.char.add(output, channels[2])
    output = np.char.add(output, "m\x1b[48;2;")
    output = np.char.add(output, channels[3])
    output = np.char.add(output, ";")
    output = np.char.add(output, channels[4])
    output = np.char.add(output, ";")
    output = np.char.add(output, channels[5])
    output = np.char.add(output, "m▀")
    return "\n".join(
        "".join(row) + "\x1b[0m\x1b[K" for row in output
    ).encode("utf-8")


def measure(function, rounds):
    function()
    gc.collect()
    gc.disable()
    try:
        groups = []
        for _ in range(9):
            started = time.perf_counter_ns()
            for _ in range(rounds):
                function()
            groups.append((time.perf_counter_ns() - started) / rounds / 1e6)
    finally:
        gc.enable()
    groups.sort()
    return {
        "median_ms": statistics.median(groups),
        "worst_group_ms": groups[-1],
    }


def output_size(output):
    """Return the referenced frame/output footprint for benchmark reporting."""
    if isinstance(output, np.ndarray):
        return output.nbytes
    if isinstance(output, EffectFrame):
        total = output.rgb.nbytes
        if output.cells is not None:
            total += output.cells.glyph_indices.nbytes
            if output.cells.fg_rgb is not None:
                total += output.cells.fg_rgb.nbytes
            total += len(output.cells.glyphs.encode("utf-8"))
        return total
    return len(output)


def effect_case(processor, frame, cell_shape, *, renderer=None, style=None):
    """Build a stateful benchmark call matching one playback presentation."""
    sequence = 0

    def run():
        nonlocal sequence
        video_time = 12.375 + sequence / 60.0
        source = style.apply(frame, video_time) if style is not None else frame
        context = EffectContext(
            video_time=video_time,
            frame_sequence=sequence,
            cell_shape=cell_shape,
            requested_pixels=True,
            advance_state=True,
        )
        effected = processor.apply(source, context)
        sequence += 1
        if renderer is None:
            return effected
        return renderer.render(effected.rgb, effected.cells)

    return run


def composed_baseline_case(frame, renderer, style):
    """Build the style-plus-renderer control for ``effect=none`` overhead."""
    sequence = 0

    def run():
        nonlocal sequence
        video_time = 12.375 + sequence / 60.0
        sequence += 1
        return renderer.render(style.apply(frame, video_time))

    return run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=34)
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.width < 1 or args.height < 1 or args.rounds < 1:
        parser.error("width, height and rounds must be positive")

    rng = np.random.default_rng(20260803)
    color_frame = rng.integers(
        0, 256, (args.height, args.width, 3), dtype=np.uint8
    )
    half_frame = rng.integers(
        0, 256, (args.height * 2, args.width, 3), dtype=np.uint8
    )
    grayscale_frame = rng.integers(
        0, 256, (args.height, args.width, 3), dtype=np.uint8
    )

    color = AnsiRenderer(DENSE, rng=np.random.default_rng(1))
    half = AnsiRenderer(DENSE, half_block=True, rng=np.random.default_rng(2))
    gray = AnsiRenderer(DENSE, color=False, rng=np.random.default_rng(3))
    scatter = AnsiRenderer(DENSE, rng=np.random.default_rng(4))
    rain = AnsiRenderer(DENSE, rain_chars=MATRIX, rng=np.random.default_rng(5))
    styles = {name: StyleProcessor(name) for name in STYLE_NAMES}

    cases = [
        ("truecolor/legacy", lambda: legacy_chars(color_frame)),
        ("truecolor/new", lambda: color.render(color_frame)),
        ("half-block/legacy", lambda: legacy_half(half_frame)),
        ("half-block/new", lambda: half.render(half_frame)),
        ("grayscale/legacy-rgb", lambda: legacy_chars(grayscale_frame, color=False)),
        ("grayscale/new-rgb", lambda: gray.render(grayscale_frame)),
        ("scatter/new-50pct", lambda: scatter.render_scatter(color_frame, 0.5)),
        ("rain/new-50pct", lambda: rain.render_rain(color_frame, 0.5)),
    ]
    cases.extend(
        (
            f"style/{name}",
            # Style transforms run before character/half-block composition.
            # Use the 2x-height RGB frame so the default 240x68 benchmark
            # measures the documented 240x136 terminal-pixel workload.
            lambda processor=processor: processor.apply(half_frame, 12.375),
        )
        for name, processor in styles.items()
    )
    cases.append((
        "effect/composed/baseline-no-engine",
        composed_baseline_case(
            half_frame,
            AnsiRenderer(
                DENSE, half_block=True, rng=np.random.default_rng(99)
            ),
            StyleProcessor("duotone"),
        ),
    ))
    for index, name in enumerate(EFFECT_NAMES):
        isolated = EffectProcessor(
            name,
            glyph_mode="ascii",
            speed=1.0,
            seed=20260804,
            effect_text=DEFAULT_EFFECT_TEXT,
        )
        composed = EffectProcessor(
            name,
            glyph_mode="ascii",
            speed=1.0,
            seed=20260804,
            effect_text=DEFAULT_EFFECT_TEXT,
        )
        cases.append((
            f"effect/isolated/{name}",
            effect_case(isolated, half_frame, (args.height, args.width)),
        ))
        cases.append((
            f"effect/composed/{name}",
            effect_case(
                composed,
                half_frame,
                (args.height, args.width),
                renderer=AnsiRenderer(
                    DENSE,
                    half_block=True,
                    rng=np.random.default_rng(100 + index),
                ),
                style=StyleProcessor("duotone"),
            ),
        ))

    results = {
        "width": args.width,
        "height": args.height,
        "rounds": args.rounds,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "cases": {},
    }
    for name, function in cases:
        output = function()
        timing = measure(function, args.rounds)
        output_bytes = output_size(output)
        results["cases"][name] = {
            **timing,
            "bytes_per_frame": output_bytes,
            "mib_per_second_at_60fps": output_bytes * 60 / (1024 * 1024),
        }

    for mode in ("truecolor", "half-block", "grayscale"):
        legacy = results["cases"][f"{mode}/legacy" if mode != "grayscale" else "grayscale/legacy-rgb"]
        new = results["cases"][f"{mode}/new" if mode != "grayscale" else "grayscale/new-rgb"]
        new["speedup_vs_legacy"] = legacy["median_ms"] / new["median_ms"]
    baseline = results["cases"]["effect/composed/baseline-no-engine"]
    none = results["cases"]["effect/composed/none"]
    none["overhead_vs_baseline_percent"] = (
        none["median_ms"] / baseline["median_ms"] - 1.0
    ) * 100.0

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
        return

    print(f"renderer benchmark: {args.width}x{args.height}, {args.rounds} rounds/group")
    environment = results["environment"]
    print(
        f"Python {environment['python']}, NumPy {environment['numpy']}, "
        f"{environment['platform']}"
    )
    print(f"{'case':34} {'median':>10} {'worst grp':>10} {'bytes/frame':>14} {'speedup':>10}")
    for name, values in results["cases"].items():
        speedup = values.get("speedup_vs_legacy")
        speedup_text = f"{speedup:.2f}x" if speedup else "-"
        print(
            f"{name:34} {values['median_ms']:9.3f}ms "
            f"{values['worst_group_ms']:9.3f}ms {values['bytes_per_frame']:14,d} "
            f"{speedup_text:>10}"
        )
    print(
        "effect/composed/none overhead vs baseline: "
        f"{none['overhead_vs_baseline_percent']:+.2f}%"
    )


if __name__ == "__main__":
    main()

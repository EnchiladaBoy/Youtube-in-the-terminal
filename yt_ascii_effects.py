"""Renderer-independent visual effects for RGB terminal video frames.

Graphical effects transform RGB/luminance arrays before terminal composition,
so they remain visible with character, color-cell, and half-block renderers.
Only the deliberately textual ``digital-rain`` and ``terminal-hud`` effects
own a :class:`~yt_ascii_frames.CellPlane`; their compatibility metadata keeps
them out of non-text renderer cycles.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType

import numpy as np

from yt_ascii_backends import (
    RENDER_BACKENDS,
    get_render_backend,
    render_modes_for_frame_kind,
)
from yt_ascii_frames import CellPlane, EffectContext, EffectFrame


DEFAULT_EFFECT_TEXT = "YTASCII"
RENDER_MODES = tuple(RENDER_BACKENDS)

# This is intentionally a compact product registry rather than an inventory of
# every experiment the project has ever shipped.  Aliases below are accepted
# at selection boundaries but never appear while cycling.
EFFECT_NAMES = (
    "none",
    "pixelate",
    "glitch",
    "crt",
    "chromatic-shift",
    "wave",
    "trails",
    "prism",
    "digital-rain",
    "terminal-hud",
)
EFFECT_ALIASES = MappingProxyType(
    {
        "tile-mosaic": "pixelate",
        "wave-lines": "wave",
        "afterimage": "trails",
        "hologram": "crt",
    }
)
TEXT_EFFECT_NAMES = frozenset(("digital-rain", "terminal-hud"))
# Compatibility name for presentation code written before the renderer pivot.
GLYPH_EFFECT_NAMES = TEXT_EFFECT_NAMES
GRAPHICAL_EFFECT_NAMES = frozenset(
    name
    for name in EFFECT_NAMES
    if name != "none" and name not in TEXT_EFFECT_NAMES
)
STATEFUL_EFFECT_NAMES = frozenset(("trails",))


@dataclass(frozen=True)
class EffectSpec:
    """Stable effect metadata shared by playback and presentation layers."""

    name: str
    kind: str
    category: str
    stateful: bool = False

    @property
    def frame_kind(self):
        """Frame contract emitted by this effect before terminal composition."""
        return "text" if self.kind == "text" else "rgb"

    @property
    def compatible_renderers(self):
        """Derive compatibility from backend capabilities, not backend names."""
        return render_modes_for_frame_kind(self.frame_kind)

    @property
    def glyph_owned(self):
        """Compatibility spelling for effects that compose their own text."""
        return self.kind == "text"

    @property
    def pixel_policy(self):
        """Compatibility spelling used by pre-pivot presentation code."""
        return "char-cells" if self.glyph_owned else "native"

    def supports(self, render_mode):
        return render_mode in self.compatible_renderers


EFFECT_SPECS = (
    EffectSpec("none", "identity", "identity"),
    EffectSpec("pixelate", "graphical", "spatial-transform"),
    EffectSpec("glitch", "graphical", "spatial-temporal"),
    EffectSpec("crt", "graphical", "display-simulation"),
    EffectSpec("chromatic-shift", "graphical", "spatial-transform"),
    EffectSpec("wave", "graphical", "spatial-transform"),
    EffectSpec("trails", "graphical", "temporal-transform", True),
    EffectSpec("prism", "graphical", "spatial-transform"),
    EffectSpec("digital-rain", "text", "text-overlay"),
    EffectSpec("terminal-hud", "text", "text-overlay"),
)
_SPEC_BY_NAME = {spec.name: spec for spec in EFFECT_SPECS}


def _validate_render_mode(render_mode):
    return get_render_backend(render_mode).name


def resolve_effect_name(name):
    """Return the canonical effect name, accepting documented legacy aliases."""
    canonical = EFFECT_ALIASES.get(name, name)
    if canonical not in EFFECT_NAMES:
        choices = ", ".join(EFFECT_NAMES)
        raise ValueError(f"unknown effect {name!r}; choose from: {choices}")
    return canonical


def effect_names_for_renderer(render_mode):
    """Return canonical cycle order filtered for a rendering backend."""
    _validate_render_mode(render_mode)
    return tuple(
        spec.name for spec in EFFECT_SPECS if spec.supports(render_mode)
    )


# Every ASCII schema is portable.  The opt-in Unicode variants contain only
# curated, non-combining, single-cell symbols.
_GLYPHS = {
    "digital-rain": {
        "ascii": " 01|",
        "unicode": " 01│",
    },
    "terminal-hud": {
        "ascii": " .:-=+*#@|[]0123456789",
        "unicode": " ·░▒▓█─│┼‹›0123456789",
    },
}
_WAVE = np.array(
    (0, 1, 2, 3, 4, 3, 2, 1, 0, -1, -2, -3, -4, -3, -2, -1),
    dtype=np.int16,
)
_UINT64_MASK = (1 << 64) - 1
_RTL_BIDI_CLASSES = frozenset(("R", "AL", "AN", "RLE", "RLO", "RLI"))


def _hud_schema(glyph_mode, effect_text):
    """Build a bounded HUD schema and the exact label it can display."""
    base = _GLYPHS["terminal-hud"][glyph_mode]
    glyphs = list(base)
    known = set(glyphs)
    label = []
    for glyph in effect_text:
        if glyph not in known:
            if len(glyphs) == 256:
                break
            known.add(glyph)
            glyphs.append(glyph)
        label.append(glyph)
    return "".join(glyphs), "".join(label)


def _validate_effect_text(effect_text, glyph_mode):
    """Keep the historical option safe even though text is no longer tiled."""
    if not isinstance(effect_text, str):
        raise TypeError("effect_text must be a string")
    if not 1 <= len(effect_text) <= 253:
        raise ValueError("effect_text must contain 1 to 253 code points")
    if not any(glyph != " " for glyph in effect_text):
        raise ValueError(
            "effect_text must contain at least one non-space character"
        )
    for position, glyph in enumerate(effect_text, 1):
        category = unicodedata.category(glyph)
        if (
            (glyph.isspace() and glyph != " ")
            or not glyph.isprintable()
            or category in ("Cc", "Cf", "Cs")
            or category.startswith("M")
            or unicodedata.combining(glyph)
            or unicodedata.bidirectional(glyph) in _RTL_BIDI_CLASSES
            or unicodedata.east_asian_width(glyph) in ("W", "F")
        ):
            raise ValueError(
                "effect_text contains an unsupported code point at position "
                f"{position} (U+{ord(glyph):04X}); only safe single-cell "
                "left-to-right characters are allowed"
            )
        if glyph_mode == "ascii" and not 0x20 <= ord(glyph) <= 0x7E:
            raise ValueError(
                "effect_text contains a non-ASCII code point at position "
                f"{position} (U+{ord(glyph):04X}); use glyph_mode='unicode' "
                "for safe Unicode text"
            )


def _validate_frame(frame):
    if not isinstance(frame, np.ndarray):
        raise TypeError("frame must be a NumPy array")
    if frame.dtype != np.uint8:
        raise ValueError("frame must have dtype uint8")
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("frame must have shape (rows, cols, 3)")


def _context_values(frame, context):
    if isinstance(context, EffectContext):
        return (
            context.video_time,
            context.frame_sequence,
            context.cell_shape,
            context.advance_state,
            _validate_render_mode(context.render_mode),
        )
    try:
        video_time = float(context)
    except (TypeError, ValueError) as error:
        raise ValueError("time_seconds must be a finite number") from error
    if not math.isfinite(video_time):
        raise ValueError("time_seconds must be a finite number")
    return video_time, None, frame.shape[:2], True, "chars"


def _effect_tick(video_time, speed, rate):
    """Return a floor tick, including when finite operands overflow float."""
    scaled = video_time * speed * rate
    if math.isfinite(scaled):
        return math.floor(scaled)
    time_numerator, time_denominator = video_time.as_integer_ratio()
    speed_numerator, speed_denominator = speed.as_integer_ratio()
    return (
        time_numerator * speed_numerator * rate
        // (time_denominator * speed_denominator)
    )


def _luminance(rgb):
    """Return deterministic integer Rec.601-like luma in the 0..255 range."""
    values = rgb.astype(np.uint32)
    return (
        values[:, :, 0] * 77
        + values[:, :, 1] * 150
        + values[:, :, 2] * 29
    ) >> 8


def _cell_rgb(frame, shape):
    """Sample RGB into terminal cells, averaging exact integer reductions."""
    rows, cols = shape
    source_rows, source_cols = frame.shape[:2]
    if rows == 0 or cols == 0 or source_rows == 0 or source_cols == 0:
        return np.zeros((rows, cols, 3), dtype=np.uint8)
    if (rows, cols) == (source_rows, source_cols):
        return frame.copy()
    if source_rows % rows == 0 and source_cols % cols == 0:
        row_scale = source_rows // rows
        col_scale = source_cols // cols
        totals = frame.reshape(
            rows, row_scale, cols, col_scale, 3
        ).sum(axis=(1, 3), dtype=np.uint32)
        return (totals // (row_scale * col_scale)).astype(np.uint8)
    row_indices = np.minimum(
        ((np.arange(rows, dtype=np.int64) * 2 + 1) * source_rows) // (2 * rows),
        source_rows - 1,
    )
    col_indices = np.minimum(
        ((np.arange(cols, dtype=np.int64) * 2 + 1) * source_cols) // (2 * cols),
        source_cols - 1,
    )
    return frame[row_indices[:, None], col_indices[None, :]].copy()


def _sobel(luminance):
    rows, cols = luminance.shape
    if rows == 0 or cols == 0:
        empty = np.zeros((rows, cols), dtype=np.int32)
        return empty, empty.copy()
    values = luminance.astype(np.int32, copy=False)
    padded = np.pad(values, ((1, 1), (1, 1)), mode="edge")
    top_left, top, top_right = (
        padded[:-2, :-2], padded[:-2, 1:-1], padded[:-2, 2:]
    )
    left, right = padded[1:-1, :-2], padded[1:-1, 2:]
    bottom_left, bottom, bottom_right = (
        padded[2:, :-2], padded[2:, 1:-1], padded[2:, 2:]
    )
    horizontal = (
        -top_left + top_right - 2 * left + 2 * right
        - bottom_left + bottom_right
    )
    vertical = (
        -top_left - 2 * top - top_right
        + bottom_left + 2 * bottom + bottom_right
    )
    return horizontal, vertical


def _hash_grid(rows, cols, seed):
    if rows == 0 or cols == 0:
        return np.zeros((rows, cols), dtype=np.uint64)
    row_values = np.arange(rows, dtype=np.uint64)[:, None]
    col_values = np.arange(cols, dtype=np.uint64)[None, :]
    values = (
        row_values * np.uint64(0x9E3779B185EBCA87)
        ^ col_values * np.uint64(0xC2B2AE3D27D4EB4F)
        ^ np.uint64(seed & _UINT64_MASK)
    )
    values ^= values >> np.uint64(30)
    values *= np.uint64(0xBF58476D1CE4E5B9)
    values ^= values >> np.uint64(27)
    values *= np.uint64(0x94D049BB133111EB)
    return values ^ (values >> np.uint64(31))


def _pixelate(frame):
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    tile_rows = max(1, min(4, rows))
    tile_cols = max(1, min(6, cols))
    row_starts = np.arange(0, rows, tile_rows)
    col_starts = np.arange(0, cols, tile_cols)
    row_sizes = np.diff(np.append(row_starts, rows))
    col_sizes = np.diff(np.append(col_starts, cols))
    row_sums = np.add.reduceat(frame.astype(np.uint32), row_starts, axis=0)
    tile_sums = np.add.reduceat(row_sums, col_starts, axis=1)
    divisors = row_sizes[:, None] * col_sizes[None, :]
    colors = (tile_sums // divisors[:, :, None]).astype(np.uint8)
    return np.repeat(
        np.repeat(colors, row_sizes, axis=0), col_sizes, axis=1
    )


def _crt(frame, video_time, speed, seed):
    """Simulate scanlines and an RGB shadow mask without terminal glyphs."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    tick = _effect_tick(video_time, speed, 30)
    row_phase = (np.arange(rows) + (tick & 3)) & 3
    scan_weights = np.array((256, 176, 232, 200), dtype=np.uint16)[row_phase]
    values = frame.astype(np.uint16) * scan_weights[:, None, None] // 256
    channel_phase = (
        np.arange(cols, dtype=np.int64) + (seed % 3) + (tick % 3)
    ) % 3
    mask = np.full((cols, 3), 206, dtype=np.uint16)
    mask[np.arange(cols), channel_phase] = 256
    values = values * mask[None, :, :] // 256

    # A cheap elliptical vignette reinforces the display surface while keeping
    # the transform fully vectorized at stress resolution.
    y = np.abs(np.arange(rows, dtype=np.int32) * 2 - (rows - 1))
    x = np.abs(np.arange(cols, dtype=np.int32) * 2 - (cols - 1))
    edge = np.maximum(
        y[:, None] * 256 // max(1, rows),
        x[None, :] * 256 // max(1, cols),
    )
    vignette = np.clip(288 - edge // 3, 192, 256).astype(np.uint16)
    values = values * vignette[:, :, None] // 256
    return np.minimum(values, 255).astype(np.uint8)


def _clamped_shift(channel, amount, axis=1):
    size = channel.shape[axis]
    if size == 0 or amount == 0:
        return channel.copy()
    indices = np.clip(np.arange(size) - amount, 0, size - 1)
    return np.take(channel, indices, axis=axis)


def _glitch(frame, video_time, speed, seed):
    """Displace deterministic row bands with scanlines and channel tearing."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    tick = _effect_tick(video_time, speed, 8)
    separation = min(2, max(0, cols - 1))
    result = frame.copy()
    result[:, :, 0] = _clamped_shift(frame[:, :, 0], separation)
    result[:, :, 2] = _clamped_shift(frame[:, :, 2], -separation)
    for band in range(3):
        value = (
            (tick & _UINT64_MASK)
            ^ (seed & _UINT64_MASK)
            ^ (0x9E3779B97F4A7C15 * (band + 1) & _UINT64_MASK)
        )
        value ^= value >> 30
        value = value * 0xBF58476D1CE4E5B9 & _UINT64_MASK
        value ^= value >> 27
        value = value * 0x94D049BB133111EB & _UINT64_MASK
        value ^= value >> 31
        maximum_height = max(1, rows // 8)
        height = 1 + value % maximum_height
        start = (value >> 8) % max(1, rows - height + 1)
        maximum_shift = max(1, min(8, cols // 12 or 1))
        shift = 1 + (value >> 24) % maximum_shift
        if (value >> 40) & 1:
            shift = -shift
        result[start:start + height] = np.roll(
            result[start:start + height], int(shift), axis=1
        )
    scanlines = result[3::4]
    scanlines[:] = (
        scanlines.astype(np.uint16) * np.uint16(3) // np.uint16(4)
    ).astype(np.uint8)
    return result


def _chromatic_shift(frame, seed):
    """Displace RGB plates in opposing directions with clamped edges."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    amount = min(max(1, cols // 32 + 1), 4)
    if seed & 1:
        amount = -amount
    result = frame.copy()
    result[:, :, 0] = _clamped_shift(frame[:, :, 0], amount)
    result[:, :, 2] = _clamped_shift(frame[:, :, 2], -amount)
    if rows > 1:
        result[:, :, 1] = _clamped_shift(
            frame[:, :, 1], 1 if seed & 2 else -1, axis=0
        )
    return result


def _wave(frame, video_time, speed, seed):
    """Geometrically displace source samples with an analytic wave field."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    tick = _effect_tick(video_time, speed, 12)
    row_grid = np.arange(rows, dtype=np.intp)[:, None]
    col_grid = np.arange(cols, dtype=np.intp)[None, :]
    horizontal = _WAVE[
        (np.arange(rows) * 2 + (tick % 16) + (seed & 15)) & 15
    ].astype(np.intp)
    vertical = _WAVE[
        (np.arange(cols) + ((tick // 2) % 16) + ((seed >> 4) & 15)) & 15
    ].astype(np.intp) // 2
    source_rows = np.clip(row_grid - vertical[None, :], 0, rows - 1)
    source_cols = np.clip(col_grid - horizontal[:, None], 0, cols - 1)
    return frame[source_rows, source_cols].copy()


def _prism(frame, seed):
    """Split channels diagonally and add bright spectral edge accents."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    direction = seed % 4
    row_offset = (-1, 0, 1, 0)[direction]
    col_offset = (0, 1, 0, -1)[direction]
    red = np.roll(frame[:, :, 0], (row_offset, col_offset), axis=(0, 1))
    blue = np.roll(
        frame[:, :, 2], (-row_offset, -col_offset), axis=(0, 1)
    )
    values = frame.astype(np.uint16)
    result = frame.copy()
    result[:, :, 0] = ((values[:, :, 0] + red) // 2).astype(np.uint8)
    result[:, :, 2] = ((values[:, :, 2] + blue) // 2).astype(np.uint8)
    horizontal, vertical = _sobel(_luminance(frame))
    edges = np.abs(horizontal) + np.abs(vertical) >= 96
    result[edges] = np.minimum(
        255, result[edges].astype(np.uint16) + (52, 32, 64)
    ).astype(np.uint8)
    return result


def _digital_rain(frame, shape, glyphs, video_time, speed, seed):
    """Render analytic 12 Hz column heads and fading binary trails."""
    colors = _cell_rgb(frame, shape)
    rows, cols = shape
    indices = np.zeros(shape, dtype=np.uint8)
    if rows == 0 or cols == 0:
        return EffectFrame(frame, CellPlane(indices, glyphs, colors))
    trail_length = min(8, rows)
    period = rows + trail_length
    tick = _effect_tick(video_time, speed, 12)
    column_hashes = _hash_grid(1, cols, seed)[0]
    offsets = (column_hashes % np.uint64(period)).astype(np.int64)
    strides = 1 + (
        (column_hashes >> np.uint64(16)) % np.uint64(3)
    ).astype(np.int64)
    heads = ((tick % period) * strides + offsets) % period
    row_grid = np.arange(rows, dtype=np.int64)[:, None]
    ages = (heads[None, :] - row_grid) % period
    active = ages < trail_length
    luminance = _luminance(colors).astype(np.uint16)
    density = ((trail_length - ages).clip(0, trail_length) * 255) // max(
        1, trail_length
    )
    hashes = (
        _hash_grid(rows, cols, seed ^ (tick & _UINT64_MASK)) >> np.uint64(56)
    ).astype(np.uint16)
    visible = active & (hashes < ((luminance + density) // 2))
    bits = 1 + (
        (_hash_grid(rows, cols, seed + 1) >> np.uint64(63)).astype(np.uint8)
    )
    indices[visible] = bits[visible]
    indices[ages == 0] = 3

    weights = np.where(active, np.maximum(1, trail_length - ages), 0)
    foreground = np.zeros_like(colors)
    green = np.minimum(
        255, colors[:, :, 1].astype(np.uint16) + luminance // 2 + 48
    )
    foreground[:, :, 0] = (
        colors[:, :, 0].astype(np.uint16) * weights // (trail_length * 4)
    ).astype(np.uint8)
    foreground[:, :, 1] = (green * weights // trail_length).astype(np.uint8)
    foreground[:, :, 2] = (
        colors[:, :, 2].astype(np.uint16) * weights // (trail_length * 3)
    ).astype(np.uint8)
    return EffectFrame(frame, CellPlane(indices, glyphs, foreground))


def _terminal_hud(
    frame, shape, glyphs, glyph_mode, label_text, video_time, speed, seed
):
    """Overlay a configured label, instrumentation, and analytic clock."""
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    rows, cols = shape
    if glyph_mode == "ascii":
        tones = " .:-=+*#@"
        horizontal_mark, vertical_mark, reticle_mark = "-", "|", "+"
        left_mark, right_mark = "[", "]"
    else:
        tones = " ·░▒▓█"
        horizontal_mark, vertical_mark, reticle_mark = "─", "│", "┼"
        left_mark, right_mark = "‹", "›"
    lookup = {glyph: index for index, glyph in enumerate(glyphs)}
    tone_indices = np.fromiter(
        (lookup[glyph] for glyph in tones), dtype=np.uint8
    )
    levels = (
        luminance.astype(np.uint16) * len(tone_indices) // 256
    ).astype(np.uint8)
    indices = tone_indices[levels]
    if rows == 0 or cols == 0:
        return EffectFrame(frame, CellPlane(indices, glyphs, colors))

    horizontal_index = lookup[horizontal_mark]
    vertical_index = lookup[vertical_mark]
    reticle_index = lookup[reticle_mark]
    indices[0, :] = horizontal_index
    indices[-1, :] = horizontal_index
    indices[:, 0] = vertical_index
    indices[:, -1] = vertical_index
    indices[0, 0] = reticle_index
    indices[0, -1] = reticle_index
    indices[-1, 0] = reticle_index
    indices[-1, -1] = reticle_index
    if rows >= 3 and cols >= 3:
        center_row, center_col = rows // 2, cols // 2
        indices[center_row, center_col] = reticle_index
        indices[center_row, max(1, center_col - 1)] = horizontal_index
        indices[center_row, min(cols - 2, center_col + 1)] = horizontal_index
        indices[max(1, center_row - 1), center_col] = vertical_index
        indices[min(rows - 2, center_row + 1), center_col] = vertical_index
        for col in range(8 + (seed % 8), cols - 1, 8):
            indices[0, col] = reticle_index
            indices[-1, col] = reticle_index
        for row in range(4 + (seed % 4), rows - 1, 4):
            indices[row, 0] = reticle_index
            indices[row, -1] = reticle_index
    if cols >= 7:
        tick = _effect_tick(video_time, speed, 10)
        readout = left_mark + f"{tick % 100000:05d}" + right_mark
        start = max(0, cols - 7)
        indices[0, start:start + 7] = np.fromiter(
            (lookup[glyph] for glyph in readout), dtype=np.uint8, count=7
        )
    # The bottom label makes --effect-text useful without reviving tiled word
    # art. It occupies one instrumentation field and is clipped structurally,
    # with both delimiters retained whenever two interior cells are available.
    available = max(0, cols - 2)
    if available >= 2:
        body = label_text[:available - 2]
        label = left_mark + body + right_mark
        indices[-1, 1:1 + len(label)] = np.fromiter(
            (lookup[glyph] for glyph in label),
            dtype=np.uint8,
            count=len(label),
        )
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


class EffectProcessor:
    """Select, cycle, and apply deterministic structural visual effects."""

    def __init__(
        self,
        name="none",
        glyph_mode="ascii",
        speed=1.0,
        seed=0,
        effect_text=DEFAULT_EFFECT_TEXT,
    ):
        if glyph_mode not in ("ascii", "unicode"):
            raise ValueError("glyph_mode must be 'ascii' or 'unicode'")
        _validate_effect_text(effect_text, glyph_mode)
        try:
            speed = float(speed)
        except (TypeError, ValueError) as error:
            raise ValueError("speed must be a positive finite number") from error
        if not math.isfinite(speed) or speed <= 0.0:
            raise ValueError("speed must be a positive finite number")
        if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)):
            raise TypeError("seed must be an integer")
        self.glyph_mode = glyph_mode
        self.speed = speed
        self.seed = int(seed)
        self.effect_text = effect_text
        self._name = "none"
        self._trail_accumulator = None
        self._trail_previous = None
        self._trail_time = None
        self._trail_sequence = None
        self.select(name)

    @property
    def name(self):
        return self._name

    @property
    def spec(self):
        return _SPEC_BY_NAME[self._name]

    def _clear_transient_state(self):
        self._trail_accumulator = None
        self._trail_previous = None
        self._trail_time = None
        self._trail_sequence = None

    def ensure_compatible(self, render_mode):
        render_mode = _validate_render_mode(render_mode)
        if not self.spec.supports(render_mode):
            supported = ", ".join(self.spec.compatible_renderers)
            raise ValueError(
                f"effect {self._name!r} is not compatible with render mode "
                f"{render_mode!r}; supported render modes: {supported}"
            )
        return self._name

    def select(self, name, render_mode=None):
        canonical = resolve_effect_name(name)
        if render_mode is not None:
            render_mode = _validate_render_mode(render_mode)
            spec = _SPEC_BY_NAME[canonical]
            if not spec.supports(render_mode):
                supported = ", ".join(spec.compatible_renderers)
                raise ValueError(
                    f"effect {canonical!r} is not compatible with render mode "
                    f"{render_mode!r}; supported render modes: {supported}"
                )
        self._name = canonical
        self._clear_transient_state()
        return self._name

    def cycle(self, render_mode="chars"):
        render_mode = _validate_render_mode(render_mode)
        start = EFFECT_NAMES.index(self._name)
        for offset in range(1, len(EFFECT_NAMES) + 1):
            candidate = EFFECT_NAMES[(start + offset) % len(EFFECT_NAMES)]
            if _SPEC_BY_NAME[candidate].supports(render_mode):
                return self.select(candidate, render_mode)
        raise RuntimeError(f"no effects support render mode {render_mode!r}")

    def reset(self, reason=None):
        """Discard layout/history state while preserving selection."""
        self._clear_transient_state()

    def _trails(self, frame, video_time, sequence, advance_state):
        accumulator = self._trail_accumulator
        previous = self._trail_previous
        if (
            accumulator is None
            or previous is None
            or accumulator.shape != frame.shape
            or previous.shape != frame.shape
        ):
            if not advance_state:
                return frame.copy()
            self._trail_accumulator = np.zeros(frame.shape, dtype=np.float32)
            self._trail_previous = frame.copy()
            self._trail_time = video_time
            self._trail_sequence = sequence
            return frame.copy()
        if not advance_state:
            return np.maximum(frame, np.rint(accumulator).astype(np.uint8))
        non_increasing_sequence = (
            sequence is not None
            and self._trail_sequence is not None
            and sequence <= self._trail_sequence
        )
        if non_increasing_sequence or video_time <= self._trail_time:
            return np.maximum(previous, np.rint(accumulator).astype(np.uint8))
        elapsed = video_time - self._trail_time
        decay = np.float32(0.5 ** (elapsed * self.speed / 0.65))
        np.multiply(accumulator, decay, out=accumulator)
        difference = np.abs(
            frame.astype(np.int16) - previous.astype(np.int16)
        ).max(axis=2).astype(np.float32) / np.float32(255.0)
        trail = previous.astype(np.float32) * difference[:, :, None]
        np.maximum(accumulator, trail, out=accumulator)
        np.copyto(previous, frame)
        self._trail_time = video_time
        self._trail_sequence = sequence
        return np.maximum(frame, np.rint(accumulator).astype(np.uint8))

    def apply(self, frame, context=0.0):
        _validate_frame(frame)
        video_time, sequence, cell_shape, advance_state, render_mode = (
            _context_values(frame, context)
        )
        self.ensure_compatible(render_mode)
        if self._name == "none":
            return EffectFrame(frame)
        if self._name == "pixelate":
            return EffectFrame(_pixelate(frame))
        if self._name == "glitch":
            return EffectFrame(
                _glitch(frame, video_time, self.speed, self.seed)
            )
        if self._name == "crt":
            return EffectFrame(
                _crt(frame, video_time, self.speed, self.seed)
            )
        if self._name == "chromatic-shift":
            return EffectFrame(_chromatic_shift(frame, self.seed))
        if self._name == "wave":
            return EffectFrame(_wave(frame, video_time, self.speed, self.seed))
        if self._name == "trails":
            return EffectFrame(
                self._trails(frame, video_time, sequence, advance_state)
            )
        if self._name == "prism":
            return EffectFrame(_prism(frame, self.seed))
        if self._name == "digital-rain":
            glyphs = _GLYPHS[self._name][self.glyph_mode]
            return _digital_rain(
                frame,
                cell_shape,
                glyphs,
                video_time,
                self.speed,
                self.seed,
            )
        if self._name == "terminal-hud":
            glyphs, label_text = _hud_schema(
                self.glyph_mode, self.effect_text
            )
            return _terminal_hud(
                frame,
                cell_shape,
                glyphs,
                self.glyph_mode,
                label_text,
                video_time,
                self.speed,
                self.seed,
            )
        raise RuntimeError(f"unhandled effect: {self._name}")

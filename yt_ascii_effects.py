"""Terminal-native structural effects applied after RGB video styles.

Glyph effects return a :class:`~yt_ascii_frames.CellPlane`; raster effects
return a transformed RGB frame.  The ``none`` path deliberately preserves the
input object so enabling the effect engine has no cost in the default path.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

import numpy as np

from yt_ascii_frames import CellPlane, EffectContext, EffectFrame


DEFAULT_EFFECT_TEXT = "YTASCII"

EFFECT_NAMES = (
    "none",
    "geometry",
    "contour-glyph",
    "hatch",
    "dotfield",
    "tile-mosaic",
    "wave-lines",
    "voronoi",
    "afterimage",
    "number-field",
    "glyph-grid",
    "vector-field",
    "word-field",
    "inscription",
    "type-echo",
    "error-diffusion",
    "halftone",
    "poster-press",
    "cross-stitch",
    "weave",
    "kilim",
    "quadtree",
    "patchwork",
    "digital-rain",
    "ribbon-scan",
    "pixel-sort",
    "stardust",
    "type-collage",
    "engraving",
    "brickwork",
    "prism",
    "hologram",
    "glass",
    "terminal-hud",
)
GLYPH_EFFECT_NAMES = frozenset(
    (
        "geometry",
        "contour-glyph",
        "hatch",
        "dotfield",
        "number-field",
        "glyph-grid",
        "vector-field",
        "word-field",
        "inscription",
        "type-echo",
        "error-diffusion",
        "halftone",
        "cross-stitch",
        "weave",
        "kilim",
        "digital-rain",
        "stardust",
        "type-collage",
        "engraving",
        "brickwork",
        "terminal-hud",
    )
)
STATEFUL_EFFECT_NAMES = frozenset(("afterimage",))


@dataclass(frozen=True)
class EffectSpec:
    """Stable behavior metadata used by playback and presentation layers."""

    name: str
    glyph_owned: bool
    stateful: bool
    pixel_policy: str


@dataclass(frozen=True)
class _TextSchema:
    """Immutable glyph schema and token indices for a configured text effect."""

    glyphs: str
    token: np.ndarray


EFFECT_SPECS = tuple(
    EffectSpec(
        name,
        glyph_owned=name in GLYPH_EFFECT_NAMES,
        stateful=name in STATEFUL_EFFECT_NAMES,
        pixel_policy=(
            "char-cells" if name in GLYPH_EFFECT_NAMES else "native"
        ),
    )
    for name in EFFECT_NAMES
)
_SPEC_BY_NAME = {spec.name: spec for spec in EFFECT_SPECS}

# Every ASCII schema is strictly portable.  Opt-in Unicode schemas use curated
# non-combining code points and exclude East Asian wide/full-width characters;
# several geometric symbols have locale-dependent ambiguous width.
_GLYPHS = {
    "geometry": {
        "ascii": " .-|/\\oO#",
        "unicode": " ·─│╱╲○●█",
    },
    "contour-glyph": {
        "ascii": " -|/\\",
        "unicode": " ╌╎⟋⟍",
    },
    "hatch": {
        "ascii": " /\\x#",
        "unicode": " ⟋⟍⠶⣿",
    },
    "dotfield": {
        "ascii": " .:*#",
        "unicode": " ⠂⠒⠤⠶",
    },
    "number-field": {
        "ascii": "0123456789",
        "unicode": "⓪①②③④⑤⑥⑦⑧⑨",
    },
    # Grid schemas are ordered as blank, light interior, light/heavy
    # horizontal, light/heavy vertical, light/heavy intersection, and heavy
    # interior.  Keeping one code point per role lets the same index plane use
    # either the portable or richer presentation.
    "glyph-grid": {
        "ascii": " .-=|!+@#",
        "unicode": " ·─━│┃┼╋█",
    },
    # Direction order is east, southeast, south, southwest, west, northwest,
    # north, northeast.  ASCII necessarily shares slash glyphs between
    # opposite diagonal directions; Unicode preserves all eight arrows.
    "vector-field": {
        "ascii": " >\\v/<\\^/",
        "unicode": " →↘↓↙←↖↑↗",
    },
    "error-diffusion": {
        "ascii": " #",
        "unicode": " █",
    },
    "halftone": {
        "ascii": " .oO@",
        "unicode": " ·○●█",
    },
    "cross-stitch": {
        "ascii": " /\\x#",
        "unicode": " ╱╲╳█",
    },
    "weave": {
        "ascii": " -|+#",
        "unicode": " ─│┼█",
    },
    "kilim": {
        "ascii": " .<>/\\#",
        "unicode": " ·◆◇╱╲█",
    },
    "digital-rain": {
        "ascii": " 01|",
        "unicode": " 01│",
    },
    "stardust": {
        "ascii": " .+*",
        "unicode": " ·✦✧",
    },
    "engraving": {
        "ascii": " .-|/\\x#",
        "unicode": " ·─│╱╲╳█",
    },
    "brickwork": {
        "ascii": " .#-|+",
        "unicode": " ·█─│┼",
    },
    # Tone marks are followed by border/reticle, bracket, and digit roles.
    # Repeated '-' and '+' roles are deliberately deduplicated in ASCII.
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
_TEXT_EFFECT_NAMES = frozenset(
    ("word-field", "inscription", "type-echo", "type-collage")
)
_HALFTONE_RANKS = np.array(
    (
        (12, 5, 6, 13),
        (4, 0, 1, 7),
        (11, 3, 2, 8),
        (15, 10, 9, 14),
    ),
    dtype=np.uint8,
)
_RTL_BIDI_CLASSES = frozenset(("R", "AL", "AN", "RLE", "RLO", "RLI"))


def _validate_effect_text(effect_text, glyph_mode):
    """Validate exact, single-cell text without rewriting user input."""
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


def _make_text_schema(effect_text, decorations, token_text):
    glyph_list = [" "]
    glyph_indices = {" ": 0}
    # User characters have stable priority over decorations, making schemas
    # predictable even when the configured text includes punctuation used by
    # the effect itself.
    for glyph in effect_text:
        if glyph not in glyph_indices:
            glyph_indices[glyph] = len(glyph_list)
            glyph_list.append(glyph)
    for glyph in decorations:
        if glyph not in glyph_indices:
            glyph_indices[glyph] = len(glyph_list)
            glyph_list.append(glyph)
    token = np.fromiter(
        (glyph_indices[glyph] for glyph in token_text),
        dtype=np.uint8,
        count=len(token_text),
    )
    token.setflags(write=False)
    return _TextSchema("".join(glyph_list), token)


def _text_schemas(effect_text, glyph_mode):
    if glyph_mode == "ascii":
        word_mark, left_mark, right_mark, echo_mark = ".", "[", "]", ":"
    else:
        word_mark, left_mark, right_mark, echo_mark = "·", "‹", "›", "∶"
    return {
        "word-field": _make_text_schema(
            effect_text,
            word_mark,
            effect_text + word_mark + " ",
        ),
        "inscription": _make_text_schema(
            effect_text,
            left_mark + right_mark,
            left_mark + effect_text + right_mark + " ",
        ),
        "type-echo": _make_text_schema(
            effect_text,
            echo_mark,
            effect_text + echo_mark + " ",
        ),
        "type-collage": _make_text_schema(
            effect_text,
            left_mark + right_mark,
            left_mark + effect_text + right_mark + " ",
        ),
    }


def _effect_tick(video_time, speed, rate):
    """Return a floor tick, including when finite operands overflow float."""
    scaled = video_time * speed * rate
    if math.isfinite(scaled):
        return math.floor(scaled)
    # A finite float has a compact exact integer ratio.  The fallback avoids
    # converting an infinite product while retaining floor semantics for very
    # large positive and negative inputs.
    time_numerator, time_denominator = video_time.as_integer_ratio()
    speed_numerator, speed_denominator = speed.as_integer_ratio()
    return (
        time_numerator * speed_numerator * rate
        // (time_denominator * speed_denominator)
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
        )
    try:
        video_time = float(context)
    except (TypeError, ValueError) as error:
        raise ValueError("time_seconds must be a finite number") from error
    if not math.isfinite(video_time):
        raise ValueError("time_seconds must be a finite number")
    # The numeric shorthand is useful for direct callers and unit tests.  The
    # player supplies EffectContext so half-block input can target character
    # dimensions independently from the RGB frame dimensions.
    return video_time, None, frame.shape[:2], True


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
    if rows == 0 or cols == 0:
        return np.zeros((rows, cols, 3), dtype=np.uint8)
    if source_rows == 0 or source_cols == 0:
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


def _splitmix64(value):
    value = (value + 0x9E3779B97F4A7C15) & _UINT64_MASK
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _UINT64_MASK
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _UINT64_MASK
    return value ^ (value >> 31)


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


def _error_diffusion_order(rows, cols, seed):
    """Return a seeded serpentine cell traversal as flat indices."""
    if rows == 0 or cols == 0:
        return np.zeros(0, dtype=np.intp)
    row_order = np.roll(np.arange(rows, dtype=np.intp), -(seed % rows))
    columns = np.arange(cols, dtype=np.intp)
    order = np.empty(rows * cols, dtype=np.intp)
    for ordinal, row in enumerate(row_order):
        row_columns = columns
        if (ordinal + seed) & 1:
            row_columns = columns[::-1]
        start = ordinal * cols
        order[start:start + cols] = row * cols + row_columns
    order.setflags(write=False)
    return order


def _patchwork_layout(rows, cols, seed):
    """Create a seeded BSP map with at most 32 four-pixel-wide regions."""
    labels = np.zeros((rows, cols), dtype=np.uint8)
    if rows == 0 or cols == 0:
        labels.setflags(write=False)
        return labels

    rectangles = [(0, rows, 0, cols)]
    random_value = seed & _UINT64_MASK
    while len(rectangles) < 32:
        candidates = [
            index
            for index, (top, bottom, left, right) in enumerate(rectangles)
            if bottom - top >= 8 or right - left >= 8
        ]
        if not candidates:
            break
        # Split the largest available patch.  SplitMix is used only for
        # deterministic tie-breaking and cut placement.
        largest_area = max(
            (rectangles[index][1] - rectangles[index][0])
            * (rectangles[index][3] - rectangles[index][2])
            for index in candidates
        )
        largest = [
            index
            for index in candidates
            if (rectangles[index][1] - rectangles[index][0])
            * (rectangles[index][3] - rectangles[index][2]) == largest_area
        ]
        random_value = _splitmix64(random_value)
        index = largest[random_value % len(largest)]
        top, bottom, left, right = rectangles.pop(index)
        height, width = bottom - top, right - left
        can_split_rows = height >= 8
        can_split_cols = width >= 8
        if can_split_rows and can_split_cols:
            split_rows = height > width
            if height == width:
                split_rows = bool((random_value >> 8) & 1)
        else:
            split_rows = can_split_rows

        random_value = _splitmix64(random_value)
        if split_rows:
            span = height - 7
            cut = top + 4 + (random_value % span)
            children = ((top, cut, left, right), (cut, bottom, left, right))
        else:
            span = width - 7
            cut = left + 4 + (random_value % span)
            children = ((top, bottom, left, cut), (top, bottom, cut, right))
        rectangles[index:index] = children

    for label, (top, bottom, left, right) in enumerate(rectangles):
        labels[top:bottom, left:right] = label
    labels.setflags(write=False)
    return labels


def _glass_layout(rows, cols, seed):
    """Cache bounded 4x6 block refraction maps and material accents."""
    if rows == 0 or cols == 0:
        empty_indices = np.zeros((rows, cols), dtype=np.intp)
        empty_frost = np.zeros((rows, cols), dtype=np.int8)
        empty_edges = np.zeros((rows, cols), dtype=bool)
        for value in (empty_indices, empty_frost, empty_edges):
            value.setflags(write=False)
        return empty_indices, empty_indices.copy(), empty_frost, empty_edges

    row_grid, col_grid = np.indices((rows, cols), dtype=np.intp)
    block_rows = (rows + 3) // 4
    block_cols = (cols + 5) // 6
    hashes = _hash_grid(block_rows, block_cols, seed)
    block_row = row_grid // 4
    block_col = col_grid // 6
    selected = hashes[block_row, block_col]
    row_offset = ((selected & np.uint64(7)).astype(np.int16) % 5) - 2
    col_offset = (
        ((selected >> np.uint64(8)) & np.uint64(7)).astype(np.int16) % 5
    ) - 2
    source_rows = np.clip(row_grid + row_offset, 0, rows - 1).astype(np.intp)
    source_cols = np.clip(col_grid + col_offset, 0, cols - 1).astype(np.intp)
    frost = (
        ((selected >> np.uint64(16)) & np.uint64(15)).astype(np.int16) - 7
    ).astype(np.int8)
    edges = ((row_grid % 4) == 0) | ((col_grid % 6) == 0)
    for value in (source_rows, source_cols, frost, edges):
        value.setflags(write=False)
    return source_rows, source_cols, frost, edges


def _geometry(frame, shape, glyphs):
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    horizontal, vertical = _sobel(luminance)
    indices = np.zeros(shape, dtype=np.uint8)
    indices[luminance >= 32] = 1
    mid_tone = (luminance >= 64) & (luminance < 128)
    if mid_tone.any():
        row_grid, col_grid = np.indices(shape, dtype=np.int32)
        fallback = 2 + ((row_grid + col_grid * 2) & 3)
        indices[mid_tone] = fallback[mid_tone]
    indices[luminance >= 128] = 6
    indices[luminance >= 192] = 7
    indices[luminance >= 224] = 8

    abs_horizontal = np.abs(horizontal)
    abs_vertical = np.abs(vertical)
    edges = abs_horizontal + abs_vertical >= 144
    indices[edges & (abs_vertical > abs_horizontal * 2)] = 2
    indices[edges & (abs_horizontal > abs_vertical * 2)] = 3
    diagonal = edges & ~(
        (abs_vertical > abs_horizontal * 2)
        | (abs_horizontal > abs_vertical * 2)
    )
    indices[diagonal & ((horizontal < 0) == (vertical < 0))] = 4
    indices[diagonal & ((horizontal < 0) != (vertical < 0))] = 5
    return EffectFrame(
        frame, CellPlane(indices, glyphs, colors),
    )


def _contour_glyph(frame, shape, glyphs):
    colors = _cell_rgb(frame, shape)
    horizontal, vertical = _sobel(_luminance(colors))
    abs_horizontal = np.abs(horizontal)
    abs_vertical = np.abs(vertical)
    active = abs_horizontal + abs_vertical >= 96
    indices = np.zeros(shape, dtype=np.uint8)
    # Glyph order is blank, horizontal, vertical, rising, falling.  Sobel is
    # the edge normal, so dominant x gradients produce vertical contours.
    indices[active & (abs_vertical > abs_horizontal * 2)] = 1
    indices[active & (abs_horizontal > abs_vertical * 2)] = 2
    diagonal = active & (indices == 0)
    indices[diagonal & ((horizontal < 0) == (vertical < 0))] = 3
    indices[diagonal & ((horizontal < 0) != (vertical < 0))] = 4
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


def _hatch(frame, shape, glyphs, seed):
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    darkness = 255 - luminance
    levels = (darkness * len(glyphs)) >> 8
    rows, cols = shape
    if rows and cols:
        parity = (
            np.arange(rows, dtype=np.int64)[:, None]
            + np.arange(cols, dtype=np.int64)[None, :]
            + (seed & 1)
        ) & 1
        # Alternate the two directional strokes at the first shaded levels;
        # dense tones retain the cross/full glyphs.
        directional = (levels == 1) | (levels == 2)
        levels[directional] = 1 + parity[directional]
    return EffectFrame(
        frame, CellPlane(levels.astype(np.uint8), glyphs, colors)
    )


def _dotfield(frame, shape, glyphs, seed):
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    maximum = len(glyphs) - 1
    scaled = luminance * maximum
    base = scaled // 255
    remainder = scaled % 255
    hashes = (_hash_grid(*shape, seed) >> np.uint64(56)).astype(np.uint16)
    indices = base + (hashes < remainder)
    return EffectFrame(
        frame, CellPlane(indices.astype(np.uint8), glyphs, colors)
    )


def _number_field(frame, shape, glyphs):
    """Label every terminal cell with its exact luminance decile."""
    colors = _cell_rgb(frame, shape)
    indices = ((_luminance(colors) * 10) // 256).astype(np.uint8)
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


def _glyph_grid(frame, shape, glyphs, seed):
    """Rebuild the image as a tone-weighted, seed-shifted cell lattice."""
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    rows, cols = shape
    indices = np.zeros(shape, dtype=np.uint8)

    light = luminance >= 48
    heavy = luminance >= 176
    indices[light] = 1
    indices[heavy] = 8

    # A 4x8 cell lattice is approximately square after terminal cell aspect
    # ratio is taken into account.  Tiny planes disable an axis rather than
    # degenerating into one all-line row or column.
    horizontal = np.zeros(shape, dtype=bool)
    vertical = np.zeros(shape, dtype=bool)
    if rows >= 4:
        row_lines = (np.arange(rows) % 4) == (seed % 4)
        horizontal = np.broadcast_to(row_lines[:, None], shape)
    if cols >= 8:
        col_lines = (np.arange(cols) % 8) == ((seed // 4) % 8)
        vertical = np.broadcast_to(col_lines[None, :], shape)

    light_horizontal = light & horizontal & ~vertical
    light_vertical = light & vertical & ~horizontal
    light_intersection = light & horizontal & vertical
    indices[light_horizontal] = 2
    indices[light_vertical] = 4
    indices[light_intersection] = 6
    indices[heavy & horizontal & ~vertical] = 3
    indices[heavy & vertical & ~horizontal] = 5
    indices[heavy & horizontal & vertical] = 7
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


def _vector_field(frame, shape, glyphs):
    """Show the direction of increasing luminance as terminal data marks."""
    colors = _cell_rgb(frame, shape)
    horizontal, vertical = _sobel(_luminance(colors))
    abs_horizontal = np.abs(horizontal)
    abs_vertical = np.abs(vertical)
    active = abs_horizontal + abs_vertical >= 96
    indices = np.zeros(shape, dtype=np.uint8)

    horizontal_major = active & (abs_horizontal > abs_vertical * 2)
    vertical_major = active & (abs_vertical > abs_horizontal * 2)
    diagonal = active & ~horizontal_major & ~vertical_major

    indices[horizontal_major & (horizontal >= 0)] = 1
    indices[horizontal_major & (horizontal < 0)] = 5
    indices[vertical_major & (vertical >= 0)] = 3
    indices[vertical_major & (vertical < 0)] = 7
    indices[diagonal & (horizontal >= 0) & (vertical >= 0)] = 2
    indices[diagonal & (horizontal < 0) & (vertical >= 0)] = 4
    indices[diagonal & (horizontal < 0) & (vertical < 0)] = 6
    indices[diagonal & (horizontal >= 0) & (vertical < 0)] = 8
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


def _word_field(frame, shape, schema, seed):
    """Repeat configured text through a seeded luminance stipple."""
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    rows, cols = shape
    if rows == 0 or cols == 0:
        indices = np.zeros(shape, dtype=np.uint8)
        return EffectFrame(frame, CellPlane(indices, schema.glyphs, colors))

    token_length = len(schema.token)
    row_grid = np.arange(rows, dtype=np.int64)[:, None]
    col_grid = np.arange(cols, dtype=np.int64)[None, :]
    stagger = max(1, (token_length + 1) // 2)
    positions = (
        col_grid + row_grid * stagger + (seed % token_length)
    ) % token_length
    candidates = schema.token[positions]
    hashes = (_hash_grid(rows, cols, seed) >> np.uint64(56)).astype(np.uint16)
    visible = hashes < luminance
    visible |= luminance == 255
    indices = np.where(visible, candidates, 0).astype(np.uint8)
    return EffectFrame(frame, CellPlane(indices, schema.glyphs, colors))


def _inscription(frame, shape, schema, seed):
    """Write configured text continuously along row-major detected edges."""
    colors = _cell_rgb(frame, shape)
    horizontal, vertical = _sobel(_luminance(colors))
    active = np.abs(horizontal) + np.abs(vertical) >= 96
    indices = np.zeros(shape, dtype=np.uint8)
    flat_active = active.ravel()
    if flat_active.any():
        ordinals = np.cumsum(flat_active, dtype=np.int64) - 1
        active_ordinals = ordinals[flat_active]
        token_length = len(schema.token)
        positions = (active_ordinals + (seed % token_length)) % token_length
        indices.ravel()[flat_active] = schema.token[positions]
    return EffectFrame(frame, CellPlane(indices, schema.glyphs, colors))


def _type_echo(frame, shape, schema, video_time, speed, seed):
    """Render analytic current/prior type bands without retaining history."""
    colors = _cell_rgb(frame, shape)
    rows, cols = shape
    if rows == 0 or cols == 0:
        indices = np.zeros(shape, dtype=np.uint8)
        return EffectFrame(frame, CellPlane(indices, schema.glyphs, colors))

    token_length = len(schema.token)
    period = min(6, rows)
    echo_count = min(3, period)
    tick = _effect_tick(video_time, speed, 6)
    row_grid = np.arange(rows, dtype=np.int64)[:, None]
    col_grid = np.arange(cols, dtype=np.int64)[None, :]
    age = ((tick % period) + (seed % period) - row_grid) % period
    active = np.broadcast_to(age < echo_count, shape)

    # Only small modular residues enter NumPy, so arbitrary-size Python seed
    # and tick values cannot overflow an array scalar conversion.
    positions = (
        col_grid
        - (tick % token_length)
        + age
        + ((seed // period) % token_length)
    ) % token_length
    candidates = schema.token[positions]

    weights = np.zeros(shape, dtype=np.uint16)
    echo_weights = np.array((5, 3, 1), dtype=np.uint16)
    broadcast_age = np.broadcast_to(age, shape)
    weights[active] = echo_weights[broadcast_age[active]]
    luminance = _luminance(colors).astype(np.uint16)
    thresholds = (luminance * weights) // 5
    hashes = (_hash_grid(rows, cols, seed) >> np.uint64(56)).astype(np.uint16)
    visible = active & (hashes < thresholds)
    visible |= active & (broadcast_age == 0) & (luminance == 255)
    indices = np.where(visible, candidates, 0).astype(np.uint8)
    foreground = (
        colors.astype(np.uint16) * weights[:, :, None] // 5
    ).astype(np.uint8)
    return EffectFrame(frame, CellPlane(indices, schema.glyphs, foreground))


def _error_diffusion(frame, shape, glyphs, order, seed):
    """Quantize luma through a seeded serpentine prefix accumulator."""
    colors = _cell_rgb(frame, shape)
    indices = np.zeros(shape, dtype=np.uint8)
    if not indices.size:
        return EffectFrame(frame, CellPlane(indices, glyphs, colors))

    ordered = _luminance(colors).ravel()[order].astype(np.uint64)
    bias = seed % 255
    quotas = (np.cumsum(ordered, dtype=np.uint64) + bias) // 255
    previous = np.empty_like(quotas)
    previous[0] = 0
    previous[1:] = quotas[:-1]
    marks = (quotas > previous).astype(np.uint8)
    indices.ravel()[order] = marks
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


def _halftone(frame, shape, glyphs, seed):
    """Apply a seed-shifted 4x4 clustered-dot coverage screen."""
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    rows, cols = shape
    indices = np.zeros(shape, dtype=np.uint8)
    if rows and cols:
        row_phase = (np.arange(rows) + (seed % 4)) & 3
        col_phase = (np.arange(cols) + ((seed // 4) % 4)) & 3
        ranks = _HALFTONE_RANKS[row_phase[:, None], col_phase[None, :]]
        coverage = (luminance.astype(np.uint16) * 16 + 255) // 256
        active = ranks < coverage
        weights = 1 + (luminance.astype(np.uint16) * 4 // 256)
        indices[active] = weights[active].astype(np.uint8)
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


def _cross_stitch(frame, shape, glyphs, seed):
    """Turn tone bands into alternating diagonal and crossed stitches."""
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    levels = (luminance.astype(np.uint16) * 4 // 256).astype(np.uint8)
    rows, cols = shape
    indices = np.zeros(shape, dtype=np.uint8)
    if rows and cols:
        parity = (
            np.arange(rows, dtype=np.int64)[:, None]
            + np.arange(cols, dtype=np.int64)[None, :]
            + (seed & 1)
        ) & 1
        light = levels == 1
        indices[light] = (1 + parity[light]).astype(np.uint8)
        indices[levels == 2] = 3
        indices[levels == 3] = 4
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


def _weave(frame, shape, glyphs, seed):
    """Render a four-cell over-under warp and weft lattice."""
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    rows, cols = shape
    indices = np.zeros(shape, dtype=np.uint8)
    if rows and cols:
        row_grid = np.arange(rows, dtype=np.int64)[:, None]
        col_grid = np.arange(cols, dtype=np.int64)[None, :]
        row_phase = (row_grid + (seed % 4)) & 3
        col_phase = (col_grid + ((seed // 4) % 4)) & 3
        horizontal = np.broadcast_to(row_phase == 0, shape)
        vertical = np.broadcast_to(col_phase == 0, shape)
        visible = luminance >= 48
        over = ((row_grid // 2 + col_grid // 2 + (seed & 1)) & 1) == 0
        base = np.where(over, 1, 2).astype(np.uint8)
        indices[visible] = base[visible]
        indices[visible & horizontal & vertical] = 3
        indices[(luminance >= 224) & ~(horizontal | vertical)] = 4
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


def _kilim(frame, shape, glyphs, seed):
    """Gate a mirrored 8x16 chevron-and-diamond motif by source tone."""
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    rows, cols = shape
    indices = np.zeros(shape, dtype=np.uint8)
    if rows and cols:
        row_phase = (
            np.arange(rows, dtype=np.int64)[:, None] + (seed % 8)
        ) & 7
        col_phase = (
            np.arange(cols, dtype=np.int64)[None, :] + ((seed // 8) % 16)
        ) & 15
        mirrored_row = np.minimum(row_phase, 7 - row_phase)
        mirrored_col = np.minimum(col_phase, 15 - col_phase)
        distance = np.abs(mirrored_col - mirrored_row * 2)
        candidates = np.ones(shape, dtype=np.uint8)
        left_half = np.broadcast_to(col_phase < 8, shape)
        candidates[(distance == 0) & left_half] = 4
        candidates[(distance == 0) & ~left_half] = 5
        candidates[(distance == 1) & left_half] = 2
        candidates[(distance == 1) & ~left_half] = 3
        candidates[(mirrored_row == 3) & (mirrored_col >= 6)] = 6
        levels = (luminance.astype(np.uint16) * 7 // 256).astype(np.uint8)
        visible = levels > 0
        indices[visible] = np.minimum(candidates[visible], levels[visible])
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


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
    hashes = (_hash_grid(rows, cols, seed ^ (tick & _UINT64_MASK)) >> 56).astype(
        np.uint16
    )
    visible = active & (hashes < ((luminance + density) // 2))
    bits = 1 + (
        (_hash_grid(rows, cols, seed + 1) >> np.uint64(63)).astype(np.uint8)
    )
    indices[visible] = bits[visible]
    indices[ages == 0] = 3

    weights = np.where(active, np.maximum(1, trail_length - ages), 0)
    foreground = np.zeros_like(colors)
    green = np.minimum(
        255,
        colors[:, :, 1].astype(np.uint16) + luminance // 2 + 48,
    )
    foreground[:, :, 0] = (
        colors[:, :, 0].astype(np.uint16) * weights // (trail_length * 4)
    ).astype(np.uint8)
    foreground[:, :, 1] = (
        green * weights // trail_length
    ).astype(np.uint8)
    foreground[:, :, 2] = (
        colors[:, :, 2].astype(np.uint16) * weights // (trail_length * 3)
    ).astype(np.uint8)
    return EffectFrame(frame, CellPlane(indices, glyphs, foreground))


def _stardust(frame, shape, glyphs, video_time, speed, seed):
    """Drift deterministic luma-fed particles and twinkle their roles."""
    colors = _cell_rgb(frame, shape)
    rows, cols = shape
    indices = np.zeros(shape, dtype=np.uint8)
    if rows == 0 or cols == 0:
        return EffectFrame(frame, CellPlane(indices, glyphs, colors))
    tick = _effect_tick(video_time, speed, 8)
    row_shift = (tick + seed) % rows
    col_shift = (tick * 2 + seed // 7) % cols
    base = _hash_grid(rows, cols, seed)
    hashes = np.roll(base, (row_shift, col_shift), axis=(0, 1))
    density = (hashes >> np.uint64(56)).astype(np.uint16)
    luminance = _luminance(colors).astype(np.uint16)
    visible = density < luminance
    phases = (
        ((hashes >> np.uint64(48)).astype(np.uint16) + (tick % 3)) % 3
    ).astype(np.uint8)
    indices[visible] = 1 + phases[visible]
    weights = np.array((0, 2, 4, 5), dtype=np.uint16)[indices]
    foreground = (
        colors.astype(np.uint16) * weights[:, :, None] // 5
    ).astype(np.uint8)
    return EffectFrame(frame, CellPlane(indices, glyphs, foreground))


def _type_collage(frame, shape, schema, video_time, speed, seed):
    """Tile staggered, alternating label bands through a luma mask."""
    colors = _cell_rgb(frame, shape)
    rows, cols = shape
    indices = np.zeros(shape, dtype=np.uint8)
    if rows == 0 or cols == 0:
        return EffectFrame(frame, CellPlane(indices, schema.glyphs, colors))
    tick = _effect_tick(video_time, speed, 3)
    token_length = len(schema.token)
    row_grid = np.arange(rows, dtype=np.int64)[:, None]
    col_grid = np.arange(cols, dtype=np.int64)[None, :]
    band = row_grid & 1
    phase = (
        col_grid
        + (tick % token_length)
        + row_grid * max(1, token_length // 2)
        + (seed % token_length)
    ) % token_length
    forward = schema.token[phase]
    reverse = schema.token[(-phase - 1) % token_length]
    candidates = np.where(band == 0, forward, reverse)
    luminance = _luminance(colors)
    hashes = (_hash_grid(rows, cols, seed) >> np.uint64(56)).astype(np.uint16)
    visible = hashes < luminance
    visible |= luminance == 255
    indices[visible] = candidates[visible]
    return EffectFrame(frame, CellPlane(indices, schema.glyphs, colors))


def _engraving(frame, shape, glyphs, seed):
    """Combine thresholded Sobel contours with directional tone hatching."""
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    horizontal, vertical = _sobel(luminance)
    abs_horizontal = np.abs(horizontal)
    abs_vertical = np.abs(vertical)
    rows, cols = shape
    indices = np.zeros(shape, dtype=np.uint8)
    if rows and cols:
        row_grid = np.arange(rows, dtype=np.int64)[:, None]
        col_grid = np.arange(cols, dtype=np.int64)[None, :]
        darkness = 255 - luminance
        hatch = darkness >= 48
        phase = (row_grid + col_grid + (seed & 3)) & 3
        indices[hatch & (phase == 0)] = 1
        indices[hatch & (phase == 1)] = 2
        indices[hatch & (phase == 2)] = 3
        indices[hatch & (phase == 3)] = 6
        indices[darkness >= 224] = 7

        edges = abs_horizontal + abs_vertical >= 96
        vertical_edge = edges & (abs_horizontal > abs_vertical * 2)
        horizontal_edge = edges & (abs_vertical > abs_horizontal * 2)
        indices[vertical_edge] = 3
        indices[horizontal_edge] = 2
        diagonal = edges & ~(vertical_edge | horizontal_edge)
        indices[diagonal & ((horizontal < 0) == (vertical < 0))] = 4
        indices[diagonal & ((horizontal < 0) != (vertical < 0))] = 5
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


def _brickwork(frame, shape, glyphs, seed):
    """Lay tone-weighted interiors inside staggered 4x8 brick courses."""
    colors = _cell_rgb(frame, shape)
    luminance = _luminance(colors)
    rows, cols = shape
    indices = np.zeros(shape, dtype=np.uint8)
    if rows and cols:
        row_grid = np.arange(rows, dtype=np.int64)[:, None]
        col_grid = np.arange(cols, dtype=np.int64)[None, :]
        row_phase = (row_grid + (seed % 4)) % 4
        course = (row_grid + (seed % 4)) // 4
        stagger = ((course + (seed & 1)) & 1) * 4
        col_phase = (col_grid + stagger + ((seed // 4) % 8)) % 8
        horizontal = np.broadcast_to(row_phase == 0, shape)
        vertical = col_phase == 0
        visible = luminance >= 40
        indices[visible] = 1
        indices[luminance >= 176] = 2
        indices[visible & horizontal & ~vertical] = 3
        indices[visible & vertical & ~horizontal] = 4
        indices[visible & horizontal & vertical] = 5
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


def _terminal_hud(
    frame, shape, glyphs, glyph_mode, video_time, speed, seed
):
    """Overlay terminal instrumentation and a five-digit analytic clock."""
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
    return EffectFrame(frame, CellPlane(indices, glyphs, colors))


def _poster_press(frame, seed):
    """Posterize four channel levels with displaced plates and dark edges."""
    if not frame.size:
        return frame.copy()
    quantized = ((frame.astype(np.uint16) * 3 + 127) // 255 * 85).astype(
        np.uint8
    )
    direction = seed % 4
    row_offset = (-1, 0, 1, 0)[direction]
    col_offset = (0, 1, 0, -1)[direction]
    result = quantized.copy()
    result[:, :, 0] = np.roll(
        quantized[:, :, 0], (row_offset, col_offset), axis=(0, 1)
    )
    result[:, :, 2] = np.roll(
        quantized[:, :, 2], (-row_offset, -col_offset), axis=(0, 1)
    )
    horizontal, vertical = _sobel(_luminance(frame))
    edges = np.abs(horizontal) + np.abs(vertical) >= 96
    result[edges] = (
        result[edges].astype(np.uint16) * 2 // 5
    ).astype(np.uint8)
    return result


def _quadtree(frame):
    """Approximate a frame with variance-limited mean-color quadtree leaves."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    luminance = _luminance(frame).astype(np.int64)
    integral_luma = np.pad(
        np.cumsum(np.cumsum(luminance, axis=0), axis=1), ((1, 0), (1, 0))
    )
    integral_square = np.pad(
        np.cumsum(np.cumsum(luminance * luminance, axis=0), axis=1),
        ((1, 0), (1, 0)),
    )
    values = frame.astype(np.int64)
    integral_rgb = np.pad(
        np.cumsum(np.cumsum(values, axis=0), axis=1),
        ((1, 0), (1, 0), (0, 0)),
    )

    # Evaluate each depth as a vector.  This preserves exact recursive
    # boundaries while avoiding a Python visit for every internal node.
    tops = np.array((0,), dtype=np.intp)
    bottoms = np.array((rows,), dtype=np.intp)
    lefts = np.array((0,), dtype=np.intp)
    rights = np.array((cols,), dtype=np.intp)
    leaf_parts = []
    for depth in range(6):
        heights = bottoms - tops
        widths = rights - lefts
        areas = heights * widths
        totals = (
            integral_luma[bottoms, rights]
            - integral_luma[tops, rights]
            - integral_luma[bottoms, lefts]
            + integral_luma[tops, lefts]
        )
        square_totals = (
            integral_square[bottoms, rights]
            - integral_square[tops, rights]
            - integral_square[bottoms, lefts]
            + integral_square[tops, lefts]
        )
        variable = (
            square_totals * areas - totals * totals
            >= 256 * areas * areas
        )
        split = (
            (depth < 5) & (heights >= 8) & (widths >= 8) & variable
        )
        leaf = ~split
        if leaf.any():
            leaf_parts.append(
                (tops[leaf], bottoms[leaf], lefts[leaf], rights[leaf])
            )
        if not split.any():
            break
        split_tops = tops[split]
        split_bottoms = bottoms[split]
        split_lefts = lefts[split]
        split_rights = rights[split]
        middle_rows = split_tops + (split_bottoms - split_tops) // 2
        middle_cols = split_lefts + (split_rights - split_lefts) // 2
        tops = np.concatenate(
            (split_tops, split_tops, middle_rows, middle_rows)
        )
        bottoms = np.concatenate(
            (middle_rows, middle_rows, split_bottoms, split_bottoms)
        )
        lefts = np.concatenate(
            (split_lefts, middle_cols, split_lefts, middle_cols)
        )
        rights = np.concatenate(
            (middle_cols, split_rights, middle_cols, split_rights)
        )

    leaf_tops = np.concatenate([part[0] for part in leaf_parts])
    leaf_bottoms = np.concatenate([part[1] for part in leaf_parts])
    leaf_lefts = np.concatenate([part[2] for part in leaf_parts])
    leaf_rights = np.concatenate([part[3] for part in leaf_parts])
    leaf_areas = (
        (leaf_bottoms - leaf_tops) * (leaf_rights - leaf_lefts)
    )
    color_totals = (
        integral_rgb[leaf_bottoms, leaf_rights]
        - integral_rgb[leaf_tops, leaf_rights]
        - integral_rgb[leaf_bottoms, leaf_lefts]
        + integral_rgb[leaf_tops, leaf_lefts]
    )
    means = (color_totals // leaf_areas[:, None]).astype(np.uint8)
    label_values = np.arange(len(leaf_tops), dtype=np.int32)
    label_differences = np.zeros((rows + 1, cols + 1), dtype=np.int32)
    np.add.at(label_differences, (leaf_tops, leaf_lefts), label_values)
    np.add.at(label_differences, (leaf_bottoms, leaf_lefts), -label_values)
    np.add.at(label_differences, (leaf_tops, leaf_rights), -label_values)
    np.add.at(label_differences, (leaf_bottoms, leaf_rights), label_values)
    labels = np.cumsum(
        np.cumsum(label_differences, axis=0), axis=1
    )[:-1, :-1].astype(np.uint16)
    result = means[labels]
    boundaries = np.zeros((rows, cols), dtype=bool)
    boundaries[1:] |= labels[1:] != labels[:-1]
    boundaries[:, 1:] |= labels[:, 1:] != labels[:, :-1]
    result[boundaries] = (
        result[boundaries].astype(np.uint16) * 2 // 5
    ).astype(np.uint8)
    return result


def _patchwork(frame, labels):
    """Fill a cached BSP layout with per-frame region means and seams."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    label_count = int(labels.max()) + 1
    flat_labels = labels.ravel()
    counts = np.bincount(flat_labels, minlength=label_count).astype(np.uint64)
    means = np.empty((label_count, 3), dtype=np.uint8)
    for channel in range(3):
        totals = np.bincount(
            flat_labels,
            weights=frame[:, :, channel].ravel(),
            minlength=label_count,
        )
        means[:, channel] = (totals // counts).astype(np.uint8)
    result = means[labels]
    boundaries = np.zeros((rows, cols), dtype=bool)
    boundaries[1:] |= labels[1:] != labels[:-1]
    boundaries[:, 1:] |= labels[:, 1:] != labels[:, :-1]
    result[boundaries] = (
        result[boundaries].astype(np.uint16) * 2 // 5
    ).astype(np.uint8)
    return result


def _ribbon_scan(frame, video_time, speed, seed):
    """Sweep three-pixel warped highlight ribbons at 8 Hz."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    tick = _effect_tick(video_time, speed, 8)
    row_grid = np.arange(rows, dtype=np.int64)[:, None]
    col_grid = np.arange(cols, dtype=np.int64)[None, :]
    wave = _WAVE[(col_grid + (tick % 16) + (seed & 15)) & 15]
    ribbon = ((row_grid - (tick % 12) - wave) % 12) < 3
    values = frame.astype(np.uint16)
    result = (values * 3 // 5).astype(np.uint8)
    highlights = np.minimum(255, values * 6 // 5 + 48).astype(np.uint8)
    result[ribbon] = highlights[ribbon]
    return result


def _pixel_sort(frame, video_time, speed, seed):
    """Stable-sort luma inside time-phased 16-pixel horizontal blocks."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    tick = _effect_tick(video_time, speed, 6)
    phase = (tick + seed) % 16
    rolled = np.roll(frame, -phase, axis=1)
    block_count = (cols + 15) // 16
    padded_cols = block_count * 16
    if padded_cols != cols:
        padded = np.zeros((rows, padded_cols, 3), dtype=np.uint8)
        padded[:, :cols] = rolled
        luma = np.full((rows, padded_cols), 256, dtype=np.uint16)
        luma[:, :cols] = _luminance(rolled)
    else:
        padded = rolled
        luma = _luminance(rolled).astype(np.uint16)
    blocks = padded.reshape(rows, block_count, 16, 3)
    luma_blocks = luma.reshape(rows, block_count, 16)
    order = np.argsort(luma_blocks, axis=2, kind="stable")
    sorted_blocks = np.take_along_axis(blocks, order[:, :, :, None], axis=2)
    sorted_frame = sorted_blocks.reshape(rows, padded_cols, 3)[:, :cols]
    return np.roll(sorted_frame, phase, axis=1).copy()


def _prism(frame, seed):
    """Blend opposing seeded color displacements with bright contours."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    direction = seed % 4
    row_offset = (-1, 0, 1, 0)[direction]
    col_offset = (0, 1, 0, -1)[direction]
    displaced_red = np.roll(
        frame[:, :, 0], (row_offset, col_offset), axis=(0, 1)
    )
    displaced_blue = np.roll(
        frame[:, :, 2], (-row_offset, -col_offset), axis=(0, 1)
    )
    values = frame.astype(np.uint16)
    result = frame.copy()
    result[:, :, 0] = ((values[:, :, 0] + displaced_red) // 2).astype(
        np.uint8
    )
    result[:, :, 2] = ((values[:, :, 2] + displaced_blue) // 2).astype(
        np.uint8
    )
    horizontal, vertical = _sobel(_luminance(frame))
    edges = np.abs(horizontal) + np.abs(vertical) >= 96
    result[edges] = np.minimum(
        255, result[edges].astype(np.uint16) + 40
    ).astype(np.uint8)
    return result


def _hologram(frame, video_time, speed, seed):
    """Map luma to cyan/magenta scanlines with analytic sparkles."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    tick = _effect_tick(video_time, speed, 12)
    luminance = _luminance(frame).astype(np.uint16)
    row_grid = np.arange(rows, dtype=np.int64)[:, None]
    cyan_lines = ((row_grid + (tick & 1)) & 1) == 0
    result = np.empty_like(frame)
    result[:, :, 0] = np.where(cyan_lines, luminance // 5, luminance // 2)
    result[:, :, 1] = np.where(cyan_lines, luminance, luminance // 3)
    result[:, :, 2] = np.minimum(
        255, np.where(cyan_lines, luminance + 32, luminance)
    )
    scanline = ((row_grid + (tick % 4)) & 3) == 0
    result[scanline[:, 0]] = (
        result[scanline[:, 0]].astype(np.uint16) * 3 // 5
    ).astype(np.uint8)
    hashes = (
        _hash_grid(rows, cols, seed ^ (tick & _UINT64_MASK)) >> np.uint64(56)
    ).astype(np.uint16)
    sparkles = hashes < (luminance // 32)
    result[sparkles] = np.array((220, 255, 255), dtype=np.uint8)
    return result


def _glass(frame, layout):
    """Refract cached blocks, then apply frost and pane highlights."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    source_rows, source_cols, frost, edges = layout
    result = frame[source_rows, source_cols].astype(np.int16)
    result += frost[:, :, None]
    np.clip(result, 0, 255, out=result)
    result[edges] = np.minimum(255, result[edges] + 28)
    return result.astype(np.uint8)


def _tile_mosaic(frame):
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


def _wave_lines(frame, video_time, speed, seed):
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    tick = _effect_tick(video_time, speed, 12)
    luminance = _luminance(frame)
    row_grid = np.arange(rows, dtype=np.int32)[:, None]
    col_grid = np.arange(cols, dtype=np.int64)[None, :]
    wave = _WAVE[(col_grid + (tick % 16) + (seed & 15)) & 15].astype(
        np.int32
    )
    # Tone bends the six-row line field locally instead of merely tinting it.
    tone_offset = (luminance // 48).astype(np.int32)
    line_mask = ((row_grid + wave + tone_offset) % 6) == 0

    values = frame.astype(np.uint16)
    result = ((values * 3) // 5).astype(np.uint8)
    highlighted = np.minimum(255, (values * 6) // 5 + 48).astype(np.uint8)
    result[line_mask] = highlighted[line_mask]
    return result


def _voronoi_layout(rows, cols, seed):
    area = rows * cols
    if area == 0:
        return (
            np.zeros((rows, cols), dtype=np.uint16),
            np.zeros(0, dtype=np.intp),
            np.zeros(0, dtype=np.intp),
        )
    site_count = min(area, max(1, min(32, math.isqrt(area) // 3)))
    coordinates = []
    used = set()
    value = seed & _UINT64_MASK
    attempt = 0
    while len(coordinates) < site_count:
        value = _splitmix64(value + attempt)
        flat = value % area
        if flat not in used:
            used.add(flat)
            coordinates.append(divmod(flat, cols))
        attempt += 1
    site_rows = np.fromiter((item[0] for item in coordinates), dtype=np.intp)
    site_cols = np.fromiter((item[1] for item in coordinates), dtype=np.intp)
    row_grid = np.arange(rows, dtype=np.int64)[:, None]
    col_grid = np.arange(cols, dtype=np.int64)[None, :]
    best = np.full((rows, cols), np.iinfo(np.int64).max, dtype=np.int64)
    labels = np.zeros((rows, cols), dtype=np.uint16)
    for index, (site_row, site_col) in enumerate(coordinates):
        distance = (row_grid - site_row) ** 2 + (col_grid - site_col) ** 2
        nearer = distance < best
        best[nearer] = distance[nearer]
        labels[nearer] = index
    return labels, site_rows, site_cols


class EffectProcessor:
    """Select, cycle, and apply deterministic structural terminal effects."""

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
        self._text_schemas = _text_schemas(effect_text, glyph_mode)
        self._name = "none"
        self._cache = {}
        self._afterimage_accumulator = None
        self._afterimage_previous = None
        self._afterimage_time = None
        self._afterimage_sequence = None
        self.select(name)

    @property
    def name(self):
        return self._name

    @property
    def spec(self):
        return _SPEC_BY_NAME[self._name]

    def _clear_transient_state(self):
        self._cache.clear()
        self._afterimage_accumulator = None
        self._afterimage_previous = None
        self._afterimage_time = None
        self._afterimage_sequence = None

    def select(self, name):
        if name not in EFFECT_NAMES:
            choices = ", ".join(EFFECT_NAMES)
            raise ValueError(f"unknown effect {name!r}; choose from: {choices}")
        self._name = name
        self._clear_transient_state()
        return self._name

    def cycle(self):
        index = (EFFECT_NAMES.index(self._name) + 1) % len(EFFECT_NAMES)
        return self.select(EFFECT_NAMES[index])

    def reset(self, reason=None):
        """Discard size/layout/history state while preserving selection.

        ``reason`` is accepted for player diagnostics; state semantics are the
        same for source, seek, resize, style, and selection boundaries.
        """
        self._clear_transient_state()

    def _cached_layout(self, key, factory):
        layout = self._cache.get(key)
        if layout is None:
            # Layout effects retain only the active terminal/frame geometry.
            self._cache.clear()
            layout = factory()
            self._cache[key] = layout
        return layout

    def _error_diffusion(self, frame, shape, glyphs):
        rows, cols = shape
        key = ("error-diffusion", rows, cols, self.seed)
        order = self._cached_layout(
            key, lambda: _error_diffusion_order(rows, cols, self.seed)
        )
        return _error_diffusion(frame, shape, glyphs, order, self.seed)

    def _patchwork(self, frame):
        rows, cols = frame.shape[:2]
        key = ("patchwork", rows, cols, self.seed)
        labels = self._cached_layout(
            key, lambda: _patchwork_layout(rows, cols, self.seed)
        )
        return _patchwork(frame, labels)

    def _glass(self, frame):
        rows, cols = frame.shape[:2]
        key = ("glass", rows, cols, self.seed)
        layout = self._cached_layout(
            key, lambda: _glass_layout(rows, cols, self.seed)
        )
        return _glass(frame, layout)

    def _voronoi(self, frame):
        rows, cols = frame.shape[:2]
        key = (rows, cols, self.seed)
        layout = self._cache.get(key)
        if layout is None:
            # Keep the layout cache bounded to the active presentation size.
            self._cache.clear()
            layout = _voronoi_layout(rows, cols, self.seed)
            self._cache[key] = layout
        labels, site_rows, site_cols = layout
        if not labels.size:
            return frame.copy()
        result = frame[site_rows, site_cols][labels].copy()
        boundaries = np.zeros((rows, cols), dtype=bool)
        boundaries[1:] |= labels[1:] != labels[:-1]
        boundaries[:-1] |= labels[:-1] != labels[1:]
        boundaries[:, 1:] |= labels[:, 1:] != labels[:, :-1]
        boundaries[:, :-1] |= labels[:, :-1] != labels[:, 1:]
        if boundaries.any():
            result[boundaries] = (
                result[boundaries].astype(np.uint16) * 2 // 5
            ).astype(np.uint8)
        return result

    def _afterimage(self, frame, video_time, sequence, advance_state):
        accumulator = self._afterimage_accumulator
        previous = self._afterimage_previous
        if (
            accumulator is None
            or previous is None
            or accumulator.shape != frame.shape
            or previous.shape != frame.shape
        ):
            if not advance_state:
                return frame.copy()
            self._afterimage_accumulator = np.zeros(frame.shape, dtype=np.float32)
            self._afterimage_previous = frame.copy()
            self._afterimage_time = video_time
            self._afterimage_sequence = sequence
            return frame.copy()

        if not advance_state:
            return np.maximum(frame, np.rint(accumulator).astype(np.uint8))
        non_increasing_sequence = (
            sequence is not None
            and self._afterimage_sequence is not None
            and sequence <= self._afterimage_sequence
        )
        if non_increasing_sequence or video_time <= self._afterimage_time:
            return np.maximum(previous, np.rint(accumulator).astype(np.uint8))

        elapsed = video_time - self._afterimage_time
        decay = np.float32(0.5 ** (elapsed * self.speed / 0.65))
        np.multiply(accumulator, decay, out=accumulator)
        difference = np.abs(
            frame.astype(np.int16) - previous.astype(np.int16)
        ).max(axis=2).astype(np.float32) / np.float32(255.0)
        trail = previous.astype(np.float32) * difference[:, :, None]
        np.maximum(accumulator, trail, out=accumulator)
        np.copyto(previous, frame)
        self._afterimage_time = video_time
        self._afterimage_sequence = sequence
        return np.maximum(frame, np.rint(accumulator).astype(np.uint8))

    def apply(self, frame, context=0.0):
        _validate_frame(frame)
        video_time, sequence, cell_shape, advance_state = _context_values(
            frame, context
        )
        if self._name == "none":
            return EffectFrame(frame)
        if self._name in GLYPH_EFFECT_NAMES:
            if self._name in _TEXT_EFFECT_NAMES:
                schema = self._text_schemas[self._name]
                if self._name == "word-field":
                    return _word_field(frame, cell_shape, schema, self.seed)
                if self._name == "inscription":
                    return _inscription(frame, cell_shape, schema, self.seed)
                if self._name == "type-echo":
                    return _type_echo(
                        frame,
                        cell_shape,
                        schema,
                        video_time,
                        self.speed,
                        self.seed,
                    )
                if self._name == "type-collage":
                    return _type_collage(
                        frame,
                        cell_shape,
                        schema,
                        video_time,
                        self.speed,
                        self.seed,
                    )
                raise RuntimeError(f"unhandled text effect: {self._name}")
            glyphs = _GLYPHS[self._name][self.glyph_mode]
            if self._name == "geometry":
                return _geometry(frame, cell_shape, glyphs)
            if self._name == "contour-glyph":
                return _contour_glyph(frame, cell_shape, glyphs)
            if self._name == "hatch":
                return _hatch(frame, cell_shape, glyphs, self.seed)
            if self._name == "dotfield":
                return _dotfield(frame, cell_shape, glyphs, self.seed)
            if self._name == "number-field":
                return _number_field(frame, cell_shape, glyphs)
            if self._name == "glyph-grid":
                return _glyph_grid(frame, cell_shape, glyphs, self.seed)
            if self._name == "vector-field":
                return _vector_field(frame, cell_shape, glyphs)
            if self._name == "error-diffusion":
                return self._error_diffusion(frame, cell_shape, glyphs)
            if self._name == "halftone":
                return _halftone(frame, cell_shape, glyphs, self.seed)
            if self._name == "cross-stitch":
                return _cross_stitch(frame, cell_shape, glyphs, self.seed)
            if self._name == "weave":
                return _weave(frame, cell_shape, glyphs, self.seed)
            if self._name == "kilim":
                return _kilim(frame, cell_shape, glyphs, self.seed)
            if self._name == "digital-rain":
                return _digital_rain(
                    frame,
                    cell_shape,
                    glyphs,
                    video_time,
                    self.speed,
                    self.seed,
                )
            if self._name == "stardust":
                return _stardust(
                    frame,
                    cell_shape,
                    glyphs,
                    video_time,
                    self.speed,
                    self.seed,
                )
            if self._name == "engraving":
                return _engraving(frame, cell_shape, glyphs, self.seed)
            if self._name == "brickwork":
                return _brickwork(frame, cell_shape, glyphs, self.seed)
            if self._name == "terminal-hud":
                return _terminal_hud(
                    frame,
                    cell_shape,
                    glyphs,
                    self.glyph_mode,
                    video_time,
                    self.speed,
                    self.seed,
                )
            raise RuntimeError(f"unhandled glyph effect: {self._name}")
        if self._name == "tile-mosaic":
            return EffectFrame(_tile_mosaic(frame))
        if self._name == "wave-lines":
            return EffectFrame(
                _wave_lines(frame, video_time, self.speed, self.seed)
            )
        if self._name == "voronoi":
            return EffectFrame(self._voronoi(frame))
        if self._name == "afterimage":
            return EffectFrame(
                self._afterimage(frame, video_time, sequence, advance_state)
            )
        if self._name == "poster-press":
            return EffectFrame(_poster_press(frame, self.seed))
        if self._name == "quadtree":
            return EffectFrame(_quadtree(frame))
        if self._name == "patchwork":
            return EffectFrame(self._patchwork(frame))
        if self._name == "ribbon-scan":
            return EffectFrame(
                _ribbon_scan(frame, video_time, self.speed, self.seed)
            )
        if self._name == "pixel-sort":
            return EffectFrame(
                _pixel_sort(frame, video_time, self.speed, self.seed)
            )
        if self._name == "prism":
            return EffectFrame(_prism(frame, self.seed))
        if self._name == "hologram":
            return EffectFrame(
                _hologram(frame, video_time, self.speed, self.seed)
            )
        if self._name == "glass":
            return EffectFrame(self._glass(frame))
        raise RuntimeError(f"unhandled effect: {self._name}")

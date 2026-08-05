"""Static visual treatments applied before structural effects.

Styles own color, contrast, quantization, dithering, and other image
treatments.  They are deterministic functions of the current RGB24 frame and
never own motion or frame history.  ``classic`` deliberately returns its input
object so the default playback path remains zero-copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

import numpy as np


@dataclass(frozen=True, slots=True)
class StyleSpec:
    name: str
    category: str


STYLE_SPECS = (
    StyleSpec("classic", "identity"),
    StyleSpec("bayer", "dither"),
    StyleSpec("posterize", "color-quantization"),
    StyleSpec("contour", "edge-treatment"),
    StyleSpec("edge-glow", "edge-treatment"),
    StyleSpec("ordered-dither", "dither"),
    StyleSpec("error-diffusion", "dither"),
    StyleSpec("duotone", "color-treatment"),
    StyleSpec("two-tone", "color-treatment"),
    StyleSpec("riso", "color-treatment"),
)
STYLE_NAMES = tuple(spec.name for spec in STYLE_SPECS)
STYLE_ALIASES = MappingProxyType({})


_BAYER_4X4 = np.array(
    (
        (0, 8, 2, 10),
        (12, 4, 14, 6),
        (3, 11, 1, 9),
        (15, 7, 13, 5),
    ),
    dtype=np.uint16,
)
_DUOTONE_DARK = np.array((15, 18, 42), dtype=np.float32)
_DUOTONE_LIGHT = np.array((255, 196, 92), dtype=np.float32)
_TWO_TONE_DARK = np.array((10, 18, 44), dtype=np.uint8)
_TWO_TONE_LIGHT = np.array((255, 184, 76), dtype=np.uint8)
_RISO_RED = np.array((0xEE, 0x3D, 0x34), dtype=np.uint8)
_RISO_BLUE = np.array((0x2D, 0x67, 0xD2), dtype=np.uint8)
_RISO_OVERLAP = np.array((0xBC, 0x3F, 0x91), dtype=np.uint8)
_CONTOUR_COLOR = np.array((0x78, 0xF5, 0xE1), dtype=np.float32)


def resolve_style_name(name):
    canonical = STYLE_ALIASES.get(name, name)
    if canonical not in STYLE_NAMES:
        choices = ", ".join(STYLE_NAMES)
        raise ValueError(f"unknown style {name!r}; choose from: {choices}")
    return canonical


def _validate_frame(frame):
    if not isinstance(frame, np.ndarray):
        raise TypeError("frame must be a NumPy array")
    if frame.dtype != np.uint8:
        raise ValueError("frame must have dtype uint8")
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("frame must have shape (rows, cols, 3)")


def _luminance(rgb):
    values = rgb.astype(np.uint32)
    return (
        values[:, :, 0] * 77
        + values[:, :, 1] * 150
        + values[:, :, 2] * 29
    ) >> 8


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


def _bayer(frame):
    """Quantize brightness while scaling channels together to preserve hue."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    brightness = frame.max(axis=2).astype(np.float32)
    thresholds = _threshold_grid(rows, cols).astype(np.float32)
    offset = ((thresholds + 0.5) / 16.0 - 0.5) * 85.0
    levels = np.rint(np.clip(brightness + offset, 0.0, 255.0) / 85.0) * 85.0
    scale = np.divide(
        levels,
        brightness,
        out=np.zeros_like(brightness),
        where=brightness > 0.0,
    )
    result = frame.astype(np.float32) * scale[:, :, None]
    return np.rint(np.clip(result, 0.0, 255.0)).astype(np.uint8)


def _posterize(frame):
    """Quantize RGB to five exact levels without changing channel identity."""
    levels = (frame.astype(np.uint16) * 4 + 127) // 255
    return ((levels * 255 + 2) // 4).astype(np.uint8)


def _edge_glow(frame):
    """Render source contours as cyan/magenta light over a dim image."""
    if not frame.size:
        return frame.copy()
    horizontal, vertical = _sobel(_luminance(frame))
    magnitude = np.abs(horizontal) + np.abs(vertical)
    core = np.clip((magnitude - 56) * 255 // 512, 0, 255).astype(np.uint16)
    padded = np.pad(core, ((1, 1), (1, 1)), mode="edge")
    glow = np.maximum.reduce((
        padded[1:-1, 1:-1],
        padded[:-2, 1:-1],
        padded[2:, 1:-1],
        padded[1:-1, :-2],
        padded[1:-1, 2:],
    ))
    source = frame.astype(np.uint16)
    result = source * 3 // 20
    result[:, :, 0] += core * 3 // 5
    result[:, :, 1] += glow
    result[:, :, 2] += np.maximum(core, glow * 4 // 5)
    return np.minimum(result, 255).astype(np.uint8)


def _contour(frame):
    """Draw a clean cyan Sobel contour without source-image glow."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    horizontal, vertical = _sobel(_luminance(frame))
    magnitude = np.sqrt(
        horizontal.astype(np.float32) ** 2
        + vertical.astype(np.float32) ** 2
    ) / 1020.0
    strength = np.clip((magnitude - 0.08) * 2.2, 0.0, 1.0)
    result = strength[:, :, None] * _CONTOUR_COLOR[None, None, :]
    return np.rint(result).astype(np.uint8)


def _ordered_dither(frame):
    """Apply a four-level Bayer screen independently to every RGB channel."""
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    row_indices = np.arange(rows) & 3
    col_indices = np.arange(cols) & 3
    thresholds = (
        _BAYER_4X4[row_indices[:, None], col_indices[None, :]] * 16 + 8
    )
    scaled = frame.astype(np.uint16) * 3
    base = scaled // 255
    remainder = scaled % 255
    quantized = base + (remainder > thresholds[:, :, None])
    np.minimum(quantized, 3, out=quantized)
    return ((quantized * 255 + 1) // 3).astype(np.uint8)


def _error_diffusion_order(rows, cols):
    """Return a stable serpentine traversal as flat indices."""
    if rows == 0 or cols == 0:
        return np.zeros(0, dtype=np.intp)
    columns = np.arange(cols, dtype=np.intp)
    order = np.empty(rows * cols, dtype=np.intp)
    for row in range(rows):
        row_columns = columns if not (row & 1) else columns[::-1]
        start = row * cols
        order[start:start + cols] = row * cols + row_columns
    order.setflags(write=False)
    return order


def _error_diffusion(frame, order):
    """Diffuse RGB error through a deterministic serpentine accumulator."""
    if not frame.size:
        return frame.copy()
    ordered = frame.reshape(-1, 3)[order].astype(np.uint64)
    quotas = np.cumsum(ordered, axis=0, dtype=np.uint64) // 255
    previous = np.empty_like(quotas)
    previous[0] = 0
    previous[1:] = quotas[:-1]
    marks = (quotas > previous).astype(np.uint8) * 255
    result = np.empty_like(frame.reshape(-1, 3))
    result[order] = marks
    return result.reshape(frame.shape)


def _duotone(frame):
    """Map luminance smoothly through a navy-to-gold color grade."""
    luminance = _luminance(frame).astype(np.float32) / 255.0
    blend = np.clip((luminance - 0.12) / (0.88 - 0.12), 0.0, 1.0)
    blend = blend * blend * (3.0 - 2.0 * blend)
    result = (
        _DUOTONE_DARK[None, None, :] * (1.0 - blend[:, :, None])
        + _DUOTONE_LIGHT[None, None, :] * blend[:, :, None]
    )
    return np.rint(result).astype(np.uint8)


def _two_tone(frame):
    """Threshold luminance into a high-contrast two-color palette."""
    light = _luminance(frame) >= 128
    return np.where(
        light[:, :, None], _TWO_TONE_LIGHT, _TWO_TONE_DARK
    ).astype(np.uint8)


def _threshold_grid(rows, cols, *, row_phase=0, col_phase=0):
    row_indices = (np.arange(rows) + row_phase) % 4
    col_indices = (np.arange(cols) + col_phase) % 4
    return _BAYER_4X4[row_indices[:, None], col_indices[None, :]]


def _riso(frame):
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()
    luminance = _luminance(frame).astype(np.float32)
    red_ink = (frame[:, :, 0].astype(np.float32) * 0.7 + luminance * 0.3) / 255.0
    blue_ink = (frame[:, :, 2].astype(np.float32) * 0.7 + luminance * 0.3) / 255.0
    red_threshold = (_threshold_grid(rows, cols) + 0.5) / 16.0
    blue_threshold = (
        _threshold_grid(rows, cols, row_phase=2, col_phase=1) + 0.5
    ) / 16.0
    red_on = red_ink >= red_threshold
    blue_on = blue_ink >= blue_threshold
    result = np.zeros_like(frame)
    result[red_on] = _RISO_RED
    result[blue_on] = _RISO_BLUE
    result[red_on & blue_on] = _RISO_OVERLAP
    return result


_TRANSFORMS = {
    "bayer": _bayer,
    "posterize": _posterize,
    "contour": _contour,
    "edge-glow": _edge_glow,
    "ordered-dither": _ordered_dither,
    "duotone": _duotone,
    "two-tone": _two_tone,
    "riso": _riso,
}


class StyleProcessor:
    """Select and apply one registered static RGB visual treatment."""

    def __init__(self, name="classic"):
        self._name = resolve_style_name(name)
        self._cache = {}

    @property
    def name(self):
        return self._name

    @property
    def spec(self):
        return STYLE_SPECS[STYLE_NAMES.index(self._name)]

    def select(self, name):
        canonical = resolve_style_name(name)
        self._name = canonical
        self.reset()
        return self._name

    def cycle(self):
        index = (STYLE_NAMES.index(self._name) + 1) % len(STYLE_NAMES)
        return self.select(STYLE_NAMES[index])

    def reset(self):
        self._cache.clear()

    def apply(self, frame):
        _validate_frame(frame)
        if self._name == "classic":
            return frame
        if self._name == "error-diffusion":
            key = frame.shape[:2]
            order = self._cache.get(key)
            if order is None:
                self._cache.clear()
                order = _error_diffusion_order(*key)
                self._cache[key] = order
            return _error_diffusion(frame, order)
        return _TRANSFORMS[self._name](frame)

"""NumPy video styles applied before terminal frame composition.

Every style accepts an RGB24 frame and returns another RGB24 frame with the
same shape.  Transformations never modify their input.  ``classic`` is the
exception to allocating an output: it deliberately returns the input object so
the default playback path remains zero-copy.
"""

from __future__ import annotations

import math

import numpy as np


STYLE_NAMES = (
    "classic",
    "bayer",
    "duotone",
    "riso",
    "contour",
    "glitch",
)

_BAYER_4X4 = np.array(
    (
        (0, 8, 2, 10),
        (12, 4, 14, 6),
        (3, 11, 1, 9),
        (15, 7, 13, 5),
    ),
    dtype=np.float32,
)
_DUOTONE_DARK = np.array((0x0F, 0x12, 0x2A), dtype=np.float32)
_DUOTONE_LIGHT = np.array((0xFF, 0xC4, 0x5C), dtype=np.float32)
_RISO_RED = np.array((0xEE, 0x3D, 0x34), dtype=np.uint8)
_RISO_BLUE = np.array((0x2D, 0x67, 0xD2), dtype=np.uint8)
_RISO_OVERLAP = np.array((0xBC, 0x3F, 0x91), dtype=np.uint8)
_CONTOUR_COLOR = np.array((0x78, 0xF5, 0xE1), dtype=np.float32)


def _validate_frame(frame):
    if not isinstance(frame, np.ndarray):
        raise TypeError("frame must be a NumPy array")
    if frame.dtype != np.uint8:
        raise ValueError("frame must have dtype uint8")
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("frame must have shape (rows, cols, 3)")


def _luminance(frame):
    """Return Rec.601 luminance as float32 in the 0..255 range."""
    return (
        frame[:, :, 0].astype(np.float32) * np.float32(0.299)
        + frame[:, :, 1].astype(np.float32) * np.float32(0.587)
        + frame[:, :, 2].astype(np.float32) * np.float32(0.114)
    )


def _threshold_grid(rows, cols, *, row_phase=0, col_phase=0):
    row_indices = (np.arange(rows) + row_phase) % 4
    col_indices = (np.arange(cols) + col_phase) % 4
    return _BAYER_4X4[row_indices[:, None], col_indices[None, :]]


def _bayer(frame):
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()

    # Quantize HSV-style value (the maximum RGB channel) rather than luma.
    # Mapping the brightest channel exactly onto a target level lets every
    # other channel scale by the same factor without clipping, preserving hue
    # even for saturated colors at the edge of the RGB gamut.
    brightness = frame.max(axis=2).astype(np.float32)
    # One 0..85 threshold interval is spread across the ordered matrix.  The
    # resulting target brightness always belongs to {0, 85, 170, 255}.
    offset = ((_threshold_grid(rows, cols) + 0.5) / 16.0 - 0.5) * 85.0
    levels = np.rint(np.clip(brightness + offset, 0.0, 255.0) / 85.0) * 85.0

    # Scaling all channels equally retains source hue. Black has no defined
    # hue and remains black.
    scale = np.divide(
        levels,
        brightness,
        out=np.zeros_like(brightness),
        where=brightness > 0.0,
    )
    result = frame.astype(np.float32) * scale[:, :, None]
    return np.rint(np.clip(result, 0.0, 255.0)).astype(np.uint8)


def _duotone(frame):
    luminance = _luminance(frame) / 255.0
    blend = np.clip((luminance - 0.12) / (0.88 - 0.12), 0.0, 1.0)
    blend = blend * blend * (3.0 - 2.0 * blend)
    result = (
        _DUOTONE_DARK[None, None, :] * (1.0 - blend[:, :, None])
        + _DUOTONE_LIGHT[None, None, :] * blend[:, :, None]
    )
    return np.rint(result).astype(np.uint8)


def _riso(frame):
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()

    luminance = _luminance(frame)
    # Channel-biased intensity retains recognizable source color while the
    # shared luminance component keeps neutral footage visible on both plates.
    red_ink = (frame[:, :, 0].astype(np.float32) * 0.7 + luminance * 0.3) / 255.0
    blue_ink = (frame[:, :, 2].astype(np.float32) * 0.7 + luminance * 0.3) / 255.0
    red_threshold = (_threshold_grid(rows, cols) + 0.5) / 16.0
    # Offset the second screen like two deliberately misregistered ink plates.
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


def _contour(frame):
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()

    luminance = _luminance(frame)
    padded = np.pad(luminance, ((1, 1), (1, 1)), mode="edge")
    top_left = padded[:-2, :-2]
    top = padded[:-2, 1:-1]
    top_right = padded[:-2, 2:]
    left = padded[1:-1, :-2]
    right = padded[1:-1, 2:]
    bottom_left = padded[2:, :-2]
    bottom = padded[2:, 1:-1]
    bottom_right = padded[2:, 2:]
    horizontal = (
        -top_left + top_right - 2.0 * left + 2.0 * right
        - bottom_left + bottom_right
    )
    vertical = (
        -top_left - 2.0 * top - top_right + bottom_left
        + 2.0 * bottom + bottom_right
    )
    # A Sobel component spans at most four channel-widths.  Normalize by that
    # bound before applying the requested edge threshold and gain.
    magnitude = np.sqrt(horizontal * horizontal + vertical * vertical) / 1020.0
    strength = np.clip((magnitude - 0.08) * 2.2, 0.0, 1.0)
    result = strength[:, :, None] * _CONTOUR_COLOR[None, None, :]
    return np.rint(result).astype(np.uint8)


def _clamped_shift(channel, amount):
    """Shift a channel horizontally, extending its edge pixels."""
    if channel.shape[1] == 0 or amount == 0:
        return channel
    indices = np.clip(np.arange(channel.shape[1]) - amount, 0, channel.shape[1] - 1)
    return channel[:, indices]


def _glitch_band_parameters(phase, band, rows, cols):
    """Derive stable band geometry without changing NumPy's global RNG."""
    mask = (1 << 64) - 1
    value = (phase & mask) ^ (0x9E3779B97F4A7C15 * (band + 1) & mask)
    value ^= value >> 30
    value = value * 0xBF58476D1CE4E5B9 & mask
    value ^= value >> 27
    value = value * 0x94D049BB133111EB & mask
    value ^= value >> 31

    max_height = max(1, rows // 8)
    height = 1 + value % max_height
    start = (value >> 8) % max(1, rows - height + 1)
    max_shift = max(1, min(8, cols // 12 or 1))
    shift = 1 + (value >> 24) % max_shift
    if (value >> 40) & 1:
        shift = -shift
    return int(start), int(height), int(shift)


def _glitch(frame, time_seconds):
    rows, cols = frame.shape[:2]
    if rows == 0 or cols == 0:
        return frame.copy()

    result = frame.copy()
    separation = min(2, max(0, cols - 1))
    result[:, :, 0] = _clamped_shift(frame[:, :, 0], separation)
    result[:, :, 2] = _clamped_shift(frame[:, :, 2], -separation)

    phase = math.floor(time_seconds * 8.0)
    for band in range(3):
        start, height, shift = _glitch_band_parameters(phase, band, rows, cols)
        stop = start + height
        result[start:stop] = np.roll(result[start:stop], shift, axis=1)

    # Rows four, eight, twelve, ... are scanlines at 75% brightness.
    scanlines = result[3::4]
    np.multiply(scanlines, 0.75, out=scanlines, casting="unsafe")
    return result


_TRANSFORMS = {
    "bayer": _bayer,
    "duotone": _duotone,
    "riso": _riso,
    "contour": _contour,
}


class StyleProcessor:
    """Select, cycle, and apply one of the registered RGB frame styles."""

    def __init__(self, name="classic"):
        self._name = "classic"
        self.select(name)

    @property
    def name(self):
        return self._name

    def select(self, name):
        if name not in STYLE_NAMES:
            choices = ", ".join(STYLE_NAMES)
            raise ValueError(f"unknown style {name!r}; choose from: {choices}")
        self._name = name
        return self._name

    def cycle(self):
        index = (STYLE_NAMES.index(self._name) + 1) % len(STYLE_NAMES)
        self._name = STYLE_NAMES[index]
        return self._name

    def reset(self):
        """Reset transient transform state while preserving the selection.

        Current transforms are pure functions of frame and decoded timestamp,
        so there is no cached temporal state to discard.  Keeping the explicit
        hook lets playback reset future stateful work at media boundaries.
        """

    def apply(self, frame, time_seconds=0.0):
        _validate_frame(frame)
        try:
            time_seconds = float(time_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("time_seconds must be a finite number") from error
        if not math.isfinite(time_seconds):
            raise ValueError("time_seconds must be a finite number")

        if self._name == "classic":
            return frame
        if self._name == "glitch":
            return _glitch(frame, time_seconds)
        return _TRANSFORMS[self._name](frame)

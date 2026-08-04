"""Typed frame contracts shared by structural effects and ANSI rendering."""

from __future__ import annotations

from dataclasses import dataclass
import math
import unicodedata

import numpy as np


def _validate_rgb(frame, label="rgb"):
    if not isinstance(frame, np.ndarray):
        raise TypeError(f"{label} must be a NumPy array")
    if frame.dtype != np.uint8:
        raise ValueError(f"{label} must have dtype uint8")
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError(f"{label} must have shape (rows, cols, 3)")


def _validate_glyphs(glyphs):
    if not isinstance(glyphs, str):
        raise TypeError("cell glyphs must be a string")
    if not glyphs:
        raise ValueError("cell glyphs must be non-empty")
    if len(glyphs) > 256:
        raise ValueError("cell glyphs cannot contain more than 256 entries")
    if "\x00" in glyphs:
        raise ValueError("cell glyphs cannot contain NUL")
    for glyph in glyphs:
        if (
            not glyph.isprintable()
            or unicodedata.combining(glyph)
            or unicodedata.east_asian_width(glyph) in ("W", "F")
        ):
            raise ValueError(
                "cell glyphs must contain only printable single-cell code points"
            )


@dataclass(frozen=True)
class EffectContext:
    """Per-presentation metadata supplied to a structural effect."""

    video_time: float
    frame_sequence: int
    cell_shape: tuple[int, int]
    requested_pixels: bool = False
    advance_state: bool = True

    def __post_init__(self):
        try:
            video_time = float(self.video_time)
        except (TypeError, ValueError) as error:
            raise ValueError("video_time must be a finite number") from error
        if not math.isfinite(video_time):
            raise ValueError("video_time must be a finite number")
        object.__setattr__(self, "video_time", video_time)
        if (
            isinstance(self.frame_sequence, bool)
            or not isinstance(self.frame_sequence, int)
            or self.frame_sequence < 0
        ):
            raise ValueError("frame_sequence must be a non-negative integer")
        if (
            not isinstance(self.cell_shape, tuple)
            or len(self.cell_shape) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                for value in self.cell_shape
            )
        ):
            raise ValueError("cell_shape must contain positive integer rows and cols")
        if not isinstance(self.requested_pixels, bool):
            raise TypeError("requested_pixels must be a boolean")
        if not isinstance(self.advance_state, bool):
            raise TypeError("advance_state must be a boolean")


@dataclass(frozen=True)
class CellPlane:
    """A dense terminal-cell glyph map and optional foreground colors."""

    glyph_indices: np.ndarray
    glyphs: str
    fg_rgb: np.ndarray | None = None

    def __post_init__(self):
        indices = self.glyph_indices
        if not isinstance(indices, np.ndarray):
            raise TypeError("glyph_indices must be a NumPy array")
        if indices.ndim != 2:
            raise ValueError("glyph_indices must have shape (rows, cols)")
        if indices.dtype.kind not in "iu":
            raise ValueError("glyph_indices must have an integer dtype")
        _validate_glyphs(self.glyphs)
        if indices.size:
            if indices.dtype.kind == "i" and int(indices.min()) < 0:
                raise ValueError("glyph_indices cannot be negative")
            if int(indices.max()) >= len(self.glyphs):
                raise ValueError("glyph_indices contains an out-of-range index")
        if self.fg_rgb is not None:
            _validate_rgb(self.fg_rgb, "fg_rgb")
            if self.fg_rgb.shape[:2] != indices.shape:
                raise ValueError("fg_rgb shape must match glyph_indices")


@dataclass(frozen=True)
class EffectFrame:
    """RGB samples plus an optional structural terminal-cell reconstruction."""

    rgb: np.ndarray
    cells: CellPlane | None = None

    def __post_init__(self):
        _validate_rgb(self.rgb)
        if self.cells is not None and not isinstance(self.cells, CellPlane):
            raise TypeError("cells must be a CellPlane or None")

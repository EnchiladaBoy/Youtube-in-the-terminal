"""Fast ANSI frame composition for :mod:`yt-ascii`.

The terminal protocol is byte-oriented, so frames are assembled in reusable
``uint8`` record grids instead of fixed-width Unicode arrays.  Each cell gets a
padded byte record; zero padding is removed once, in NumPy, after all cells and
row trailers have been filled.  Palette and rain glyphs may contain any Unicode
code point except NUL, which is reserved as the padding sentinel.
"""

from __future__ import annotations

import numpy as np

from yt_ascii_frames import CellPlane


FG = b"\x1b[38;2;"
BG = b"\x1b[48;2;"
RESET = b"\x1b[0m"
CLEAR_EOL = b"\x1b[K"
HALF_BLOCK = "▀".encode("utf-8")


def _decimal_lut():
    """Return right-aligned, zero-padded ASCII decimal bytes for 0..255."""
    lut = np.zeros((256, 3), dtype=np.uint8)
    for value in range(256):
        encoded = str(value).encode("ascii")
        lut[value, 3 - len(encoded):] = tuple(encoded)
    return lut


DECIMAL = _decimal_lut()


def _glyph_lut(text, label):
    """Encode code points into a zero-padded lookup table.

    NUL cannot be represented because zero is the record-padding sentinel.
    Command-line arguments cannot normally contain NUL, but an explicit error
    keeps the invariant true for programmatic callers and tests.
    """
    if not text:
        raise ValueError(f"{label} must be non-empty")
    if "\x00" in text:
        raise ValueError(f"{label} cannot contain NUL")
    encoded = [glyph.encode("utf-8") for glyph in text]
    width = max(map(len, encoded))
    lut = np.zeros((len(encoded), width), dtype=np.uint8)
    for index, glyph in enumerate(encoded):
        lut[index, :len(glyph)] = tuple(glyph)
    return lut


class AnsiRenderer:
    """Compose character, half-block, scatter and rain frames as UTF-8 bytes.

    ``color=False`` accepts a two-dimensional FFmpeg ``gray`` frame or RGB24 as
    a compatibility fallback. Color character mode expects ``(rows, cols, 3)``
    RGB24, and half-block mode expects ``(rows * 2, cols, 3)`` RGB24.
    """

    def __init__(self, chars, *, color=True, half_block=False,
                 rain_chars="01", rng=None):
        if half_block and not color:
            raise ValueError("half-block rendering requires color")
        self.color = color
        self.half_block = half_block
        self.palette_lut = _glyph_lut(chars, "palette")
        self.n_pal = self.palette_lut.shape[0] - 1
        self.rain_lut = _glyph_lut(rain_chars, "rain glyph set")
        self.rng = rng if rng is not None else np.random.default_rng()

        self._shape = None
        self._workspace_key = None
        self._effective_mode = None
        self._rain_workspace = False
        self._template = None
        self._records = None
        self._dirty = False
        self._scatter = {}
        self._rain = {}

    def reset_reveal(self):
        """Choose fresh scatter/rain timing on the next reveal frame."""
        self._scatter.clear()
        self._rain.clear()

    def render(self, frame, cell_plane=None):
        """Render a complete frame without a reveal mask."""
        plane = self._prepare_cell_plane(frame, cell_plane)
        self._fill_cells(frame, rain_workspace=False, plane=plane)
        return self._compact()

    def render_scatter(self, frame, fraction, cell_plane=None):
        """Reveal an exact fraction of cells using a stable random ordering."""
        plane = self._prepare_cell_plane(frame, cell_plane)
        rows, cols = plane[:2] if plane is not None else self._cell_shape(frame)
        total = rows * cols
        if self._scatter.get("shape") != (rows, cols):
            rank = np.empty(total, dtype=np.int64)
            rank[self.rng.permutation(total)] = np.arange(total)
            self._scatter = {
                "shape": (rows, cols),
                "rank": rank.reshape(rows, cols),
            }
        shown = max(0, min(total, int(round(total * fraction))))
        visible = self._scatter["rank"] < shown

        self._fill_cells(frame, rain_workspace=False, plane=plane)
        self._apply_visibility(visible)
        result = self._compact()
        self._dirty = True
        return result

    def render_rain(self, frame, fraction, cell_plane=None):
        """Reveal settled cells behind a sparse, fading rain trail."""
        plane = self._prepare_cell_plane(frame, cell_plane)
        rows, cols = plane[:2] if plane is not None else self._cell_shape(frame)
        if self._rain.get("cols") != cols:
            duration = self.rng.uniform(0.45, 0.8, cols)
            offset = self.rng.uniform(0.0, 1.0 - duration)
            self._rain = {
                "cols": cols,
                "duration": duration,
                "offset": offset,
            }

        trail = max(3, min(10, rows // 6))
        progress = np.clip(
            (fraction - self._rain["offset"]) / self._rain["duration"],
            0.0,
            1.0,
        )
        front = progress * (rows + trail)
        row_numbers = np.arange(rows)[:, None]
        distance = front[None, :] - row_numbers
        settled = distance >= trail
        in_trail = (
            (distance >= 0)
            & (distance < trail)
            & (front > 0.0)[None, :]
        )

        self._fill_cells(frame, rain_workspace=True, plane=plane)
        self._apply_visibility(settled)
        if in_trail.any():
            self._apply_rain_trail(distance, in_trail, trail)
        result = self._compact()
        self._dirty = True
        return result

    def _cell_shape(self, frame):
        if frame.dtype != np.uint8:
            raise ValueError("frame must have dtype uint8")
        if self.half_block:
            if frame.ndim != 3 or frame.shape[2] != 3 or frame.shape[0] % 2:
                raise ValueError("half-block frame must have shape (2*rows, cols, 3)")
            return frame.shape[0] // 2, frame.shape[1]
        if self.color:
            if frame.ndim != 3 or frame.shape[2] != 3:
                raise ValueError("color frame must have shape (rows, cols, 3)")
            return frame.shape[0], frame.shape[1]
        if frame.ndim == 2:
            return frame.shape
        if frame.ndim == 3 and frame.shape[2] == 3:
            return frame.shape[0], frame.shape[1]
        raise ValueError("grayscale frame must be gray (rows, cols) or RGB24")

    def _prepare_cell_plane(self, frame, cell_plane):
        """Validate and prepare a structured glyph plane for composition."""
        if cell_plane is None:
            return None
        if not isinstance(cell_plane, CellPlane):
            raise TypeError("cell_plane must be a CellPlane or None")

        rows, cols = self._cell_shape(frame)
        indices = cell_plane.glyph_indices
        if not isinstance(indices, np.ndarray):
            raise TypeError("cell plane glyph_indices must be a NumPy array")
        if indices.ndim != 2:
            raise ValueError(
                "cell plane glyph_indices must have shape (rows, cols)"
            )
        if indices.shape != (rows, cols):
            raise ValueError("cell plane shape must match the rendered cell shape")
        if indices.dtype.kind not in "iu":
            raise ValueError("cell plane glyph_indices must have an integer dtype")

        glyphs = cell_plane.glyphs
        if not isinstance(glyphs, str):
            raise TypeError("cell plane glyph map must be a string")
        glyph_lut = _glyph_lut(glyphs, "cell plane glyph map")
        if indices.size:
            if indices.dtype.kind == "i" and int(indices.min()) < 0:
                raise ValueError("cell plane glyph_indices cannot be negative")
            if int(indices.max()) >= len(glyphs):
                raise ValueError(
                    "cell plane glyph_indices contains an out-of-range index"
                )

        colors = cell_plane.fg_rgb
        if colors is not None:
            if not isinstance(colors, np.ndarray):
                raise TypeError("cell plane fg_rgb must be a NumPy array")
            if colors.dtype != np.uint8:
                raise ValueError("cell plane fg_rgb must have dtype uint8")
            if colors.ndim != 3 or colors.shape[2] != 3:
                raise ValueError(
                    "cell plane fg_rgb must have shape (rows, cols, 3)"
                )
            if colors.shape[:2] != indices.shape:
                raise ValueError(
                    "cell plane fg_rgb shape must match glyph_indices"
                )

        if self.color and colors is None:
            if frame.ndim != 3 or frame.shape[:2] != indices.shape:
                raise ValueError(
                    "cell plane fg_rgb is required when source pixels do not "
                    "match the cell shape"
                )
            colors = frame
        if not self.color:
            colors = None

        return rows, cols, indices, glyph_lut, colors

    def _ensure_workspace(self, rows, cols, rain_workspace, *, mode,
                          colored, glyph_width):
        key = (rows, cols, mode, colored, glyph_width, rain_workspace)
        if self._workspace_key == key:
            return

        shape = (rows, cols)
        row_suffix = RESET + CLEAR_EOL if colored or mode in (
            "half-block", "fallback-chars"
        ) else CLEAR_EOL
        rain_width = self.rain_lut.shape[1]
        if mode == "half-block":
            normal_width = 41
        elif colored:
            normal_width = 19 + glyph_width
            if mode == "fallback-chars":
                normal_width += len(RESET)
        else:
            normal_width = glyph_width
        rain_prefix = (
            len(RESET)
            if mode in ("half-block", "plane-chars", "fallback-chars")
            else 0
        )
        rain_record_width = (
            rain_prefix + 19 + rain_width + len(RESET)
            if rain_workspace else 0
        )
        record_width = max(normal_width, rain_record_width, len(row_suffix) + 1)

        template = np.zeros((rows, cols + 1, record_width), dtype=np.uint8)
        cells = template[:, :cols]
        if mode == "half-block":
            cells[:, :, :7] = tuple(FG)
            cells[:, :, 10] = ord(";")
            cells[:, :, 14] = ord(";")
            cells[:, :, 18] = ord("m")
            cells[:, :, 19:26] = tuple(BG)
            cells[:, :, 29] = ord(";")
            cells[:, :, 33] = ord(";")
            cells[:, :, 37] = ord("m")
            cells[:, :, 38:41] = tuple(HALF_BLOCK)
        elif colored:
            prefix = len(RESET) if mode == "fallback-chars" else 0
            if prefix:
                cells[:, :, :prefix] = tuple(RESET)
            cells[:, :, prefix:prefix + 7] = tuple(FG)
            cells[:, :, prefix + 10] = ord(";")
            cells[:, :, prefix + 14] = ord(";")
            cells[:, :, prefix + 18] = ord("m")

        trailers = template[:, cols]
        trailers[:, :len(row_suffix)] = tuple(row_suffix)
        trailers[:-1, len(row_suffix)] = ord("\n")

        self._shape = shape
        self._workspace_key = key
        self._effective_mode = mode
        self._rain_workspace = rain_workspace
        self._template = template
        self._records = template.copy()
        self._dirty = False

    def _fill_cells(self, frame, rain_workspace, plane=None):
        if plane is None:
            rows, cols = self._cell_shape(frame)
            glyph_lut = self.palette_lut
            mode = (
                "half-block" if self.half_block
                else "color-chars" if self.color
                else "gray-chars"
            )
            colored = self.color
        else:
            rows, cols, indices, glyph_lut, colors = plane
            mode = "fallback-chars" if self.half_block else "plane-chars"
            colored = self.color
        if not frame.flags.c_contiguous:
            frame = np.ascontiguousarray(frame)
        self._ensure_workspace(
            rows,
            cols,
            rain_workspace,
            mode=mode,
            colored=colored,
            glyph_width=glyph_lut.shape[1],
        )
        if self._dirty:
            np.copyto(self._records, self._template)
            self._dirty = False
        cells = self._records[:, :cols]

        if plane is not None:
            indices = indices.astype(np.intp, copy=False)
            glyph_start = (
                len(RESET) if mode == "fallback-chars" else 0
            ) + (19 if colored else 0)
            if colored:
                color_start = len(RESET) if mode == "fallback-chars" else 0
                cells[:, :, color_start + 7:color_start + 10] = DECIMAL[
                    colors[:, :, 0]
                ]
                cells[:, :, color_start + 11:color_start + 14] = DECIMAL[
                    colors[:, :, 1]
                ]
                cells[:, :, color_start + 15:color_start + 18] = DECIMAL[
                    colors[:, :, 2]
                ]
            cells[:, :, glyph_start:glyph_start + glyph_lut.shape[1]] = (
                glyph_lut[indices]
            )
            return

        if self.half_block:
            top = frame[0::2]
            bottom = frame[1::2]
            cells[:, :, 7:10] = DECIMAL[top[:, :, 0]]
            cells[:, :, 11:14] = DECIMAL[top[:, :, 1]]
            cells[:, :, 15:18] = DECIMAL[top[:, :, 2]]
            cells[:, :, 26:29] = DECIMAL[bottom[:, :, 0]]
            cells[:, :, 30:33] = DECIMAL[bottom[:, :, 1]]
            cells[:, :, 34:37] = DECIMAL[bottom[:, :, 2]]
            return

        if self.color:
            luminance = (
                0.299 * frame[:, :, 0]
                + 0.587 * frame[:, :, 1]
                + 0.114 * frame[:, :, 2]
            ).astype(np.int32)
            indices = (luminance * self.n_pal) // 255
            cells[:, :, 7:10] = DECIMAL[frame[:, :, 0]]
            cells[:, :, 11:14] = DECIMAL[frame[:, :, 1]]
            cells[:, :, 15:18] = DECIMAL[frame[:, :, 2]]
            cells[:, :, 19:19 + self.palette_lut.shape[1]] = self.palette_lut[indices]
            return

        if frame.ndim == 3:
            luminance = (
                0.299 * frame[:, :, 0]
                + 0.587 * frame[:, :, 1]
                + 0.114 * frame[:, :, 2]
            ).astype(np.int32)
            indices = (luminance * self.n_pal) // 255
        else:
            indices = frame.astype(np.uint32) * self.n_pal // 255
        indices = indices.astype(np.intp, copy=False)
        cells[:, :, :self.palette_lut.shape[1]] = self.palette_lut[indices]

    def _apply_visibility(self, visible):
        hidden = ~visible
        if not hidden.any():
            return
        cells = self._records[:, :self._shape[1]]
        blank = (
            RESET + b" "
            if self._effective_mode in ("half-block", "fallback-chars")
            else b" "
        )
        blank_record = np.zeros(self._records.shape[2], dtype=np.uint8)
        blank_record[:len(blank)] = tuple(blank)
        cells[hidden] = blank_record

    def _apply_rain_trail(self, distance, in_trail, trail):
        ys, xs = np.nonzero(in_trail)
        values = (
            255 - 200 * np.clip(distance[ys, xs] / trail, 0.0, 1.0)
        ).astype(np.intp)
        glyphs = self.rng.integers(0, self.rain_lut.shape[0], size=ys.size)

        width = self._records.shape[2]
        rain = np.zeros((ys.size, width), dtype=np.uint8)
        position = 0
        if self._effective_mode in (
            "half-block", "plane-chars", "fallback-chars"
        ):
            rain[:, :len(RESET)] = tuple(RESET)
            position += len(RESET)
        rain[:, position:position + len(FG)] = tuple(FG)
        rain[:, position + 7:position + 10] = DECIMAL[values]
        rain[:, position + 10] = ord(";")
        rain[:, position + 11:position + 14] = DECIMAL[values]
        rain[:, position + 14] = ord(";")
        rain[:, position + 15:position + 18] = DECIMAL[values]
        rain[:, position + 18] = ord("m")
        glyph_start = position + 19
        glyph_end = glyph_start + self.rain_lut.shape[1]
        rain[:, glyph_start:glyph_end] = self.rain_lut[glyphs]
        rain[:, glyph_end:glyph_end + len(RESET)] = tuple(RESET)
        self._records[ys, xs] = rain

    def _compact(self):
        return self._records[self._records != 0].tobytes()

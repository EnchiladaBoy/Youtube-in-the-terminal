import unittest

import numpy as np

from yt_ascii_renderer import AnsiRenderer, CLEAR_EOL, RESET


def legacy_chars(frame, chars, color=True):
    palette = np.array(list(chars), dtype="U1")
    luminance = (
        0.299 * frame[:, :, 0]
        + 0.587 * frame[:, :, 1]
        + 0.114 * frame[:, :, 2]
    ).astype(np.int32)
    cells = palette[(luminance * (palette.size - 1)) // 255]
    if not color:
        return "\n".join(
            "".join(row) + "\x1b[K" for row in cells
        ).encode("utf-8")
    red = frame[:, :, 0].astype("U3")
    green = frame[:, :, 1].astype("U3")
    blue = frame[:, :, 2].astype("U3")
    output = np.char.add("\x1b[38;2;", red)
    output = np.char.add(output, ";")
    output = np.char.add(output, green)
    output = np.char.add(output, ";")
    output = np.char.add(output, blue)
    output = np.char.add(output, "m")
    output = np.char.add(output, cells)
    return "\n".join(
        "".join(row) + "\x1b[0m\x1b[K" for row in output
    ).encode("utf-8")


def legacy_half(frame):
    top = frame[0::2]
    bottom = frame[1::2]
    tr = top[:, :, 0].astype("U3")
    tg = top[:, :, 1].astype("U3")
    tb = top[:, :, 2].astype("U3")
    br = bottom[:, :, 0].astype("U3")
    bg = bottom[:, :, 1].astype("U3")
    bb = bottom[:, :, 2].astype("U3")
    output = np.char.add("\x1b[38;2;", tr)
    output = np.char.add(output, ";")
    output = np.char.add(output, tg)
    output = np.char.add(output, ";")
    output = np.char.add(output, tb)
    output = np.char.add(output, "m\x1b[48;2;")
    output = np.char.add(output, br)
    output = np.char.add(output, ";")
    output = np.char.add(output, bg)
    output = np.char.add(output, ";")
    output = np.char.add(output, bb)
    output = np.char.add(output, "m▀")
    return "\n".join(
        "".join(row) + "\x1b[0m\x1b[K" for row in output
    ).encode("utf-8")


class RendererTests(unittest.TestCase):
    def test_color_output_matches_legacy_renderer(self):
        rng = np.random.default_rng(20260803)
        boundary = np.array(
            [0, 1, 9, 10, 99, 100, 254, 255], dtype=np.uint8
        )
        frames = [
            np.resize(boundary, (5, 8, 3)).copy(),
            rng.integers(0, 256, (7, 11, 3), dtype=np.uint8),
            rng.integers(0, 256, (13, 19, 3), dtype=np.uint8),
        ]
        for chars in ("x", " .:-=+*#%@", " ░▒▓█", " ·λ🚀"):
            renderer = AnsiRenderer(chars)
            for frame in frames:
                with self.subTest(chars=chars, shape=frame.shape):
                    self.assertEqual(renderer.render(frame), legacy_chars(frame, chars))

    def test_half_block_output_matches_legacy_renderer(self):
        rng = np.random.default_rng(17)
        for shape in ((2, 1, 3), (6, 5, 3), (18, 13, 3)):
            frame = rng.integers(0, 256, shape, dtype=np.uint8)
            with self.subTest(shape=shape):
                self.assertEqual(
                    AnsiRenderer("x", half_block=True).render(frame),
                    legacy_half(frame),
                )

    def test_exact_single_cell_snapshots(self):
        color = np.array([[[1, 2, 3]]], dtype=np.uint8)
        self.assertEqual(
            AnsiRenderer("x").render(color),
            b"\x1b[38;2;1;2;3mx\x1b[0m\x1b[K",
        )
        half = np.array([[[1, 2, 3]], [[254, 255, 0]]], dtype=np.uint8)
        self.assertEqual(
            AnsiRenderer("x", half_block=True).render(half),
            (
                b"\x1b[38;2;1;2;3m\x1b[48;2;254;255;0m"
                + "▀".encode("utf-8")
                + b"\x1b[0m\x1b[K"
            ),
        )

    def test_grayscale_uses_one_byte_frames_and_palette_endpoints(self):
        frame = np.array([[0, 127, 128, 255]], dtype=np.uint8)
        self.assertEqual(
            AnsiRenderer(" .@", color=False).render(frame),
            b"  .@\x1b[K",
        )

    def test_grayscale_long_palette_does_not_overflow(self):
        chars = "".join(chr(0x400 + index) for index in range(300))
        frame = np.array([[0, 255]], dtype=np.uint8)
        self.assertEqual(
            AnsiRenderer(chars, color=False).render(frame),
            chars[0].encode("utf-8") + chars[-1].encode("utf-8") + CLEAR_EOL,
        )

    def test_grayscale_rgb_fallback_matches_legacy_luminance(self):
        frame = np.array(
            [[[0, 10, 255], [255, 100, 1]], [[9, 99, 254], [1, 2, 3]]],
            dtype=np.uint8,
        )
        self.assertEqual(
            AnsiRenderer(" .:-=+*#%@", color=False).render(frame),
            legacy_chars(frame, " .:-=+*#%@", color=False),
        )

    def test_rows_have_one_separator_and_no_trailing_newline(self):
        frame = np.zeros((2, 2), dtype=np.uint8)
        output = AnsiRenderer("x", color=False).render(frame)
        self.assertEqual(output, b"xx\x1b[K\nxx\x1b[K")
        self.assertFalse(output.endswith(b"\n"))

    def test_scatter_endpoints_and_stable_monotonic_order(self):
        frame = np.arange(4 * 6 * 3, dtype=np.uint8).reshape(4, 6, 3)
        renderer = AnsiRenderer(" .#", rng=np.random.default_rng(4))
        plain = renderer.render(frame)
        blank_row = b" " * 6 + RESET + CLEAR_EOL
        self.assertEqual(renderer.render_scatter(frame, 0), b"\n".join([blank_row] * 4))

        renderer.render_scatter(frame, 0.25)
        rank = renderer._scatter["rank"].copy()
        visible_25 = rank < round(rank.size * 0.25)
        renderer.render_scatter(frame, 0.5)
        visible_50 = renderer._scatter["rank"] < round(rank.size * 0.5)
        self.assertEqual(int(visible_25.sum()), round(rank.size * 0.25))
        self.assertTrue(np.all(visible_25 <= visible_50))
        self.assertEqual(renderer.render_scatter(frame, 1), plain)

    def test_half_scatter_clears_hidden_background(self):
        frame = np.zeros((4, 3, 3), dtype=np.uint8)
        renderer = AnsiRenderer("x", half_block=True, rng=np.random.default_rng(1))
        blank_row = (RESET + b" ") * 3 + RESET + CLEAR_EOL
        self.assertEqual(renderer.render_scatter(frame, 0), b"\n".join([blank_row] * 2))

    def test_rain_endpoints_unicode_and_no_padding_bytes(self):
        frame = np.arange(5 * 7 * 3, dtype=np.uint8).reshape(5, 7, 3)
        renderer = AnsiRenderer(
            " .#", rain_chars="λ🚀", rng=np.random.default_rng(8)
        )
        plain = renderer.render(frame)
        blank_row = b" " * 7 + RESET + CLEAR_EOL
        self.assertEqual(renderer.render_rain(frame, 0), b"\n".join([blank_row] * 5))
        middle = renderer.render_rain(frame, 0.5)
        middle.decode("utf-8")
        self.assertNotIn(b"\x00", middle)
        self.assertEqual(renderer.render_rain(frame, 1), plain)

    def test_half_block_rain_resets_background_before_trail(self):
        frame = np.zeros((12, 4, 3), dtype=np.uint8)
        renderer = AnsiRenderer(
            "x", half_block=True, rain_chars="λ", rng=np.random.default_rng(3)
        )
        middle = renderer.render_rain(frame, 0.5)
        self.assertIn(RESET + b"\x1b[38;2;", middle)
        self.assertNotIn(b"\x00", middle)

    def test_grayscale_reveal_endpoints(self):
        frame = np.arange(20, dtype=np.uint8).reshape(4, 5)
        renderer = AnsiRenderer(
            " .#", color=False, rain_chars="λ", rng=np.random.default_rng(9)
        )
        plain = renderer.render(frame)
        self.assertEqual(renderer.render_scatter(frame, 1), plain)
        renderer.reset_reveal()
        self.assertEqual(renderer.render_rain(frame, 1), plain)

    def test_invalid_frames_and_nul_glyphs_are_rejected(self):
        with self.assertRaises(ValueError):
            AnsiRenderer("\x00")
        with self.assertRaises(ValueError):
            AnsiRenderer("x", rain_chars="\x00")
        with self.assertRaises(ValueError):
            AnsiRenderer("x", color=False, half_block=True)
        with self.assertRaises(ValueError):
            AnsiRenderer("x").render(np.zeros((2, 2), dtype=np.uint8))
        with self.assertRaises(ValueError):
            AnsiRenderer("x", half_block=True).render(
                np.zeros((3, 2, 3), dtype=np.uint8)
            )


if __name__ == "__main__":
    unittest.main()

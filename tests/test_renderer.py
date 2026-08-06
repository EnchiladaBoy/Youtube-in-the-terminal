import re
import unittest

import numpy as np

from yt_ascii_frames import CellPlane
from yt_ascii_renderer import (
    ASCII_PALETTE,
    AnsiRenderer,
    BG,
    CLEAR_EOL,
    FG,
    HALF_BLOCK,
    RENDER_BACKENDS,
    RESET,
    get_render_backend,
)


ANSI_CSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")


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


def unsafe_cell_plane(glyph_indices, glyphs, fg_rgb=None):
    """Build a plane without dataclass validation to test renderer boundaries."""
    plane = object.__new__(CellPlane)
    object.__setattr__(plane, "glyph_indices", glyph_indices)
    object.__setattr__(plane, "glyphs", glyphs)
    object.__setattr__(plane, "fg_rgb", fg_rgb)
    return plane


class RendererTests(unittest.TestCase):
    def test_backend_registry_exposes_portable_capabilities(self):
        self.assertEqual(tuple(RENDER_BACKENDS), ("chars", "cells", "half-block"))
        self.assertFalse(get_render_backend("chars").unicode_dependent)
        self.assertTrue(get_render_backend("chars").supports_cell_plane)
        self.assertFalse(get_render_backend("cells").unicode_dependent)
        self.assertFalse(get_render_backend("cells").supports_cell_plane)
        self.assertTrue(get_render_backend("cells").requires_color)
        self.assertTrue(get_render_backend("half-block").unicode_dependent)
        self.assertEqual(get_render_backend("half-block").source_rows_per_cell, 2)

        with self.assertRaisesRegex(ValueError, "unknown render mode"):
            get_render_backend("kitty")
        with self.assertRaisesRegex(TypeError, "must be a string"):
            get_render_backend(None)

    def test_default_chars_backend_is_ascii_only(self):
        frame = np.arange(3 * len(ASCII_PALETTE), dtype=np.uint8).reshape(
            1, len(ASCII_PALETTE), 3
        )
        renderer = AnsiRenderer(color=False)
        output = renderer.render(frame)
        self.assertEqual(renderer.render_mode, "chars")
        self.assertEqual(renderer.effective_render_mode, "chars")
        output.decode("ascii")
        self.assertNotIn("▀".encode("utf-8"), output)

    def test_cells_emit_background_colors_and_spaces_only(self):
        frame = np.array(
            [[[1, 2, 3], [254, 255, 0]], [[9, 10, 11], [99, 100, 101]]],
            dtype=np.uint8,
        )
        output = AnsiRenderer(render_mode="cells").render(frame)
        self.assertEqual(
            output,
            (
                b"\x1b[48;2;1;2;3m "
                b"\x1b[48;2;254;255;0m " + RESET + CLEAR_EOL + b"\n"
                b"\x1b[48;2;9;10;11m "
                b"\x1b[48;2;99;100;101m " + RESET + CLEAR_EOL
            ),
        )
        self.assertNotIn(b"\x1b[38;2;", output)
        self.assertNotIn("▀".encode("utf-8"), output)
        output.decode("ascii")

    def test_cells_no_color_has_explicit_ascii_character_fallback(self):
        frame = np.array([[0, 127, 128, 255]], dtype=np.uint8)
        renderer = AnsiRenderer(
            "λ🚀", color=False, render_mode="cells", rain_chars="λ"
        )
        output = renderer.render(frame)
        self.assertEqual(renderer.requested_render_mode, "cells")
        self.assertEqual(renderer.requested_backend, get_render_backend("cells"))
        self.assertEqual(renderer.render_mode, "chars")
        self.assertEqual(renderer.backend, get_render_backend("chars"))
        self.assertEqual(renderer.effective_render_mode, "chars")
        self.assertEqual(renderer.effective_backend, get_render_backend("chars"))
        self.assertEqual(output, b" ==@" + CLEAR_EOL)
        output.decode("ascii")

        plane = CellPlane(
            np.array([[0, 1, 0, 1]], dtype=np.uint8), "AB"
        )
        self.assertEqual(renderer.render(frame, plane), b"ABAB" + CLEAR_EOL)

        middle = renderer.render_rain(frame, 0.5)
        middle.decode("ascii")
        self.assertNotIn("λ".encode("utf-8"), middle)

    def test_half_block_no_color_has_explicit_ascii_character_fallback(self):
        frame = np.array([[0, 127, 128, 255]], dtype=np.uint8)
        renderer = AnsiRenderer(
            "λ🚀", color=False, render_mode="half-block", rain_chars="λ"
        )
        output = renderer.render(frame)
        self.assertEqual(renderer.requested_render_mode, "half-block")
        self.assertEqual(
            renderer.requested_backend, get_render_backend("half-block")
        )
        self.assertEqual(renderer.render_mode, "chars")
        self.assertEqual(renderer.backend, get_render_backend("chars"))
        self.assertEqual(renderer.effective_render_mode, "chars")
        self.assertEqual(renderer.effective_backend, get_render_backend("chars"))
        self.assertEqual(output, b" ==@" + CLEAR_EOL)
        output.decode("ascii")
        self.assertNotIn(HALF_BLOCK, output)

        middle = renderer.render_rain(frame, 0.5)
        middle.decode("ascii")
        self.assertNotIn("λ".encode("utf-8"), middle)
        self.assertNotIn(HALF_BLOCK, middle)

        plane = CellPlane(
            np.array([[0, 1, 0, 1]], dtype=np.uint8), "AB"
        )
        self.assertEqual(renderer.render(frame, plane), b"ABAB" + CLEAR_EOL)

    def test_cells_reveals_never_emit_decorative_glyphs(self):
        frame = np.arange(8 * 3 * 3, dtype=np.uint8).reshape(8, 3, 3)
        renderer = AnsiRenderer(
            render_mode="cells", rain_chars="λЖ", rng=np.random.default_rng(8)
        )
        for output in (
            renderer.render(frame),
            renderer.render_scatter(frame, 0.5),
            renderer.render_rain(frame, 0.5),
        ):
            with self.subTest(output=output[:40]):
                output.decode("ascii")
                self.assertNotIn(b"\x1b[38;2;", output)
                self.assertNotIn("λ".encode("utf-8"), output)
                self.assertNotIn("🚀".encode("utf-8"), output)
                visible = ANSI_CSI_RE.sub(b"", output)
                self.assertLessEqual(set(visible), {ord(" "), ord("\n")})
                self.assertEqual(visible.count(b" "), 8 * 3)

    def test_render_mode_and_legacy_half_block_selection(self):
        canonical = AnsiRenderer(render_mode="half-block")
        legacy = AnsiRenderer(half_block=True)
        self.assertEqual(canonical.render_mode, "half-block")
        self.assertEqual(canonical.backend, get_render_backend("half-block"))
        self.assertTrue(legacy.half_block)

        with self.assertRaisesRegex(ValueError, "conflicts"):
            AnsiRenderer(half_block=True, render_mode="cells")

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

    def test_palette_switch_rebuilds_glyph_workspace_without_resetting_reveals(self):
        frame = np.array([[0, 63, 127, 191, 255]], dtype=np.uint8)
        renderer = AnsiRenderer(" .", color=False, rng=np.random.default_rng(9))
        renderer.render_scatter(frame, 0.4)
        renderer.render_rain(frame, 0.4)
        scatter_rank = renderer._scatter["rank"].copy()
        rain_duration = renderer._rain["duration"].copy()
        rain_offset = renderer._rain["offset"].copy()

        for chars in (" ░▒▓█", "x", " .:-=+*#%@"):
            with self.subTest(chars=chars):
                renderer.set_palette(chars)
                output = renderer.render(frame)
                self.assertEqual(
                    output,
                    AnsiRenderer(chars, color=False).render(frame),
                )
                self.assertNotIn(b"\x00", output)
                np.testing.assert_array_equal(
                    renderer._scatter["rank"], scatter_rank
                )
                np.testing.assert_array_equal(
                    renderer._rain["duration"], rain_duration
                )
                np.testing.assert_array_equal(
                    renderer._rain["offset"], rain_offset
                )

    def test_palette_switch_materially_changes_colored_chars(self):
        levels = np.array([0, 63, 127, 191, 255], dtype=np.uint8)
        frame = np.repeat(levels[None, :, None], 3, axis=2)
        renderer = AnsiRenderer(" .:-=+*#%@")
        before = renderer.render(frame)
        renderer.set_palette(" 01")
        after = renderer.render(frame)
        self.assertNotEqual(after, before)
        self.assertEqual(after, AnsiRenderer(" 01").render(frame))

    def test_palette_switch_validation_is_atomic_and_chars_only(self):
        renderer = AnsiRenderer(" .", color=False)
        frame = np.array([[0, 255]], dtype=np.uint8)
        renderer.render(frame)
        original_lut = renderer.palette_lut.copy()
        original_key = renderer._workspace_key
        for invalid in ("", "x\x00y"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                renderer.set_palette(invalid)
            np.testing.assert_array_equal(renderer.palette_lut, original_lut)
            self.assertEqual(renderer._workspace_key, original_key)

        for mode in ("cells", "half-block"):
            for color in (True, False):
                with self.subTest(
                    mode=mode, color=color
                ), self.assertRaisesRegex(
                    ValueError, "require render_mode='chars'"
                ):
                    AnsiRenderer(
                        "x", render_mode=mode, color=color
                    ).set_palette(" .#")

    def test_grayscale_rgb_fallback_matches_legacy_luminance(self):
        frame = np.array(
            [[[0, 10, 255], [255, 100, 1]], [[9, 99, 254], [1, 2, 3]]],
            dtype=np.uint8,
        )
        self.assertEqual(
            AnsiRenderer(" .:-=+*#%@", color=False).render(frame),
            legacy_chars(frame, " .:-=+*#%@", color=False),
        )

    def test_ascii_cell_plane_uses_explicit_and_derived_foreground(self):
        frame = np.array([[[1, 2, 3], [4, 5, 6]]], dtype=np.uint8)
        explicit = np.array([[[7, 8, 9], [10, 11, 12]]], dtype=np.uint8)
        plane = CellPlane(
            np.array([[0, 1]], dtype=np.uint8), "XO", explicit
        )
        self.assertEqual(
            AnsiRenderer("ignored").render(frame, plane),
            (
                b"\x1b[38;2;7;8;9mX"
                b"\x1b[38;2;10;11;12mO"
                + RESET + CLEAR_EOL
            ),
        )

        derived = CellPlane(np.array([[1, 0]], dtype=np.uint8), "ab")
        self.assertEqual(
            AnsiRenderer("ignored").render(frame, derived),
            (
                b"\x1b[38;2;1;2;3mb"
                b"\x1b[38;2;4;5;6ma"
                + RESET + CLEAR_EOL
            ),
        )

    def test_multibyte_cell_plane_and_grayscale_color_policy(self):
        frame = np.array(
            [[[1, 2, 3], [4, 5, 6], [7, 8, 9]]], dtype=np.uint8
        )
        plane = CellPlane(
            np.array([[0, 1, 0]], dtype=np.uint16),
            "λ▀",
            np.full((1, 3, 3), 255, dtype=np.uint8),
        )
        self.assertEqual(
            AnsiRenderer("x", color=False).render(frame, plane),
            "λ▀λ".encode("utf-8") + CLEAR_EOL,
        )

    def test_cell_plane_schema_switch_rebuilds_only_ansi_workspace(self):
        frame = np.zeros((3, 4, 3), dtype=np.uint8)
        renderer = AnsiRenderer("x", rng=np.random.default_rng(31))
        ascii_plane = CellPlane(
            np.resize(np.array([0, 1], dtype=np.uint8), (3, 4)), "AB"
        )
        unicode_plane = CellPlane(
            np.resize(np.array([1, 0], dtype=np.uint8), (3, 4)), "λЖ"
        )

        renderer.render_scatter(frame, 0.5, ascii_plane)
        rank = renderer._scatter["rank"].copy()
        first_rain = renderer.render_rain(frame, 0.4, ascii_plane)
        rain_duration = renderer._rain["duration"].copy()
        second_rain = renderer.render_rain(frame, 0.4, unicode_plane)

        np.testing.assert_array_equal(renderer._scatter["rank"], rank)
        np.testing.assert_array_equal(renderer._rain["duration"], rain_duration)
        first_rain.decode("utf-8")
        second_rain.decode("utf-8")
        self.assertNotIn(b"\x00", second_rain)
        self.assertEqual(renderer._workspace_key[4], len("λ".encode("utf-8")))

    def test_graphical_backends_reject_structured_text_planes(self):
        frame = np.zeros((4, 2, 3), dtype=np.uint8)
        colors = np.array(
            [
                [[1, 2, 3], [4, 5, 6]],
                [[7, 8, 9], [10, 11, 12]],
            ],
            dtype=np.uint8,
        )
        plane = CellPlane(
            np.array([[0, 1], [1, 0]], dtype=np.uint8), "AB", colors
        )
        for render_mode in ("cells", "half-block"):
            with self.subTest(render_mode=render_mode), self.assertRaisesRegex(
                ValueError, "structured text cell planes require"
            ):
                AnsiRenderer(render_mode=render_mode).render(frame, plane)

    def test_renderer_revalidates_mutated_cell_plane_contract(self):
        frame = np.zeros((2, 3, 3), dtype=np.uint8)
        renderer = AnsiRenderer("x")
        valid_indices = np.zeros((2, 3), dtype=np.uint8)
        valid_colors = np.zeros((2, 3, 3), dtype=np.uint8)
        invalid_planes = (
            unsafe_cell_plane([[0]], "x", valid_colors),
            unsafe_cell_plane(np.zeros((2, 3, 1), dtype=np.uint8), "x", valid_colors),
            unsafe_cell_plane(np.zeros((2, 2), dtype=np.uint8), "x", np.zeros((2, 2, 3), dtype=np.uint8)),
            unsafe_cell_plane(valid_indices.astype(np.float32), "x", valid_colors),
            unsafe_cell_plane(valid_indices, "", valid_colors),
            unsafe_cell_plane(valid_indices, "x\x00", valid_colors),
            unsafe_cell_plane(np.full((2, 3), 1, dtype=np.uint8), "x", valid_colors),
            unsafe_cell_plane(np.full((2, 3), -1, dtype=np.int8), "x", valid_colors),
            unsafe_cell_plane(valid_indices, "x", valid_colors.astype(np.int16)),
            unsafe_cell_plane(valid_indices, "x", np.zeros((2, 3), dtype=np.uint8)),
            unsafe_cell_plane(valid_indices, "x", np.zeros((3, 2, 3), dtype=np.uint8)),
        )
        with self.assertRaises(TypeError):
            renderer.render(frame, object())
        for position, plane in enumerate(invalid_planes):
            with self.subTest(position=position), self.assertRaises(
                (TypeError, ValueError)
            ):
                renderer.render(frame, plane)

    def test_cell_plane_reveal_completion_matches_normal_render(self):
        frame = np.arange(6 * 5 * 3, dtype=np.uint8).reshape(6, 5, 3)
        plane = CellPlane(
            np.resize(np.arange(3, dtype=np.uint8), (6, 5)), "Aλ#"
        )
        renderer = AnsiRenderer("x", rain_chars="R", rng=np.random.default_rng(4))
        normal = renderer.render(frame, plane)
        self.assertEqual(renderer.render_scatter(frame, 1, plane), normal)
        renderer.reset_reveal()
        self.assertEqual(renderer.render_rain(frame, 1, plane), normal)

    def test_cell_plane_rain_override_is_reset_and_foreground_bounded(self):
        frame = np.zeros((8, 2, 3), dtype=np.uint8)
        plane = CellPlane(np.zeros((8, 2), dtype=np.uint8), "P")
        renderer = AnsiRenderer(
            "x", rain_chars="R", rng=np.random.default_rng(13)
        )
        renderer._rain = {
            "cols": 2,
            "duration": np.ones(2),
            "offset": np.zeros(2),
        }
        output = renderer.render_rain(frame, 0.5, plane)
        self.assertIn(RESET + b"\x1b[38;2;", output)
        self.assertIn(b"mR" + RESET, output)
        self.assertIn(b"mP", output)
        self.assertNotIn(b"\x00", output)

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
            " .#", rain_chars="λЖ", rng=np.random.default_rng(8)
        )
        plain = renderer.render(frame)
        blank_row = b" " * 7 + RESET + CLEAR_EOL
        self.assertEqual(renderer.render_rain(frame, 0), b"\n".join([blank_row] * 5))
        middle = renderer.render_rain(frame, 0.5)
        middle.decode("utf-8")
        self.assertNotIn(b"\x00", middle)
        self.assertEqual(renderer.render_rain(frame, 1), plain)

    def test_half_block_rain_preserves_half_block_backend_contract(self):
        frame = np.zeros((12, 4, 3), dtype=np.uint8)
        renderer = AnsiRenderer(
            "x", half_block=True, rain_chars="R", rng=np.random.default_rng(3)
        )
        middle = renderer.render_rain(frame, 0.5)
        self.assertIn(RESET + b"\x1b[38;2;", middle)
        self.assertIn(BG, middle)
        self.assertIn(HALF_BLOCK, middle)
        self.assertNotIn(b"R", middle)
        self.assertNotIn(b"\x00", middle)

    def test_grayscale_reveal_endpoints(self):
        frame = np.arange(20, dtype=np.uint8).reshape(4, 5)
        renderer = AnsiRenderer(
            " .#", color=False, rain_chars="R", rng=np.random.default_rng(9)
        )
        plain = renderer.render(frame)
        self.assertEqual(renderer.render_scatter(frame, 1), plain)
        renderer.reset_reveal()
        middle = renderer.render_rain(frame, 0.5)
        self.assertNotIn(FG, middle)
        self.assertNotIn(BG, middle)
        self.assertIn(b"R", middle)
        self.assertEqual(renderer.render_rain(frame, 1), plain)

    def test_no_color_cell_plane_rain_never_reintroduces_truecolor(self):
        frame = np.zeros((8, 2, 3), dtype=np.uint8)
        colors = np.full((8, 2, 3), (20, 40, 60), dtype=np.uint8)
        plane = CellPlane(np.zeros((8, 2), dtype=np.uint8), "P", colors)
        renderer = AnsiRenderer(
            "x", color=False, rain_chars="R", rng=np.random.default_rng(13)
        )
        renderer._rain = {
            "cols": 2,
            "duration": np.ones(2),
            "offset": np.zeros(2),
        }
        output = renderer.render_rain(frame, 0.5, plane)
        self.assertNotIn(FG, output)
        self.assertNotIn(BG, output)
        self.assertIn(b"R", output)

    def test_invalid_frames_and_nul_glyphs_are_rejected(self):
        with self.assertRaises(ValueError):
            AnsiRenderer("\x00")
        with self.assertRaises(ValueError):
            AnsiRenderer("x", rain_chars="\x00")
        for unsafe in ("🚀", "A\u0301", "א"):
            with self.subTest(rain_chars=unsafe), self.assertRaisesRegex(
                ValueError, "single-cell left-to-right"
            ):
                AnsiRenderer("x", rain_chars=unsafe)
        fallback = AnsiRenderer("x", color=False, half_block=True)
        self.assertEqual(fallback.requested_render_mode, "half-block")
        self.assertEqual(fallback.render_mode, "chars")
        self.assertEqual(fallback.effective_render_mode, "chars")
        with self.assertRaises(ValueError):
            AnsiRenderer("x").render(np.zeros((2, 2), dtype=np.uint8))
        with self.assertRaises(ValueError):
            AnsiRenderer("x", half_block=True).render(
                np.zeros((3, 2, 3), dtype=np.uint8)
            )


if __name__ == "__main__":
    unittest.main()

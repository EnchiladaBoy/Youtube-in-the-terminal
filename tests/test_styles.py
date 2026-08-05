import unittest

import numpy as np

from yt_ascii_effects import EFFECT_NAMES
from yt_ascii_renderer import AnsiRenderer, BG
from yt_ascii_styles import (
    STYLE_ALIASES,
    STYLE_NAMES,
    STYLE_SPECS,
    StyleProcessor,
    StyleSpec,
)


class StyleProcessorTests(unittest.TestCase):
    def setUp(self):
        rows, cols = 12, 20
        y, x = np.indices((rows, cols), dtype=np.uint16)
        self.frame = np.stack(
            (
                (x * 13 + y * 3) & 255,
                (y * 21 + x * 5) & 255,
                ((x + y) * 9) & 255,
            ),
            axis=2,
        ).astype(np.uint8)

    def test_registry_is_static_treatments_only(self):
        self.assertEqual(
            STYLE_NAMES,
            (
                "classic",
                "bayer",
                "posterize",
                "contour",
                "edge-glow",
                "ordered-dither",
                "error-diffusion",
                "duotone",
                "two-tone",
                "riso",
            ),
        )
        self.assertEqual(dict(STYLE_ALIASES), {})
        self.assertEqual(tuple(spec.name for spec in STYLE_SPECS), STYLE_NAMES)
        self.assertTrue(all(isinstance(spec, StyleSpec) for spec in STYLE_SPECS))
        self.assertNotIn("glitch", STYLE_NAMES)
        self.assertNotIn("wave", STYLE_NAMES)
        self.assertNotIn("trails", STYLE_NAMES)
        self.assertTrue(set(STYLE_NAMES).isdisjoint(EFFECT_NAMES))

    def test_select_cycle_and_wrap(self):
        processor = StyleProcessor("bayer")
        self.assertEqual(processor.name, "bayer")
        seen = [processor.select("classic")]
        for _ in range(len(STYLE_NAMES)):
            seen.append(processor.cycle())
        self.assertEqual(tuple(seen[:-1]), STYLE_NAMES)
        self.assertEqual(seen[-1], "classic")
        self.assertEqual(StyleProcessor("contour").name, "contour")

    def test_invalid_names_and_frames_are_rejected(self):
        for name in ("missing", "glitch", "wave"):
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError, "unknown style"
            ):
                StyleProcessor(name)
        processor = StyleProcessor()
        with self.assertRaises(TypeError):
            processor.apply([[[0, 0, 0]]])
        with self.assertRaises(ValueError):
            processor.apply(np.zeros((1, 1, 3), dtype=np.float32))
        for invalid in (
            np.zeros((1, 1), dtype=np.uint8),
            np.zeros((1, 1, 4), dtype=np.uint8),
        ):
            with self.subTest(shape=invalid.shape), self.assertRaises(ValueError):
                processor.apply(invalid)

    def test_classic_is_zero_copy(self):
        processor = StyleProcessor("classic")
        self.assertIs(processor.apply(self.frame), self.frame)
        noncontiguous = self.frame[:, ::2]
        self.assertIs(processor.apply(noncontiguous), noncontiguous)

    def test_every_treatment_is_deterministic_distinct_and_nonmutating(self):
        payloads = {}
        for name in STYLE_NAMES:
            with self.subTest(style=name):
                original = self.frame.copy()
                processor = StyleProcessor(name)
                first = processor.apply(self.frame)
                repeat = StyleProcessor(name).apply(self.frame)
                np.testing.assert_array_equal(self.frame, original)
                np.testing.assert_array_equal(first, repeat)
                self.assertEqual(first.shape, self.frame.shape)
                self.assertEqual(first.dtype, np.uint8)
                if name != "classic":
                    self.assertIsNot(first, self.frame)
                    self.assertFalse(np.array_equal(first, self.frame))
                payload = first.tobytes()
                self.assertNotIn(payload, payloads)
                payloads[payload] = name

    def test_empty_and_single_pixel_frames(self):
        frames = (
            np.zeros((0, 0, 3), dtype=np.uint8),
            np.array([[[123, 45, 67]]], dtype=np.uint8),
        )
        for name in STYLE_NAMES:
            for frame in frames:
                with self.subTest(style=name, shape=frame.shape):
                    output = StyleProcessor(name).apply(frame)
                    self.assertEqual(output.shape, frame.shape)
                    self.assertEqual(output.dtype, np.uint8)

    def test_posterize_has_five_exact_channel_levels(self):
        values = np.arange(256, dtype=np.uint8)
        frame = np.repeat(values[None, :, None], 3, axis=2)
        output = StyleProcessor("posterize").apply(frame)
        self.assertEqual(
            set(np.unique(output).tolist()), {0, 64, 128, 191, 255}
        )

    def test_edge_glow_detects_contours(self):
        frame = np.zeros((9, 9, 3), dtype=np.uint8)
        frame[:, 5:] = 255
        output = StyleProcessor("edge-glow").apply(frame)
        self.assertTrue(np.any(output[:, 4:6, 1] > 0))
        self.assertGreater(int(output[:, 4:6].sum()), int(output[:, :2].sum()))

    def test_ordered_dither_uses_four_rgb_levels(self):
        frame = np.full((8, 8, 3), 128, dtype=np.uint8)
        output = StyleProcessor("ordered-dither").apply(frame)
        self.assertTrue(np.all(output[:, :, 0] == output[:, :, 1]))
        self.assertLessEqual(set(np.unique(output).tolist()), {0, 85, 170, 255})

    def test_error_diffusion_conserves_tone_and_endpoints(self):
        black = np.zeros((5, 7, 3), dtype=np.uint8)
        white = np.full_like(black, 255)
        self.assertFalse(StyleProcessor("error-diffusion").apply(black).any())
        self.assertTrue(
            np.all(StyleProcessor("error-diffusion").apply(white) == 255)
        )
        gray = np.full((5, 7, 3), 127, dtype=np.uint8)
        output = StyleProcessor("error-diffusion").apply(gray)
        expected_marks = gray.shape[0] * gray.shape[1] * 127 // 255
        for channel in range(3):
            self.assertEqual(
                int(np.count_nonzero(output[:, :, channel])), expected_marks
            )

    def test_duotone_is_a_smooth_color_grade(self):
        values = np.array((0, 128, 255), dtype=np.uint8)
        frame = np.repeat(values[None, :, None], 3, axis=2)
        output = StyleProcessor("duotone").apply(frame)
        np.testing.assert_array_equal(output[0, 0], (15, 18, 42))
        np.testing.assert_array_equal(output[0, 2], (255, 196, 92))
        self.assertTrue(np.all(output[0, 1] > output[0, 0]))
        self.assertTrue(np.all(output[0, 1] < output[0, 2]))

    def test_two_tone_is_an_exact_threshold_palette(self):
        values = np.array((0, 127, 128, 255), dtype=np.uint8)
        frame = np.repeat(values[None, :, None], 3, axis=2)
        output = StyleProcessor("two-tone").apply(frame)
        np.testing.assert_array_equal(output[0, 0], (10, 18, 44))
        np.testing.assert_array_equal(output[0, 1], (10, 18, 44))
        np.testing.assert_array_equal(output[0, 2], (255, 184, 76))
        np.testing.assert_array_equal(output[0, 3], (255, 184, 76))

    def test_riso_uses_only_two_inks_and_their_overlap(self):
        colors = {
            (0, 0, 0),
            (238, 61, 52),
            (45, 103, 210),
            (188, 63, 145),
        }
        output = StyleProcessor("riso").apply(self.frame)
        self.assertTrue({tuple(pixel) for pixel in output.reshape(-1, 3)} <= colors)

    def test_every_style_composes_with_all_render_and_reveal_modes(self):
        renderer_options = (
            {"color": True, "render_mode": "chars"},
            {"color": False, "render_mode": "chars"},
            {"color": True, "render_mode": "cells"},
            {"color": False, "render_mode": "cells"},
            {"color": True, "render_mode": "half-block"},
        )
        for style in STYLE_NAMES:
            styled = StyleProcessor(style).apply(self.frame)
            for options in renderer_options:
                with self.subTest(style=style, options=options):
                    renderer = AnsiRenderer(
                        " .#", rain_chars="01", rng=np.random.default_rng(7),
                        **options,
                    )
                    outputs = (
                        renderer.render(styled),
                        renderer.render_scatter(styled, 0.5),
                        renderer.render_rain(styled, 0.5),
                    )
                    self.assertTrue(all(isinstance(output, bytes) for output in outputs))
                    self.assertTrue(all(output for output in outputs))

    def test_every_nonidentity_style_materially_changes_cells_output(self):
        baseline = AnsiRenderer(render_mode="cells").render(self.frame)
        for style in STYLE_NAMES[1:]:
            with self.subTest(style=style):
                output = AnsiRenderer(render_mode="cells").render(
                    StyleProcessor(style).apply(self.frame)
                )
                self.assertNotEqual(output, baseline)
                self.assertIn(BG, output)
                output.decode("ascii")


if __name__ == "__main__":
    unittest.main()

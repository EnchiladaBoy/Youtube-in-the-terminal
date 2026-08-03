import unittest

import numpy as np

from yt_ascii_renderer import AnsiRenderer
from yt_ascii_styles import STYLE_NAMES, StyleProcessor


class StyleProcessorTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.array(
            [
                [[0, 0, 0], [32, 64, 96], [255, 0, 0], [255, 255, 255]],
                [[0, 255, 0], [0, 0, 255], [190, 120, 20], [11, 22, 33]],
                [[40, 80, 120], [80, 120, 160], [120, 160, 200], [160, 200, 240]],
                [[1, 2, 3], [99, 100, 101], [200, 201, 202], [253, 254, 255]],
            ],
            dtype=np.uint8,
        )

    def test_registry_order_select_cycle_and_wrap(self):
        self.assertEqual(
            STYLE_NAMES,
            ("classic", "bayer", "duotone", "riso", "contour", "glitch"),
        )
        processor = StyleProcessor()
        seen = [processor.name]
        for _ in range(len(STYLE_NAMES)):
            seen.append(processor.cycle())
        self.assertEqual(tuple(seen[:-1]), STYLE_NAMES)
        self.assertEqual(seen[-1], "classic")
        self.assertEqual(processor.select("contour"), "contour")
        self.assertEqual(processor.name, "contour")
        processor.reset()
        self.assertEqual(processor.name, "contour")

    def test_invalid_names_and_frames_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown style"):
            StyleProcessor("missing")
        processor = StyleProcessor()
        with self.assertRaisesRegex(ValueError, "unknown style"):
            processor.select("missing")
        self.assertEqual(processor.name, "classic")
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
        for invalid_time in (None, "later", float("inf"), float("nan")):
            with self.subTest(time=invalid_time), self.assertRaises(ValueError):
                processor.apply(self.frame, invalid_time)

    def test_classic_is_zero_copy(self):
        processor = StyleProcessor("classic")
        self.assertIs(processor.apply(self.frame), self.frame)
        noncontiguous = self.frame[:, ::2]
        self.assertIs(processor.apply(noncontiguous), noncontiguous)

    def test_every_transform_preserves_contract_without_mutating_input(self):
        for name in STYLE_NAMES:
            with self.subTest(style=name):
                original = self.frame.copy()
                output = StyleProcessor(name).apply(self.frame, 3.25)
                np.testing.assert_array_equal(self.frame, original)
                self.assertEqual(output.shape, self.frame.shape)
                self.assertEqual(output.dtype, np.uint8)
                if name != "classic":
                    self.assertIsNot(output, self.frame)

    def test_empty_and_single_pixel_frames(self):
        frames = (
            np.zeros((0, 0, 3), dtype=np.uint8),
            np.array([[[123, 45, 67]]], dtype=np.uint8),
        )
        for name in STYLE_NAMES:
            for frame in frames:
                with self.subTest(style=name, shape=frame.shape):
                    output = StyleProcessor(name).apply(frame, 1.0)
                    self.assertEqual(output.shape, frame.shape)
                    self.assertEqual(output.dtype, np.uint8)

    def test_bayer_has_four_brightness_levels_and_keeps_endpoints(self):
        ramp = np.arange(256, dtype=np.uint8).reshape(16, 16)
        frame = np.repeat(ramp[:, :, None], 3, axis=2)
        output = StyleProcessor("bayer").apply(frame)
        self.assertTrue(set(np.unique(output)).issubset({0, 85, 170, 255}))
        self.assertEqual(tuple(output[0, 0]), (0, 0, 0))
        self.assertEqual(tuple(output[-1, -1]), (255, 255, 255))

    def test_bayer_scales_color_channels_together(self):
        frame = np.full((4, 4, 3), (30, 60, 90), dtype=np.uint8)
        output = StyleProcessor("bayer").apply(frame)
        colored = output[np.all(output > 0, axis=2)]
        self.assertGreater(len(colored), 0)
        for pixel in colored:
            self.assertAlmostEqual(pixel[1] / pixel[0], 2.0, delta=0.08)
            self.assertAlmostEqual(pixel[2] / pixel[0], 3.0, delta=0.12)

    def test_bayer_retains_saturated_hues_without_gamut_clipping(self):
        rng = np.random.default_rng(11)
        frame = rng.integers(1, 256, size=(12, 12, 3), dtype=np.uint8)
        output = StyleProcessor("bayer").apply(frame)
        brightness = output.max(axis=2)
        self.assertTrue(set(np.unique(brightness)) <= {0, 85, 170, 255})

        visible = brightness > 0
        source_ratios = frame[visible] / frame[visible].max(axis=1, keepdims=True)
        output_ratios = output[visible] / output[visible].max(axis=1, keepdims=True)
        np.testing.assert_allclose(output_ratios, source_ratios, atol=0.007)

    def test_duotone_endpoints_and_smooth_midpoint(self):
        frame = np.array([[[0, 0, 0], [128, 128, 128], [255, 255, 255]]], dtype=np.uint8)
        output = StyleProcessor("duotone").apply(frame)
        np.testing.assert_array_equal(output[0, 0], (15, 18, 42))
        np.testing.assert_array_equal(output[0, 2], (255, 196, 92))
        self.assertTrue(np.all(output[0, 1] > output[0, 0]))
        self.assertTrue(np.all(output[0, 1] < output[0, 2]))

    def test_riso_uses_only_the_two_inks_and_overlap(self):
        colors = {
            (0, 0, 0),
            (238, 61, 52),
            (45, 103, 210),
            (188, 63, 145),
        }
        frame = np.full((4, 4, 3), 255, dtype=np.uint8)
        output = StyleProcessor("riso").apply(frame)
        self.assertEqual({tuple(pixel) for pixel in output.reshape(-1, 3)}, {(188, 63, 145)})
        mixed = StyleProcessor("riso").apply(self.frame)
        self.assertTrue({tuple(pixel) for pixel in mixed.reshape(-1, 3)} <= colors)

    def test_contour_suppresses_flat_areas_and_finds_an_edge(self):
        processor = StyleProcessor("contour")
        flat = np.full((7, 7, 3), 80, dtype=np.uint8)
        self.assertFalse(processor.apply(flat).any())
        edge = np.zeros((7, 7, 3), dtype=np.uint8)
        edge[:, 4:] = 255
        output = processor.apply(edge)
        self.assertTrue(output[:, 3:5].any())
        self.assertFalse(output[:, :2].any())
        active = output[np.any(output > 0, axis=2)]
        self.assertTrue(np.all(active[:, 0] <= 120))
        self.assertTrue(np.all(active[:, 1] <= 245))
        self.assertTrue(np.all(active[:, 2] <= 225))

    def test_glitch_is_deterministic_at_eight_hertz(self):
        frame = np.arange(12 * 20 * 3, dtype=np.uint8).reshape(12, 20, 3)
        processor = StyleProcessor("glitch")
        first = processor.apply(frame, 1.0)
        repeat = processor.apply(frame, 1.0)
        same_tick = processor.apply(frame, 1.124)
        next_tick = processor.apply(frame, 1.125)
        np.testing.assert_array_equal(first, repeat)
        np.testing.assert_array_equal(first, same_tick)
        self.assertFalse(np.array_equal(first, next_tick))
        processor.reset()
        np.testing.assert_array_equal(first, processor.apply(frame, 1.0))
        self.assertFalse(np.array_equal(first, frame))

    def test_glitch_dims_every_fourth_scanline(self):
        frame = np.full((8, 4, 3), 200, dtype=np.uint8)
        output = StyleProcessor("glitch").apply(frame, 0.0)
        np.testing.assert_array_equal(output[3], np.full((4, 3), 150, dtype=np.uint8))
        np.testing.assert_array_equal(output[7], np.full((4, 3), 150, dtype=np.uint8))

    def test_every_style_composes_with_all_render_and_reveal_modes(self):
        frame = np.arange(4 * 6 * 3, dtype=np.uint8).reshape(4, 6, 3)
        renderer_options = (
            {"color": True},
            {"color": False},
            {"color": True, "half_block": True},
        )
        for style in STYLE_NAMES:
            styled = StyleProcessor(style).apply(frame, 2.5)
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


if __name__ == "__main__":
    unittest.main()

import math
from types import MappingProxyType
import unicodedata
import unittest
from unittest import mock

import numpy as np

import yt_ascii_backends as backends
import yt_ascii_effects as effects
from yt_ascii_effects import (
    DEFAULT_EFFECT_TEXT,
    EFFECT_ALIASES,
    EFFECT_NAMES,
    EFFECT_SPECS,
    GLYPH_EFFECT_NAMES,
    GRAPHICAL_EFFECT_NAMES,
    RENDER_MODES,
    STATEFUL_EFFECT_NAMES,
    TEXT_EFFECT_NAMES,
    EffectProcessor,
    EffectSpec,
    effect_names_for_renderer,
)
from yt_ascii_frames import CellPlane, EffectContext, EffectFrame
from yt_ascii_renderer import AnsiRenderer, BG, HALF_BLOCK


def context(time=0.0, sequence=0, shape=(12, 20), *, render="chars", advance=True):
    return EffectContext(
        time, sequence, shape, render_mode=render, advance_state=advance
    )


class FrameContractTests(unittest.TestCase):
    def test_effect_context_uses_canonical_render_mode(self):
        value = EffectContext(
            video_time="1.25",
            frame_sequence=7,
            cell_shape=(3, 5),
            render_mode="cells",
            advance_state=False,
        )
        self.assertEqual(value.video_time, 1.25)
        self.assertEqual(value.render_mode, "cells")
        self.assertFalse(value.advance_state)

    def test_effect_context_rejects_invalid_metadata_and_conflicts(self):
        for video_time in (None, "later", math.inf, -math.inf, math.nan):
            with self.subTest(video_time=video_time), self.assertRaises(ValueError):
                EffectContext(video_time, 0, (1, 1))
        for sequence in (True, False, -1, 1.5, "1", np.int64(1)):
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                EffectContext(0.0, sequence, (1, 1))
        for shape in ([1, 2], (), (1,), (0, 1), (1.5, 2), (True, 2)):
            with self.subTest(shape=shape), self.assertRaises(ValueError):
                EffectContext(0.0, 0, shape)
        for render_mode in (None, "pixels", "kitty", ""):
            with self.subTest(render_mode=render_mode), self.assertRaises(
                (TypeError, ValueError)
            ):
                EffectContext(0.0, 0, (1, 1), render_mode=render_mode)
        with self.assertRaises(TypeError):
            EffectContext(0.0, 0, (1, 1), advance_state=1)

    def test_cell_plane_and_effect_frame_validate_array_contracts(self):
        indices = np.array([[0, 1], [1, 0]], dtype=np.uint16)
        foreground = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)
        plane = CellPlane(indices, " .", foreground)
        rgb = np.zeros((2, 2, 3), dtype=np.uint8)
        output = EffectFrame(rgb, plane)
        self.assertIs(output.rgb, rgb)
        self.assertIs(output.cells, plane)

        invalid_indices = (
            [[0]],
            np.zeros((1, 1, 1), dtype=np.uint8),
            np.zeros((1, 1), dtype=np.float32),
            np.array([[-1]], dtype=np.int16),
            np.array([[2]], dtype=np.uint8),
        )
        for value in invalid_indices:
            with self.subTest(value=repr(value)), self.assertRaises(
                (TypeError, ValueError)
            ):
                CellPlane(value, " .")
        for glyphs in ("", " \x00", " \n", " \u0301", " 界"):
            with self.subTest(glyphs=repr(glyphs)), self.assertRaises(ValueError):
                CellPlane(np.zeros((1, 1), dtype=np.uint8), glyphs)
        with self.assertRaises(ValueError):
            CellPlane(indices, " .", foreground.astype(np.float32))
        with self.assertRaises(ValueError):
            CellPlane(indices, " .", np.zeros((1, 2, 3), dtype=np.uint8))
        with self.assertRaises(TypeError):
            EffectFrame(rgb, object())
        with self.assertRaises(ValueError):
            EffectFrame(rgb.astype(np.float32))


class EffectProcessorTests(unittest.TestCase):
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

    def test_registry_is_small_graphical_and_explicit(self):
        self.assertEqual(
            EFFECT_NAMES,
            (
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
            ),
        )
        self.assertEqual(RENDER_MODES, ("chars", "cells", "half-block"))
        self.assertEqual(TEXT_EFFECT_NAMES, {"digital-rain", "terminal-hud"})
        self.assertEqual(GLYPH_EFFECT_NAMES, TEXT_EFFECT_NAMES)
        self.assertEqual(STATEFUL_EFFECT_NAMES, {"trails"})
        self.assertEqual(
            GRAPHICAL_EFFECT_NAMES,
            set(EFFECT_NAMES) - set(TEXT_EFFECT_NAMES) - {"none"},
        )
        self.assertEqual(tuple(spec.name for spec in EFFECT_SPECS), EFFECT_NAMES)
        self.assertEqual(len(EFFECT_NAMES), len(set(EFFECT_NAMES)))
        for spec in EFFECT_SPECS:
            with self.subTest(effect=spec.name):
                self.assertIsInstance(spec, EffectSpec)
                self.assertIn(spec.kind, ("identity", "graphical", "text"))
                self.assertTrue(spec.category)
                self.assertEqual(
                    spec.glyph_owned, spec.name in TEXT_EFFECT_NAMES
                )
                self.assertEqual(
                    spec.stateful, spec.name in STATEFUL_EFFECT_NAMES
                )
                self.assertEqual(EffectProcessor(spec.name).spec, spec)

    def test_graphical_and_text_renderer_compatibility_is_metadata_driven(self):
        expected_graphical = EFFECT_NAMES[:-2]
        self.assertEqual(effect_names_for_renderer("cells"), expected_graphical)
        self.assertEqual(
            effect_names_for_renderer("half-block"), expected_graphical
        )
        self.assertEqual(effect_names_for_renderer("chars"), EFFECT_NAMES)
        for spec in EFFECT_SPECS:
            expected = (
                ("chars",)
                if spec.name in TEXT_EFFECT_NAMES
                else RENDER_MODES
            )
            self.assertEqual(spec.compatible_renderers, expected)
        for mode in (None, "", "pixels", "kitty"):
            with self.subTest(mode=mode), self.assertRaises((TypeError, ValueError)):
                effect_names_for_renderer(mode)

    def test_future_rgb_backend_needs_no_effect_registry_changes(self):
        kitty = backends.RenderBackendSpec(
            name="kitty",
            protocol="kitty",
            source_rows_per_cell=1,
            unicode_dependent=False,
            supports_cell_plane=False,
            requires_color=True,
            portable=False,
        )
        registry = MappingProxyType({**backends.RENDER_BACKENDS, "kitty": kitty})
        with mock.patch.object(backends, "RENDER_BACKENDS", registry):
            compatible = effect_names_for_renderer("kitty")
            self.assertEqual(compatible, EFFECT_NAMES[:-2])
            self.assertNotIn("digital-rain", compatible)
            value = EffectContext(0.0, 0, (12, 20), render_mode="kitty")
            output = EffectProcessor("wave").apply(self.frame, value)
            self.assertIsNone(output.cells)

    def test_legacy_aliases_are_canonicalized_but_never_cycled(self):
        self.assertEqual(
            dict(EFFECT_ALIASES),
            {
                "tile-mosaic": "pixelate",
                "wave-lines": "wave",
                "afterimage": "trails",
                "hologram": "crt",
            },
        )
        self.assertFalse(set(EFFECT_ALIASES) & set(EFFECT_NAMES))
        for alias, canonical in EFFECT_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertEqual(EffectProcessor(alias).name, canonical)
        processor = EffectProcessor()
        cycled = []
        for _ in EFFECT_NAMES:
            cycled.append(processor.name)
            processor.cycle()
        self.assertEqual(tuple(cycled), EFFECT_NAMES)

    def test_retired_glyph_variations_are_not_selectable(self):
        retired = (
            "contour-glyph",
            "number-field",
            "glyph-grid",
            "word-field",
            "inscription",
            "type-echo",
            "type-collage",
            "geometry",
            "hatch",
            "dotfield",
            "vector-field",
            "halftone",
            "cross-stitch",
            "weave",
            "kilim",
            "stardust",
            "engraving",
            "brickwork",
            # Static treatments now belong exclusively to --style.
            "posterize",
            "edge-glow",
            "ordered-dither",
            "error-diffusion",
            "duotone",
            "poster-press",
        )
        for name in retired:
            with self.subTest(effect=name), self.assertRaisesRegex(
                ValueError, "unknown effect"
            ):
                EffectProcessor(name)

    def test_select_and_cycle_handle_incompatibility_explicitly(self):
        processor = EffectProcessor("prism")
        self.assertEqual(processor.cycle("cells"), "none")
        processor.select("digital-rain")
        self.assertEqual(processor.cycle("cells"), "none")
        processor.select("terminal-hud")
        self.assertEqual(processor.cycle("chars"), "none")

        processor.select("pixelate")
        with self.assertRaisesRegex(ValueError, "not compatible"):
            processor.select("digital-rain", "cells")
        self.assertEqual(processor.name, "pixelate")
        processor.select("terminal-hud")
        with self.assertRaisesRegex(ValueError, "supported render modes: chars"):
            processor.ensure_compatible("half-block")
        self.assertEqual(processor.ensure_compatible("chars"), "terminal-hud")

    def test_configuration_validation(self):
        self.assertEqual(DEFAULT_EFFECT_TEXT, "YTASCII")
        with self.assertRaisesRegex(ValueError, "unknown effect"):
            EffectProcessor("missing")
        for mode in ("", "wide", None):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                EffectProcessor(glyph_mode=mode)
        for speed in (0, -1, math.inf, -math.inf, math.nan, "fast", None):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                EffectProcessor(speed=speed)
        for seed in (True, 1.5, "1", None):
            with self.subTest(seed=seed), self.assertRaises(TypeError):
                EffectProcessor(seed=seed)
        self.assertEqual(EffectProcessor(seed=np.int64(-9)).seed, -9)

    def test_effect_text_compatibility_option_remains_safely_validated(self):
        accepted = (("A B", "ascii"), ("λ", "unicode"), ("é", "unicode"))
        for value, mode in accepted:
            processor = EffectProcessor(effect_text=value, glyph_mode=mode)
            self.assertEqual(processor.effect_text, value)
        with self.assertRaises(TypeError):
            EffectProcessor(effect_text=123)
        for value in ("", "   ", "A\tB", "A\nB", "e\u0301", "界", "א"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                EffectProcessor(effect_text=value, glyph_mode="unicode")
        with self.assertRaises(ValueError):
            EffectProcessor(effect_text="λ", glyph_mode="ascii")

    def test_frame_and_time_validation(self):
        processor = EffectProcessor()
        with self.assertRaises(TypeError):
            processor.apply([[[0, 0, 0]]])
        with self.assertRaises(ValueError):
            processor.apply(np.zeros((1, 1, 3), dtype=np.float32))
        for bad in (
            np.zeros((1, 1), dtype=np.uint8),
            np.zeros((1, 1, 4), dtype=np.uint8),
        ):
            with self.subTest(shape=bad.shape), self.assertRaises(ValueError):
                processor.apply(bad)
        for value in (None, "later", math.inf, -math.inf, math.nan):
            with self.subTest(time=value), self.assertRaises(ValueError):
                processor.apply(self.frame, value)

    def test_none_is_zero_copy(self):
        output = EffectProcessor("none").apply(self.frame, 3.25)
        self.assertIsInstance(output, EffectFrame)
        self.assertIs(output.rgb, self.frame)
        self.assertIsNone(output.cells)

    def test_all_effects_preserve_input_and_return_valid_contracts(self):
        for name in EFFECT_NAMES:
            with self.subTest(effect=name):
                original = self.frame.copy()
                output = EffectProcessor(name, seed=11).apply(
                    self.frame, context(0.25, 7, (6, 20))
                )
                np.testing.assert_array_equal(self.frame, original)
                self.assertIsInstance(output, EffectFrame)
                self.assertEqual(output.rgb.shape, self.frame.shape)
                self.assertEqual(output.rgb.dtype, np.uint8)
                if name in TEXT_EFFECT_NAMES:
                    self.assertIs(output.rgb, self.frame)
                    self.assertIsInstance(output.cells, CellPlane)
                    self.assertEqual(output.cells.glyph_indices.shape, (6, 20))
                    self.assertEqual(output.cells.fg_rgb.shape, (6, 20, 3))
                else:
                    self.assertIsNone(output.cells)

    def test_graphical_effects_are_deterministic_and_materially_transform_rgb(self):
        for name in GRAPHICAL_EFFECT_NAMES - {"trails"}:
            with self.subTest(effect=name):
                first = EffectProcessor(name, seed=17).apply(
                    self.frame, 0.375
                ).rgb
                repeat = EffectProcessor(name, seed=17).apply(
                    self.frame, 0.375
                ).rgb
                np.testing.assert_array_equal(first, repeat)
                self.assertFalse(np.array_equal(first, self.frame))
                self.assertIsNone(
                    EffectProcessor(name, seed=17).apply(self.frame).cells
                )

        moving = np.zeros_like(self.frame)
        moving[:, 5:10] = 255
        gone = np.zeros_like(moving)
        processor = EffectProcessor("trails")
        processor.apply(moving, context(0.0, 0))
        trailed = processor.apply(gone, context(0.2, 1)).rgb
        self.assertFalse(np.array_equal(trailed, gone))

    def test_every_effect_composes_with_every_declared_backend(self):
        for spec in EFFECT_SPECS:
            for render_mode in spec.compatible_renderers:
                with self.subTest(effect=spec.name, render_mode=render_mode):
                    source = (
                        np.repeat(self.frame, 2, axis=0)
                        if render_mode == "half-block"
                        else self.frame
                    )
                    effected = EffectProcessor(spec.name, seed=13).apply(
                        source,
                        context(
                            0.25,
                            0,
                            self.frame.shape[:2],
                            render=render_mode,
                        ),
                    )
                    output = AnsiRenderer(
                        " .#", render_mode=render_mode
                    ).render(effected.rgb, effected.cells)
                    self.assertTrue(output)
                    self.assertNotIn(b"\x00", output)
                    if render_mode == "cells":
                        self.assertIn(BG, output)
                        self.assertNotIn(HALF_BLOCK, output)
                        output.decode("ascii")

    def test_graphical_effects_materially_change_cells_frames(self):
        baseline = AnsiRenderer(render_mode="cells").render(self.frame)
        for name in GRAPHICAL_EFFECT_NAMES - {"none", "trails"}:
            with self.subTest(effect=name):
                effected = EffectProcessor(name, seed=17).apply(
                    self.frame, context(0.375, render="cells")
                )
                output = AnsiRenderer(render_mode="cells").render(
                    effected.rgb
                )
                self.assertNotEqual(output, baseline)
                self.assertIn(BG, output)
                output.decode("ascii")

    def test_retained_effects_produce_distinct_deterministic_payloads(self):
        payloads = {}
        for name in EFFECT_NAMES:
            processor = EffectProcessor(name, seed=23)
            if name == "trails":
                processor.apply(self.frame, context(0.0, 0))
                frame = np.roll(self.frame, 3, axis=1)
                output = processor.apply(frame, context(0.2, 1))
            else:
                output = processor.apply(self.frame, 0.25)
            payload = output.rgb.tobytes()
            if output.cells is not None:
                payload += output.cells.glyphs.encode("utf-8")
                payload += output.cells.glyph_indices.tobytes()
                payload += output.cells.fg_rgb.tobytes()
            with self.subTest(effect=name):
                self.assertNotIn(payload, payloads)
            payloads[payload] = name

    def test_alias_outputs_match_canonical_effects(self):
        for alias, canonical in EFFECT_ALIASES.items():
            with self.subTest(alias=alias):
                left = EffectProcessor(alias, seed=7).apply(self.frame, 0.25)
                right = EffectProcessor(canonical, seed=7).apply(
                    self.frame, 0.25
                )
                np.testing.assert_array_equal(left.rgb, right.rgb)
                self.assertEqual(left.cells, right.cells)

    def test_empty_and_tiny_frames_are_safe(self):
        frames = (
            np.zeros((0, 0, 3), dtype=np.uint8),
            np.array([[[19, 127, 241]]], dtype=np.uint8),
        )
        for name in EFFECT_NAMES:
            for frame in frames:
                with self.subTest(effect=name, shape=frame.shape):
                    output = EffectProcessor(name, seed=-3).apply(frame, 0.5)
                    self.assertEqual(output.rgb.shape, frame.shape)
                    self.assertEqual(output.rgb.dtype, np.uint8)

    def test_static_and_animated_effect_timing_contracts(self):
        static = (
            "pixelate",
            "chromatic-shift",
            "prism",
        )
        for name in static:
            with self.subTest(effect=name):
                slow = EffectProcessor(name, speed=0.25, seed=-19).apply(
                    self.frame, -1e200
                ).rgb
                fast = EffectProcessor(name, speed=8.0, seed=-19).apply(
                    self.frame, 1e200
                ).rgb
                np.testing.assert_array_equal(slow, fast)
        rates = {
            "glitch": 8,
            "crt": 30,
            "wave": 12,
            "digital-rain": 12,
            "terminal-hud": 10,
        }
        for name, rate in rates.items():
            with self.subTest(effect=name):
                processor = EffectProcessor(name, seed=7)
                first = processor.apply(self.frame, 0.0)
                same = processor.apply(self.frame, 1 / rate - 1e-9)
                changed = processor.apply(self.frame, 1 / rate)
                self.assertEqual(self._payload(first), self._payload(same))
                self.assertNotEqual(self._payload(first), self._payload(changed))

    @staticmethod
    def _payload(output):
        result = output.rgb.tobytes()
        if output.cells is not None:
            result += output.cells.glyph_indices.tobytes()
            result += output.cells.fg_rgb.tobytes()
        return result

    def test_pixelate_uses_exact_integer_tile_averages(self):
        frame = np.arange(5 * 7 * 3, dtype=np.uint8).reshape(5, 7, 3)
        output = EffectProcessor("pixelate").apply(frame).rgb
        expected = frame[:4, :6].astype(np.uint32).sum(axis=(0, 1)) // 24
        np.testing.assert_array_equal(output[0, 0], expected)
        np.testing.assert_array_equal(
            output[:4, :6], np.broadcast_to(expected, (4, 6, 3))
        )
        np.testing.assert_array_equal(output[4, 6], frame[4, 6])

    def test_crt_uses_scanlines_and_rgb_shadow_mask(self):
        frame = np.full((8, 9, 3), 200, dtype=np.uint8)
        output = EffectProcessor("crt", seed=0).apply(frame, 0.0).rgb
        self.assertGreater(len(np.unique(output[:, :, 0])), 3)
        self.assertFalse(np.array_equal(output[:, :, 0], output[:, :, 1]))
        next_tick = EffectProcessor("crt", seed=0).apply(frame, 1 / 30).rgb
        self.assertFalse(np.array_equal(output, next_tick))

    def test_glitch_displaces_bands_and_changes_at_eight_hertz(self):
        frame = np.arange(12 * 20 * 3, dtype=np.uint8).reshape(12, 20, 3)
        first = EffectProcessor("glitch", seed=4).apply(frame, 1.0).rgb
        repeat = EffectProcessor("glitch", seed=4).apply(frame, 1.124).rgb
        next_tick = EffectProcessor("glitch", seed=4).apply(frame, 1.125).rgb
        np.testing.assert_array_equal(first, repeat)
        self.assertFalse(np.array_equal(first, next_tick))
        self.assertFalse(np.array_equal(first, frame))

    def test_chromatic_shift_displaces_channels_without_unicode(self):
        frame = np.zeros((4, 8, 3), dtype=np.uint8)
        frame[:, 3] = (60, 120, 240)
        output = EffectProcessor("chromatic-shift", seed=0).apply(frame)
        self.assertIsNone(output.cells)
        self.assertFalse(np.array_equal(output.rgb[:, :, 0], frame[:, :, 0]))
        self.assertFalse(np.array_equal(output.rgb[:, :, 2], frame[:, :, 2]))
        self.assertFalse(
            np.array_equal(output.rgb[:, :, 0], output.rgb[:, :, 2])
        )

    def test_wave_geometrically_resamples_source(self):
        frame = np.arange(24 * 32 * 3, dtype=np.uint8).reshape(24, 32, 3)
        first = EffectProcessor("wave", seed=4).apply(frame, 1.0).rgb
        repeat = EffectProcessor("wave", seed=4).apply(frame, 1.0).rgb
        fast = EffectProcessor("wave", speed=2.0, seed=4).apply(
            frame, 0.5
        ).rgb
        np.testing.assert_array_equal(first, repeat)
        np.testing.assert_array_equal(first, fast)
        self.assertFalse(np.array_equal(first, frame))

    def test_trails_are_stateful_bounded_idempotent_and_resettable(self):
        bright = np.zeros((5, 7, 3), dtype=np.uint8)
        bright[2, 2] = (240, 180, 120)
        dark = np.zeros_like(bright)
        processor = EffectProcessor("trails")
        np.testing.assert_array_equal(
            processor.apply(bright, context(0.0, 0, (5, 7))).rgb, bright
        )
        trail = processor.apply(dark, context(0.325, 1, (5, 7))).rgb
        self.assertTrue(np.all(trail[2, 2] > 0))
        before = processor._trail_accumulator.copy()
        held = processor.apply(
            dark, context(0.325, 1, (5, 7), advance=False)
        ).rgb
        np.testing.assert_array_equal(processor._trail_accumulator, before)
        np.testing.assert_array_equal(held, trail)
        processor.reset("seek")
        np.testing.assert_array_equal(
            processor.apply(dark, context(2.0, 0, (5, 7))).rgb, dark
        )

        # Determinism for a temporal effect means replaying the same complete
        # history, not comparing one isolated call with fresh state.
        histories = []
        for _ in range(2):
            replay = EffectProcessor("trails", speed=1.25, seed=9)
            outputs = []
            for frame, timestamp, sequence in (
                (bright, 0.0, 0),
                (dark, 0.2, 1),
                (np.roll(bright, 1, axis=1), 0.4, 2),
            ):
                outputs.append(
                    replay.apply(
                        frame, context(timestamp, sequence, (5, 7))
                    ).rgb.copy()
                )
            histories.append(outputs)
        for first, repeated in zip(*histories):
            np.testing.assert_array_equal(first, repeated)

    def test_prism_splits_channels_and_accents_edges(self):
        flat = np.empty((4, 5, 3), dtype=np.uint8)
        flat[:] = (30, 90, 150)
        np.testing.assert_array_equal(
            EffectProcessor("prism", seed=3).apply(flat).rgb, flat
        )
        frame = np.zeros((7, 7, 3), dtype=np.uint8)
        frame[:, 4:] = (80, 100, 120)
        output = EffectProcessor("prism", seed=1).apply(frame).rgb
        self.assertFalse(np.array_equal(output, frame))
        self.assertTrue(np.any(output[:, 3:5] > frame[:, 3:5]))

    def test_digital_rain_is_distinctive_analytic_text(self):
        frame = np.empty((6, 4, 3), dtype=np.uint8)
        frame[:] = (120, 100, 80)

        def zero_hash(rows, cols, seed):
            return np.zeros((rows, cols), dtype=np.uint64)

        with mock.patch.object(effects, "_hash_grid", side_effect=zero_hash):
            tick_zero = EffectProcessor("digital-rain").apply(frame, 0.0).cells
            tick_one = EffectProcessor("digital-rain").apply(
                frame, 1 / 12
            ).cells
        self.assertTrue(np.all(tick_zero.glyph_indices[0] == 3))
        self.assertTrue(np.all(tick_one.glyph_indices[1] == 3))
        self.assertGreater(
            int(tick_zero.fg_rgb[0, 0, 1]),
            int(tick_zero.fg_rgb[0, 0, 0]),
        )

    def test_terminal_hud_draws_border_reticle_and_clock(self):
        frame = np.full((9, 20, 3), 128, dtype=np.uint8)
        plane = EffectProcessor("terminal-hud", seed=0).apply(
            frame, 12.3
        ).cells
        glyph_rows = np.array(tuple(plane.glyphs))[plane.glyph_indices]
        self.assertEqual("".join(glyph_rows[0, -7:]), "[00123]")
        self.assertEqual(glyph_rows[4, 10], "+")
        self.assertEqual(glyph_rows[4, 9], "-")
        self.assertEqual(glyph_rows[3, 10], "|")
        self.assertEqual("".join(glyph_rows[-1, 1:10]), "[YTASCII]")

        custom = EffectProcessor(
            "terminal-hud", effect_text="CODEX"
        ).apply(frame, 0.0).cells
        custom_rows = np.array(tuple(custom.glyphs))[custom.glyph_indices]
        self.assertEqual("".join(custom_rows[-1, 1:8]), "[CODEX]")

    def test_text_schemas_are_ascii_by_default_and_safe_when_unicode(self):
        for name in TEXT_EFFECT_NAMES:
            schemas = []
            for mode in ("ascii", "unicode"):
                plane = EffectProcessor(name, glyph_mode=mode).apply(
                    self.frame
                ).cells
                schemas.append(plane.glyphs)
                if mode == "ascii":
                    self.assertTrue(all(ord(glyph) < 128 for glyph in plane.glyphs))
                else:
                    self.assertTrue(any(ord(glyph) >= 128 for glyph in plane.glyphs))
                    for glyph in plane.glyphs:
                        self.assertFalse(unicodedata.combining(glyph))
                        self.assertNotIn(
                            unicodedata.east_asian_width(glyph), ("W", "F")
                        )
                self.assertLess(
                    int(plane.glyph_indices.max(initial=0)), len(plane.glyphs)
                )
            self.assertNotEqual(*schemas)

    def test_text_apply_rejects_non_text_context_when_available(self):
        for effect in TEXT_EFFECT_NAMES:
            for render in ("cells", "half-block"):
                with self.subTest(effect=effect, render=render):
                    with self.assertRaisesRegex(ValueError, "not compatible"):
                        EffectProcessor(effect).apply(
                            self.frame, context(render=render)
                        )


if __name__ == "__main__":
    unittest.main()

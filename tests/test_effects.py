import math
from pathlib import Path
import runpy
import unicodedata
import unittest
from unittest import mock

import numpy as np
import yt_ascii_effects as effects

from yt_ascii_effects import (
    DEFAULT_EFFECT_TEXT,
    EFFECT_NAMES,
    EFFECT_SPECS,
    GLYPH_EFFECT_NAMES,
    STATEFUL_EFFECT_NAMES,
    EffectProcessor,
    EffectSpec,
)
from yt_ascii_frames import CellPlane, EffectContext, EffectFrame
from yt_ascii_renderer import AnsiRenderer


def context(time=0.0, sequence=0, shape=(12, 20), *, advance=True):
    return EffectContext(
        video_time=time,
        frame_sequence=sequence,
        cell_shape=shape,
        advance_state=advance,
    )


class FrameContractTests(unittest.TestCase):
    def test_effect_context_accepts_and_normalizes_valid_metadata(self):
        value = EffectContext(
            video_time="1.25",
            frame_sequence=7,
            cell_shape=(3, 5),
            requested_pixels=True,
            advance_state=False,
        )
        self.assertEqual(value.video_time, 1.25)
        self.assertIs(type(value.frame_sequence), int)
        self.assertEqual(value.cell_shape, (3, 5))
        self.assertIs(value.requested_pixels, True)
        self.assertIs(value.advance_state, False)

    def test_effect_context_rejects_nonfinite_time_and_invalid_sequence(self):
        for video_time in (None, "later", math.inf, -math.inf, math.nan):
            with self.subTest(video_time=video_time), self.assertRaises(ValueError):
                EffectContext(video_time, 0, (1, 1))
        for sequence in (True, False, -1, 1.5, "1", np.int64(1)):
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                EffectContext(0.0, sequence, (1, 1))

    def test_effect_context_rejects_invalid_shape_and_boolean_flags(self):
        for shape in (
            [1, 2],
            (),
            (1,),
            (1, 2, 3),
            (0, 1),
            (-1, 1),
            (1.5, 2),
            (True, 2),
        ):
            with self.subTest(shape=shape), self.assertRaises(ValueError):
                EffectContext(0.0, 0, shape)
        for field, kwargs in (
            ("requested_pixels", {"requested_pixels": 1}),
            ("advance_state", {"advance_state": 0}),
        ):
            with self.subTest(field=field), self.assertRaises(TypeError):
                EffectContext(0.0, 0, (1, 1), **kwargs)

    def test_cell_plane_accepts_integer_indices_and_ambiguous_width_glyphs(self):
        indices = np.array([[0, 1], [1, 0]], dtype=np.uint16)
        foreground = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)
        plane = CellPlane(indices, " ●", foreground)
        self.assertIs(plane.glyph_indices, indices)
        self.assertIs(plane.fg_rgb, foreground)

    def test_cell_plane_rejects_invalid_index_arrays_and_ranges(self):
        invalid = (
            ([[0]], TypeError),
            (np.zeros((1, 1, 1), dtype=np.uint8), ValueError),
            (np.zeros((1, 1), dtype=np.float32), ValueError),
            (np.array([[-1]], dtype=np.int16), ValueError),
            (np.array([[2]], dtype=np.uint8), ValueError),
        )
        for indices, error in invalid:
            with self.subTest(indices=repr(indices)), self.assertRaises(error):
                CellPlane(indices, " .")

    def test_cell_plane_rejects_unsafe_or_invalid_glyph_schemas(self):
        indices = np.zeros((1, 1), dtype=np.uint8)
        invalid = (
            ((), TypeError),
            ("", ValueError),
            ("a" * 257, ValueError),
            (" \x00", ValueError),
            (" \n", ValueError),
            (" \u200d", ValueError),
            (" \u0301", ValueError),
            (" \u2028", ValueError),
            (" \ud800", ValueError),
            (" \u0378", ValueError),
            (" 界", ValueError),
        )
        for glyphs, error in invalid:
            with self.subTest(glyphs=repr(glyphs)), self.assertRaises(error):
                CellPlane(indices, glyphs)

    def test_cell_plane_rejects_foreground_type_dtype_and_shape_mismatches(self):
        indices = np.zeros((2, 3), dtype=np.uint8)
        invalid = (
            ([[[0, 0, 0]]], TypeError),
            (np.zeros((2, 3, 3), dtype=np.float32), ValueError),
            (np.zeros((2, 3), dtype=np.uint8), ValueError),
            (np.zeros((2, 3, 4), dtype=np.uint8), ValueError),
            (np.zeros((3, 2, 3), dtype=np.uint8), ValueError),
        )
        for foreground, error in invalid:
            with self.subTest(shape=getattr(foreground, "shape", None)):
                with self.assertRaises(error):
                    CellPlane(indices, " ", foreground)

    def test_effect_frame_validates_rgb_and_cell_plane_type(self):
        rgb = np.zeros((2, 3, 3), dtype=np.uint8)
        plane = CellPlane(np.zeros((2, 3), dtype=np.uint8), " ")
        output = EffectFrame(rgb, plane)
        self.assertIs(output.rgb, rgb)
        self.assertIs(output.cells, plane)
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

    def test_registry_order_and_glyph_subset_are_public_contracts(self):
        self.assertEqual(
            EFFECT_NAMES,
            (
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
            ),
        )
        self.assertEqual(
            GLYPH_EFFECT_NAMES,
            frozenset(
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
            ),
        )
        self.assertEqual(STATEFUL_EFFECT_NAMES, frozenset(("afterimage",)))
        self.assertEqual(len(EFFECT_NAMES), len(set(EFFECT_NAMES)))
        self.assertEqual(tuple(spec.name for spec in EFFECT_SPECS), EFFECT_NAMES)
        self.assertTrue(all(isinstance(spec, EffectSpec) for spec in EFFECT_SPECS))
        for spec in EFFECT_SPECS:
            self.assertEqual(spec.glyph_owned, spec.name in GLYPH_EFFECT_NAMES)
            self.assertEqual(
                spec.pixel_policy,
                "char-cells" if spec.glyph_owned else "native",
            )
            self.assertEqual(spec.stateful, spec.name in STATEFUL_EFFECT_NAMES)
            self.assertEqual(EffectProcessor(spec.name).spec, spec)

    def test_select_cycle_wrap_and_reset_preserve_selection(self):
        processor = EffectProcessor()
        seen = [processor.name]
        for _ in EFFECT_NAMES:
            seen.append(processor.cycle())
        self.assertEqual(tuple(seen[:-1]), EFFECT_NAMES)
        self.assertEqual(seen[-1], "none")
        self.assertEqual(processor.select("voronoi"), "voronoi")
        processor.apply(self.frame, 1.0)
        self.assertTrue(processor._cache)
        processor.reset("resize")
        self.assertEqual(processor.name, "voronoi")
        self.assertFalse(processor._cache)

    def test_configuration_validation(self):
        self.assertEqual(DEFAULT_EFFECT_TEXT, "YTASCII")
        with self.assertRaisesRegex(ValueError, "unknown effect"):
            EffectProcessor("missing")
        processor = EffectProcessor()
        with self.assertRaisesRegex(ValueError, "unknown effect"):
            processor.select("missing")
        self.assertEqual(processor.name, "none")
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

    def test_effect_text_validation_preserves_safe_input_exactly(self):
        accepted = (
            ("A B", "ascii"),
            (" A ", "ascii"),
            ("A" * 253, "ascii"),
            ("λ", "unicode"),
            ("é", "unicode"),
        )
        for value, mode in accepted:
            with self.subTest(value=value[:8], mode=mode):
                processor = EffectProcessor(
                    effect_text=value, glyph_mode=mode
                )
                self.assertEqual(processor.effect_text, value)

        with self.assertRaises(TypeError):
            EffectProcessor(effect_text=123)
        invalid = (
            "",
            "   ",
            "A" * 254,
            "A\tB",
            "A\nB",
            "A\x00B",
            "A\u200dB",
            "e\u0301",
            "界",
            "א",
        )
        for value in invalid:
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                EffectProcessor(effect_text=value, glyph_mode="unicode")
        with self.assertRaises(ValueError):
            EffectProcessor(effect_text="λ", glyph_mode="ascii")

    def test_launcher_and_engine_effect_text_validation_stay_in_parity(self):
        launcher = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "yt-ascii"),
            run_name="yt_ascii_effect_validation_test",
        )
        cases = (
            ("YTASCII", "ascii"),
            (" A B ", "ascii"),
            ("λ", "unicode"),
            ("é", "unicode"),
            ("", "ascii"),
            ("   ", "ascii"),
            ("A" * 254, "ascii"),
            ("λ", "ascii"),
            ("e\u0301", "unicode"),
            ("A\u200dB", "unicode"),
            ("界", "unicode"),
            ("א", "unicode"),
            (123, "ascii"),
        )
        for value, mode in cases:
            with self.subTest(value=repr(value), mode=mode):
                outcomes = []
                for validate in (
                    lambda: launcher["validate_effect_text"](value, mode),
                    lambda: EffectProcessor(
                        glyph_mode=mode, effect_text=value
                    ).effect_text,
                ):
                    try:
                        result = validate()
                    except (TypeError, ValueError) as error:
                        outcomes.append(type(error))
                    else:
                        outcomes.append(("accepted", result))
                self.assertEqual(outcomes[0], outcomes[1])

    def test_unicode_inscription_uses_full_256_entry_schema_safely(self):
        safe = []
        for codepoint in range(0x21, 0x10000):
            glyph = chr(codepoint)
            if glyph in "‹›":
                continue
            category = unicodedata.category(glyph)
            if (
                not glyph.isprintable()
                or glyph.isspace()
                or category in ("Cc", "Cf", "Cs")
                or category.startswith("M")
                or unicodedata.combining(glyph)
                or unicodedata.bidirectional(glyph)
                in ("R", "AL", "AN", "RLE", "RLO", "RLI")
                or unicodedata.east_asian_width(glyph) in ("W", "F")
            ):
                continue
            safe.append(glyph)
            if len(safe) == 253:
                break
        self.assertEqual(len(safe), 253)

        processor = EffectProcessor(
            "inscription",
            glyph_mode="unicode",
            effect_text="".join(safe),
        )
        schema = processor._text_schemas["inscription"]
        self.assertEqual(len(schema.glyphs), 256)
        self.assertEqual(int(schema.token[-2]), 255)
        self.assertEqual(int(schema.token.max()), 255)

        frame = np.full((1, 256, 3), 127, dtype=np.uint8)
        gradient = np.full((1, 256), 96, dtype=np.int32)
        with mock.patch.object(
            effects,
            "_sobel",
            return_value=(gradient, np.zeros_like(gradient)),
        ):
            plane = processor.apply(frame).cells
        self.assertEqual(len(plane.glyphs), 256)
        self.assertEqual(int(plane.glyph_indices.max()), 255)

    def test_default_text_effect_schemas_and_tokens_are_stable(self):
        frame = np.full((3, 8, 3), 255, dtype=np.uint8)
        expected = {
            "word-field": (" YTASCI.", [1, 2, 3, 4, 5, 6, 6, 7, 0]),
            "inscription": (" YTASCI[]", [7, 1, 2, 3, 4, 5, 6, 6, 8, 0]),
            "type-echo": (" YTASCI:", [1, 2, 3, 4, 5, 6, 6, 7, 0]),
        }
        for name, (glyphs, token) in expected.items():
            with self.subTest(effect=name):
                processor = EffectProcessor(name)
                plane = processor.apply(frame, context(shape=(3, 8))).cells
                self.assertEqual(plane.glyphs, glyphs)
                np.testing.assert_array_equal(
                    processor._text_schemas[name].token,
                    np.array(token, dtype=np.uint8),
                )

        unicode_expected = {
            "word-field": " YTASCI·",
            "inscription": " YTASCI‹›",
            "type-echo": " YTASCI∶",
        }
        for name, glyphs in unicode_expected.items():
            with self.subTest(effect=name):
                plane = EffectProcessor(
                    name, glyph_mode="unicode"
                ).apply(frame, context(shape=(3, 8))).cells
                self.assertEqual(plane.glyphs, glyphs)

    def test_dynamic_schema_deduplicates_text_and_decorations(self):
        frame = np.full((2, 8, 3), 255, dtype=np.uint8)
        cases = (
            ("word-field", ".I I", " .I"),
            ("inscription", "[A]", " [A]"),
            ("type-echo", ":AA", " :A"),
        )
        for name, text, expected in cases:
            with self.subTest(effect=name):
                processor = EffectProcessor(name, effect_text=text)
                first = processor.apply(frame).cells
                self.assertEqual(first.glyphs, expected)
                processor.reset("seek")
                second = processor.apply(frame).cells
                self.assertEqual(second.glyphs, expected)

    def test_remaining_roadmap_glyph_schemas_are_stable(self):
        expected = {
            "error-diffusion": (" #", " █"),
            "halftone": (" .oO@", " ·○●█"),
            "cross-stitch": (" /\\x#", " ╱╲╳█"),
            "weave": (" -|+#", " ─│┼█"),
            "kilim": (" .<>/\\#", " ·◆◇╱╲█"),
            "digital-rain": (" 01|", " 01│"),
            "stardust": (" .+*", " ·✦✧"),
            "type-collage": (" YTASCI[]", " YTASCI‹›"),
            "engraving": (" .-|/\\x#", " ·─│╱╲╳█"),
            "brickwork": (" .#-|+", " ·█─│┼"),
            "terminal-hud": (
                " .:-=+*#@|[]0123456789",
                " ·░▒▓█─│┼‹›0123456789",
            ),
        }
        frame = np.full((12, 20, 3), 173, dtype=np.uint8)
        for name, (ascii_glyphs, unicode_glyphs) in expected.items():
            for mode, glyphs in (
                ("ascii", ascii_glyphs),
                ("unicode", unicode_glyphs),
            ):
                with self.subTest(effect=name, mode=mode):
                    plane = EffectProcessor(name, glyph_mode=mode).apply(
                        frame, 0.5
                    ).cells
                    self.assertEqual(plane.glyphs, glyphs)
                    self.assertLess(
                        int(plane.glyph_indices.max(initial=0)), len(glyphs)
                    )

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

    def test_none_is_zero_copy_with_no_cell_plane(self):
        processor = EffectProcessor("none")
        output = processor.apply(self.frame, 3.25)
        self.assertIsInstance(output, EffectFrame)
        self.assertIs(output.rgb, self.frame)
        self.assertIsNone(output.cells)
        view = self.frame[:, ::2]
        self.assertIs(processor.apply(view).rgb, view)

    def test_all_effects_preserve_input_and_return_valid_contracts(self):
        presentation = context(2.25, 7, (6, 20))
        for name in EFFECT_NAMES:
            with self.subTest(effect=name):
                original = self.frame.copy()
                output = EffectProcessor(name, seed=11).apply(
                    self.frame, presentation
                )
                np.testing.assert_array_equal(self.frame, original)
                self.assertIsInstance(output, EffectFrame)
                self.assertEqual(output.rgb.shape, self.frame.shape)
                self.assertEqual(output.rgb.dtype, np.uint8)
                if name in GLYPH_EFFECT_NAMES:
                    self.assertIs(output.rgb, self.frame)
                    self.assertIsInstance(output.cells, CellPlane)
                    self.assertEqual(output.cells.glyph_indices.shape, (6, 20))
                    self.assertEqual(output.cells.fg_rgb.shape, (6, 20, 3))
                    self.assertEqual(output.cells.fg_rgb.dtype, np.uint8)
                else:
                    self.assertIsNone(output.cells)

    def test_every_effect_composes_with_render_modes_and_completed_reveals(self):
        modes = (
            (False, True, self.frame, self.frame.shape[:2]),
            (False, False, self.frame, self.frame.shape[:2]),
            (True, True, np.repeat(self.frame, 2, axis=0), self.frame.shape[:2]),
        )
        for name in EFFECT_NAMES:
            glyph_modes = ("ascii", "unicode") if name in GLYPH_EFFECT_NAMES else ("ascii",)
            for half_block, color, frame, cell_shape in modes:
                for glyph_mode in glyph_modes:
                    with self.subTest(
                        effect=name,
                        half_block=half_block,
                        color=color,
                        glyphs=glyph_mode,
                    ):
                        processor = EffectProcessor(
                            name, glyph_mode=glyph_mode, seed=17
                        )
                        effected = processor.apply(
                            frame,
                            EffectContext(
                                video_time=0.5,
                                frame_sequence=0,
                                cell_shape=cell_shape,
                                requested_pixels=half_block,
                            ),
                        )
                        renderer = AnsiRenderer(
                            " .#", color=color, half_block=half_block,
                            rng=np.random.default_rng(19),
                        )
                        expected = renderer.render(effected.rgb, effected.cells)
                        self.assertEqual(
                            renderer.render_scatter(
                                effected.rgb, 1.0, effected.cells
                            ),
                            expected,
                        )
                        self.assertEqual(
                            renderer.render_rain(
                                effected.rgb, 1.0, effected.cells
                            ),
                            expected,
                        )

    def test_empty_and_tiny_frames_are_safe_in_all_modes(self):
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
                    if output.cells is not None:
                        self.assertEqual(
                            output.cells.glyph_indices.shape, frame.shape[:2]
                        )

    def test_new_static_effects_ignore_time_speed_and_sequence(self):
        static_names = (
            "error-diffusion",
            "halftone",
            "poster-press",
            "cross-stitch",
            "weave",
            "kilim",
            "quadtree",
            "patchwork",
            "engraving",
            "brickwork",
            "prism",
            "glass",
        )

        def payload(output):
            cells = output.cells
            return (
                output.rgb.copy(),
                None if cells is None else cells.glyph_indices.copy(),
                None if cells is None else cells.fg_rgb.copy(),
            )

        for name in static_names:
            with self.subTest(effect=name):
                slow = EffectProcessor(name, speed=0.25, seed=-19).apply(
                    self.frame,
                    context(-1e200, 2, self.frame.shape[:2], advance=False),
                )
                fast = EffectProcessor(name, speed=8.0, seed=-19).apply(
                    self.frame,
                    context(1e200, 999, self.frame.shape[:2]),
                )
                for left, right in zip(payload(slow), payload(fast)):
                    if left is None:
                        self.assertIsNone(right)
                    else:
                        np.testing.assert_array_equal(left, right)

    def test_new_procedural_effects_are_seeded_and_accept_huge_integers(self):
        seeded_names = (
            "error-diffusion",
            "halftone",
            "poster-press",
            "cross-stitch",
            "weave",
            "kilim",
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

        def payload(output):
            values = [output.rgb]
            if output.cells is not None:
                values.extend(
                    (output.cells.glyph_indices, output.cells.fg_rgb)
                )
            return values

        for name in seeded_names:
            with self.subTest(effect=name):
                first = EffectProcessor(
                    name, seed=0, effect_text="CODEX"
                ).apply(self.frame, 0.375)
                repeat = EffectProcessor(
                    name, seed=0, effect_text="CODEX"
                ).apply(self.frame, 0.375)
                changed = EffectProcessor(
                    name, seed=1, effect_text="CODEX"
                ).apply(self.frame, 0.375)
                for left, right in zip(payload(first), payload(repeat)):
                    np.testing.assert_array_equal(left, right)
                self.assertTrue(
                    any(
                        not np.array_equal(left, right)
                        for left, right in zip(payload(first), payload(changed))
                    )
                )
                for seed in (-(10**100), 10**100):
                    output = EffectProcessor(
                        name, seed=seed, effect_text="CODEX"
                    ).apply(self.frame, 0.375)
                    self.assertEqual(output.rgb.shape, self.frame.shape)

    def test_new_animated_effects_use_analytic_time_and_speed(self):
        animated_names = (
            "digital-rain",
            "ribbon-scan",
            "pixel-sort",
            "stardust",
            "type-collage",
            "hologram",
            "terminal-hud",
        )

        def arrays(output):
            yield output.rgb
            if output.cells is not None:
                yield output.cells.glyph_indices
                yield output.cells.fg_rgb

        for name in animated_names:
            with self.subTest(effect=name):
                normal = EffectProcessor(name, speed=1.0, seed=23).apply(
                    self.frame, context(1.25, 4, self.frame.shape[:2])
                )
                fast = EffectProcessor(name, speed=2.0, seed=23).apply(
                    self.frame,
                    context(0.625, 400, self.frame.shape[:2], advance=False),
                )
                for left, right in zip(arrays(normal), arrays(fast)):
                    np.testing.assert_array_equal(left, right)

                processor = EffectProcessor(name, seed=23)
                first = processor.apply(
                    self.frame, context(0.75, 11, self.frame.shape[:2])
                )
                processor.reset("seek")
                replay = processor.apply(
                    self.frame,
                    context(0.75, 0, self.frame.shape[:2], advance=False),
                )
                for left, right in zip(arrays(first), arrays(replay)):
                    np.testing.assert_array_equal(left, right)

                for video_time in (-1e300, 1e300):
                    output = processor.apply(self.frame, video_time)
                    self.assertEqual(output.rgb.shape, self.frame.shape)
                    if output.cells is not None:
                        self.assertEqual(
                            output.cells.glyph_indices.shape,
                            self.frame.shape[:2],
                        )

    def test_new_layout_caches_are_reused_bounded_and_resettable(self):
        for name in ("error-diffusion", "patchwork", "glass"):
            with self.subTest(effect=name):
                processor = EffectProcessor(name, seed=31)
                first = processor.apply(self.frame, 0.5)
                self.assertTrue(processor._cache)
                cached = dict(processor._cache)
                repeat = processor.apply(self.frame, 99.0)
                self.assertEqual(tuple(processor._cache), tuple(cached))
                for key, value in cached.items():
                    self.assertIs(processor._cache[key], value)
                np.testing.assert_array_equal(first.rgb, repeat.rgb)
                if first.cells is not None:
                    np.testing.assert_array_equal(
                        first.cells.glyph_indices,
                        repeat.cells.glyph_indices,
                    )

                resized = self.frame[:7, :11]
                processor.apply(resized, 0.5)
                self.assertNotEqual(tuple(processor._cache), tuple(cached))
                processor.reset("resize")
                self.assertFalse(processor._cache)
                rebuilt = processor.apply(self.frame, 0.5)
                np.testing.assert_array_equal(first.rgb, rebuilt.rgb)
                if first.cells is not None:
                    np.testing.assert_array_equal(
                        first.cells.glyph_indices,
                        rebuilt.cells.glyph_indices,
                    )

    def test_new_animations_change_only_at_documented_tick_boundaries(self):
        rates = {
            "digital-rain": 12,
            "ribbon-scan": 8,
            "pixel-sort": 6,
            "stardust": 8,
            "type-collage": 3,
            "hologram": 12,
            "terminal-hud": 10,
        }

        def changed(left, right):
            if not np.array_equal(left.rgb, right.rgb):
                return True
            if left.cells is None:
                return False
            return not np.array_equal(
                left.cells.glyph_indices, right.cells.glyph_indices
            ) or not np.array_equal(left.cells.fg_rgb, right.cells.fg_rgb)

        for name, rate in rates.items():
            with self.subTest(effect=name):
                processor = EffectProcessor(name, seed=7, effect_text="CODEX")
                first = processor.apply(self.frame, 0.0)
                same_tick = processor.apply(
                    self.frame, (1.0 / rate) - 1e-9
                )
                next_tick = processor.apply(self.frame, 1.0 / rate)
                self.assertFalse(changed(first, same_tick))
                self.assertTrue(changed(first, next_tick))

    def test_error_diffusion_order_is_seeded_serpentine_permutation(self):
        rows, cols = 5, 7
        first = effects._error_diffusion_order(rows, cols, 0)
        repeat = effects._error_diffusion_order(rows, cols, 0)
        changed = effects._error_diffusion_order(rows, cols, 1)
        np.testing.assert_array_equal(first, repeat)
        np.testing.assert_array_equal(np.sort(first), np.arange(rows * cols))
        np.testing.assert_array_equal(first[:cols], np.arange(cols))
        np.testing.assert_array_equal(
            first[cols:2 * cols], np.arange(2 * cols - 1, cols - 1, -1)
        )
        self.assertFalse(np.array_equal(first, changed))
        self.assertFalse(first.flags.writeable)
        self.assertEqual(
            effects._error_diffusion_order(0, cols, 9).shape, (0,)
        )

    def test_patchwork_bsp_layout_has_bounded_rectangular_regions(self):
        rows, cols = 48, 72
        labels = effects._patchwork_layout(rows, cols, -123)
        repeat = effects._patchwork_layout(rows, cols, -123)
        changed = effects._patchwork_layout(rows, cols, -122)
        np.testing.assert_array_equal(labels, repeat)
        self.assertFalse(np.array_equal(labels, changed))
        self.assertFalse(labels.flags.writeable)
        unique = np.unique(labels)
        self.assertLessEqual(len(unique), 32)
        np.testing.assert_array_equal(unique, np.arange(len(unique)))
        for label in unique:
            positions = np.argwhere(labels == label)
            top, left = positions.min(axis=0)
            bottom, right = positions.max(axis=0) + 1
            self.assertGreaterEqual(bottom - top, 4)
            self.assertGreaterEqual(right - left, 4)
            self.assertTrue(np.all(labels[top:bottom, left:right] == label))

    def test_glass_layout_offsets_edges_and_frost_are_bounded(self):
        rows, cols = 17, 23
        source_rows, source_cols, frost, edges = effects._glass_layout(
            rows, cols, 81
        )
        row_grid, col_grid = np.indices((rows, cols))
        self.assertTrue(np.all(np.abs(source_rows - row_grid) <= 2))
        self.assertTrue(np.all(np.abs(source_cols - col_grid) <= 2))
        self.assertGreaterEqual(int(source_rows.min()), 0)
        self.assertLess(int(source_rows.max()), rows)
        self.assertGreaterEqual(int(source_cols.min()), 0)
        self.assertLess(int(source_cols.max()), cols)
        self.assertGreaterEqual(int(frost.min()), -7)
        self.assertLessEqual(int(frost.max()), 8)
        np.testing.assert_array_equal(
            edges, ((row_grid % 4) == 0) | ((col_grid % 6) == 0)
        )
        for value in (source_rows, source_cols, frost, edges):
            self.assertFalse(value.flags.writeable)

    def test_error_diffusion_conserves_tone_and_has_exact_endpoints(self):
        black = np.zeros((4, 7, 3), dtype=np.uint8)
        white = np.full_like(black, 255)
        self.assertFalse(
            EffectProcessor("error-diffusion", seed=-99)
            .apply(black)
            .cells.glyph_indices.any()
        )
        self.assertTrue(
            np.all(
                EffectProcessor("error-diffusion", seed=99)
                .apply(white)
                .cells.glyph_indices
                == 1
            )
        )

        values = np.arange(12 * 20, dtype=np.uint8).reshape(12, 20)
        frame = np.repeat(values[:, :, None], 3, axis=2)
        outputs = []
        for seed in (0, 255):
            indices = EffectProcessor(
                "error-diffusion", seed=seed
            ).apply(frame).cells.glyph_indices
            expected_marks = (int(values.sum()) + seed % 255) // 255
            self.assertEqual(int(indices.sum()), expected_marks)
            outputs.append(indices)
        self.assertFalse(np.array_equal(*outputs))

    def test_halftone_uses_exact_clustered_rank_coverage(self):
        for luminance in (0, 1, 16, 64, 128, 192, 255):
            with self.subTest(luminance=luminance):
                frame = np.full((4, 4, 3), luminance, dtype=np.uint8)
                indices = EffectProcessor("halftone", seed=0).apply(
                    frame
                ).cells.glyph_indices
                coverage = (luminance * 16 + 255) // 256
                weight = 1 + luminance * 4 // 256
                self.assertEqual(int(np.count_nonzero(indices)), coverage)
                if coverage:
                    self.assertEqual(
                        set(indices[indices != 0].tolist()), {weight}
                    )

        one_dot = EffectProcessor("halftone", seed=0).apply(
            np.full((4, 4, 3), 16, dtype=np.uint8)
        ).cells.glyph_indices
        expected = np.zeros((4, 4), dtype=np.uint8)
        expected[1, 1] = 1
        np.testing.assert_array_equal(one_dot, expected)
        shifted = EffectProcessor("halftone", seed=1).apply(
            np.full((4, 4, 3), 16, dtype=np.uint8)
        ).cells.glyph_indices
        self.assertFalse(np.array_equal(one_dot, shifted))
        self.assertEqual(int(np.count_nonzero(shifted)), 1)

    def test_cross_stitch_exact_tone_roles_and_seeded_checker(self):
        values = np.array((0, 63, 64, 127, 128, 191, 192, 255), dtype=np.uint8)
        frame = np.repeat(values[None, :, None], 3, axis=2)
        first = EffectProcessor("cross-stitch", seed=0).apply(
            frame
        ).cells.glyph_indices
        second = EffectProcessor("cross-stitch", seed=1).apply(
            frame
        ).cells.glyph_indices
        np.testing.assert_array_equal(
            first,
            np.array(((0, 0, 1, 2, 3, 3, 4, 4),), dtype=np.uint8),
        )
        np.testing.assert_array_equal(
            second,
            np.array(((0, 0, 2, 1, 3, 3, 4, 4),), dtype=np.uint8),
        )

    def test_weave_and_kilim_repeat_their_locked_motifs(self):
        weave_frame = np.full((8, 8, 3), 128, dtype=np.uint8)
        weave = EffectProcessor("weave", seed=0).apply(
            weave_frame
        ).cells.glyph_indices
        np.testing.assert_array_equal(weave, np.tile(weave[:4, :4], (2, 2)))
        self.assertEqual(set(np.unique(weave).tolist()), {1, 2, 3})
        self.assertFalse(
            EffectProcessor("weave").apply(
                np.zeros_like(weave_frame)
            ).cells.glyph_indices.any()
        )

        kilim_frame = np.full((16, 32, 3), 255, dtype=np.uint8)
        kilim = EffectProcessor("kilim", seed=0).apply(
            kilim_frame
        ).cells.glyph_indices
        np.testing.assert_array_equal(kilim, np.tile(kilim[:8, :16], (2, 2)))
        self.assertEqual(set(np.unique(kilim).tolist()), {1, 2, 3, 4, 5, 6})
        changed = EffectProcessor("kilim", seed=1).apply(
            kilim_frame
        ).cells.glyph_indices
        self.assertFalse(np.array_equal(kilim, changed))

    def test_digital_rain_has_analytic_heads_trails_and_green_fade(self):
        frame = np.empty((6, 4, 3), dtype=np.uint8)
        frame[:] = (120, 100, 80)

        def zero_hash(rows, cols, seed):
            return np.zeros((rows, cols), dtype=np.uint64)

        with mock.patch.object(effects, "_hash_grid", side_effect=zero_hash):
            tick_zero = EffectProcessor("digital-rain").apply(
                frame, 0.0
            ).cells
            tick_one = EffectProcessor("digital-rain").apply(
                frame, 1 / 12
            ).cells
        self.assertTrue(np.all(tick_zero.glyph_indices[0] == 3))
        self.assertFalse(tick_zero.glyph_indices[1:].any())
        self.assertTrue(np.all(tick_one.glyph_indices[0] == 1))
        self.assertTrue(np.all(tick_one.glyph_indices[1] == 3))
        self.assertFalse(tick_one.glyph_indices[2:].any())
        self.assertGreater(
            int(tick_zero.fg_rgb[0, 0, 1]),
            int(tick_zero.fg_rgb[0, 0, 0]),
        )
        self.assertTrue(np.all(tick_zero.fg_rgb[1:] == 0))

    def test_stardust_density_twinkle_and_color_weights(self):
        black = np.zeros((5, 7, 3), dtype=np.uint8)
        self.assertFalse(
            EffectProcessor("stardust").apply(black).cells.glyph_indices.any()
        )
        frame = np.empty((5, 7, 3), dtype=np.uint8)
        frame[:] = (100, 150, 200)
        hashes = np.zeros((5, 7), dtype=np.uint64)
        with mock.patch.object(effects, "_hash_grid", return_value=hashes):
            planes = [
                EffectProcessor("stardust").apply(frame, tick / 8).cells
                for tick in range(3)
            ]
        for expected_index, plane in enumerate(planes, 1):
            self.assertTrue(np.all(plane.glyph_indices == expected_index))
        np.testing.assert_array_equal(planes[0].fg_rgb[0, 0], (40, 60, 80))
        np.testing.assert_array_equal(planes[1].fg_rgb[0, 0], (80, 120, 160))
        np.testing.assert_array_equal(planes[2].fg_rgb[0, 0], (100, 150, 200))

    def test_type_collage_preserves_custom_text_and_reverses_odd_bands(self):
        frame = np.full((2, 10, 3), 255, dtype=np.uint8)
        plane = EffectProcessor(
            "type-collage", effect_text="AB", seed=0
        ).apply(frame, 0.0).cells
        self.assertEqual(plane.glyphs, " AB[]")
        np.testing.assert_array_equal(
            plane.glyph_indices,
            np.array(
                (
                    (3, 1, 2, 4, 0, 3, 1, 2, 4, 0),
                    (2, 1, 3, 0, 4, 2, 1, 3, 0, 4),
                ),
                dtype=np.uint8,
            ),
        )
        dark = EffectProcessor(
            "type-collage", effect_text="AB"
        ).apply(np.zeros_like(frame)).cells
        self.assertFalse(dark.glyph_indices.any())

    def test_engraving_threshold_orientation_and_hatch_roles(self):
        white = np.full((4, 4, 3), 255, dtype=np.uint8)
        black = np.zeros_like(white)
        self.assertFalse(
            EffectProcessor("engraving").apply(white).cells.glyph_indices.any()
        )
        self.assertTrue(
            np.all(
                EffectProcessor("engraving")
                .apply(black)
                .cells.glyph_indices
                == 7
            )
        )

        frame = np.full((1, 2, 3), 200, dtype=np.uint8)
        for strength, expected in ((95, (1, 2)), (96, (3, 3))):
            gradients = np.full((1, 2), strength, dtype=np.int32)
            with self.subTest(strength=strength), mock.patch.object(
                effects,
                "_sobel",
                return_value=(gradients, np.zeros_like(gradients)),
            ):
                indices = EffectProcessor("engraving", seed=0).apply(
                    frame
                ).cells.glyph_indices
                self.assertEqual(tuple(indices[0]), expected)

    def test_brickwork_has_staggered_four_by_eight_mortar_courses(self):
        frame = np.full((16, 16, 3), 200, dtype=np.uint8)
        indices = EffectProcessor("brickwork", seed=0).apply(
            frame
        ).cells.glyph_indices
        np.testing.assert_array_equal(indices, np.tile(indices[:8, :8], (2, 2)))
        self.assertEqual(set(np.unique(indices).tolist()), {2, 3, 4, 5})
        self.assertTrue(np.all(indices[0, 1:8] == 3))
        self.assertTrue(np.all(indices[1:4, 0] == 4))
        self.assertFalse(
            EffectProcessor("brickwork").apply(
                np.zeros_like(frame)
            ).cells.glyph_indices.any()
        )

    def test_terminal_hud_draws_border_reticle_ticks_and_five_digit_time(self):
        frame = np.full((9, 20, 3), 128, dtype=np.uint8)
        plane = EffectProcessor("terminal-hud", seed=0).apply(
            frame, 12.3
        ).cells
        glyph_rows = np.array(tuple(plane.glyphs))[plane.glyph_indices]
        self.assertEqual("".join(glyph_rows[0, -7:]), "[00123]")
        for col in range(1, 19):
            self.assertEqual(glyph_rows[-1, col], "+" if col in (8, 16) else "-")
        for row in range(1, 8):
            expected = "+" if row == 4 else "|"
            self.assertEqual(glyph_rows[row, 0], expected)
            self.assertEqual(glyph_rows[row, -1], expected)
        center_row, center_col = 4, 10
        self.assertEqual(glyph_rows[center_row, center_col], "+")
        self.assertEqual(glyph_rows[center_row, center_col - 1], "-")
        self.assertEqual(glyph_rows[center_row, center_col + 1], "-")
        self.assertEqual(glyph_rows[center_row - 1, center_col], "|")
        self.assertEqual(glyph_rows[center_row + 1, center_col], "|")
        self.assertEqual(glyph_rows[-1, 8], "+")

    def test_poster_press_has_four_levels_displaced_plates_and_dark_edges(self):
        for value, expected in (
            (0, 0),
            (42, 0),
            (43, 85),
            (127, 85),
            (128, 170),
            (212, 170),
            (213, 255),
            (255, 255),
        ):
            with self.subTest(value=value):
                frame = np.full((2, 2, 3), value, dtype=np.uint8)
                output = EffectProcessor("poster-press").apply(frame).rgb
                self.assertTrue(np.all(output == expected))

        frame = np.arange(4 * 5 * 3, dtype=np.uint8).reshape(4, 5, 3) * 4
        quantized = (
            (frame.astype(np.uint16) * 3 + 127) // 255 * 85
        ).astype(np.uint8)
        zeros = np.zeros(frame.shape[:2], dtype=np.int32)
        with mock.patch.object(effects, "_sobel", return_value=(zeros, zeros)):
            output = EffectProcessor("poster-press", seed=0).apply(frame).rgb
        np.testing.assert_array_equal(
            output[:, :, 0], np.roll(quantized[:, :, 0], -1, axis=0)
        )
        np.testing.assert_array_equal(output[:, :, 1], quantized[:, :, 1])
        np.testing.assert_array_equal(
            output[:, :, 2], np.roll(quantized[:, :, 2], 1, axis=0)
        )

        base = np.full((1, 2, 3), 128, dtype=np.uint8)
        for strength, expected in ((95, 170), (96, 68)):
            gradient = np.full((1, 2), strength, dtype=np.int32)
            with self.subTest(strength=strength), mock.patch.object(
                effects,
                "_sobel",
                return_value=(gradient, np.zeros_like(gradient)),
            ):
                result = EffectProcessor("poster-press").apply(base).rgb
                self.assertTrue(np.all(result == expected))

    def test_quadtree_uses_leaf_means_seams_and_four_pixel_depth_cap(self):
        flat = np.empty((16, 16, 3), dtype=np.uint8)
        flat[:] = (20, 80, 140)
        np.testing.assert_array_equal(
            EffectProcessor("quadtree").apply(flat).rgb, flat
        )

        quadrants = np.zeros((16, 16, 3), dtype=np.uint8)
        quadrant_colors = ((40, 80, 120), (80, 120, 160),
                           (120, 160, 200), (160, 200, 240))
        quadrants[:8, :8] = quadrant_colors[0]
        quadrants[:8, 8:] = quadrant_colors[1]
        quadrants[8:, :8] = quadrant_colors[2]
        quadrants[8:, 8:] = quadrant_colors[3]
        output = EffectProcessor("quadtree").apply(quadrants).rgb
        np.testing.assert_array_equal(output[1, 1], quadrant_colors[0])
        np.testing.assert_array_equal(output[1, 9], quadrant_colors[1])
        np.testing.assert_array_equal(output[9, 1], quadrant_colors[2])
        np.testing.assert_array_equal(output[9, 9], quadrant_colors[3])
        np.testing.assert_array_equal(
            output[8, 1], np.array(quadrant_colors[2]) * 2 // 5
        )
        np.testing.assert_array_equal(
            output[1, 8], np.array(quadrant_colors[1]) * 2 // 5
        )

        rows, cols = np.indices((128, 128))
        detailed = (((rows + cols) & 1) * 255).astype(np.uint8)
        detailed = np.repeat(detailed[:, :, None], 3, axis=2)
        capped = EffectProcessor("quadtree").apply(detailed).rgb
        for top in range(0, 128, 4):
            for left in range(0, 128, 4):
                interior = capped[top + 1:top + 4, left + 1:left + 4]
                self.assertTrue(np.all(interior == interior[0, 0]))

    def test_patchwork_fills_cached_region_means_and_one_sided_seams(self):
        processor = EffectProcessor("patchwork", seed=18)
        output = processor.apply(self.frame).rgb
        labels = next(iter(processor._cache.values()))
        expected = np.empty_like(self.frame)
        for label in np.unique(labels):
            mask = labels == label
            mean = self.frame[mask].astype(np.uint32).sum(axis=0) // mask.sum()
            expected[mask] = mean.astype(np.uint8)
        boundaries = np.zeros(labels.shape, dtype=bool)
        boundaries[1:] |= labels[1:] != labels[:-1]
        boundaries[:, 1:] |= labels[:, 1:] != labels[:, :-1]
        expected[boundaries] = (
            expected[boundaries].astype(np.uint16) * 2 // 5
        ).astype(np.uint8)
        np.testing.assert_array_equal(output, expected)

        layout = labels
        changed_frame = 255 - self.frame
        changed = processor.apply(changed_frame).rgb
        self.assertIs(next(iter(processor._cache.values())), layout)
        self.assertFalse(np.array_equal(output, changed))

    def test_ribbon_scan_has_three_pixel_highlights_per_twelve_rows(self):
        frame = np.full((24, 32, 3), 100, dtype=np.uint8)
        output = EffectProcessor("ribbon-scan", seed=0).apply(
            frame, 0.0
        ).rgb
        self.assertEqual(set(np.unique(output).tolist()), {60, 168})
        for col in range(output.shape[1]):
            self.assertEqual(int(np.count_nonzero(output[:, col, 0] == 168)), 6)
        changed = EffectProcessor("ribbon-scan", seed=1).apply(
            frame, 0.0
        ).rgb
        self.assertFalse(np.array_equal(output, changed))

    def test_pixel_sort_is_stable_and_preserves_each_row_permutation(self):
        values = np.arange(31, -1, -1, dtype=np.uint8)
        frame = np.repeat(values[None, :, None], 3, axis=2)
        output = EffectProcessor("pixel-sort", seed=0).apply(
            frame, 0.0
        ).rgb
        expected = np.concatenate((np.arange(16, 32), np.arange(16))).astype(
            np.uint8
        )
        np.testing.assert_array_equal(output[0, :, 0], expected)

        equal_luma = np.array(
            (
                (0, 0, 128), (0, 8, 88), (0, 16, 48), (0, 24, 0),
                (0, 24, 8), (8, 0, 104), (8, 8, 64), (8, 16, 24),
                (16, 0, 88), (16, 8, 40), (16, 8, 48), (16, 16, 0),
                (24, 0, 64), (24, 8, 24), (32, 0, 40), (32, 8, 0),
            ),
            dtype=np.uint8,
        )[None, :, :]
        self.assertEqual(
            set(effects._luminance(equal_luma).ravel().tolist()), {14}
        )
        stable = EffectProcessor("pixel-sort", seed=0).apply(
            equal_luma, 0.0
        ).rgb
        np.testing.assert_array_equal(stable, equal_luma)

        rich = np.stack((self.frame, 255 - self.frame), axis=0).reshape(
            24, 20, 3
        )
        sorted_frame = EffectProcessor("pixel-sort", seed=13).apply(
            rich, 0.5
        ).rgb
        for source_row, sorted_row in zip(rich, sorted_frame):
            source_codes = (
                source_row[:, 0].astype(np.uint32) << 16
                | source_row[:, 1].astype(np.uint32) << 8
                | source_row[:, 2].astype(np.uint32)
            )
            sorted_codes = (
                sorted_row[:, 0].astype(np.uint32) << 16
                | sorted_row[:, 1].astype(np.uint32) << 8
                | sorted_row[:, 2].astype(np.uint32)
            )
            np.testing.assert_array_equal(
                np.sort(source_codes), np.sort(sorted_codes)
            )

    def test_prism_displaces_opposing_channels_and_highlights_edges(self):
        flat = np.empty((4, 5, 3), dtype=np.uint8)
        flat[:] = (30, 90, 150)
        np.testing.assert_array_equal(
            EffectProcessor("prism", seed=3).apply(flat).rgb, flat
        )

        frame = np.arange(4 * 5 * 3, dtype=np.uint8).reshape(4, 5, 3) * 4
        zeros = np.zeros(frame.shape[:2], dtype=np.int32)
        with mock.patch.object(effects, "_sobel", return_value=(zeros, zeros)):
            output = EffectProcessor("prism", seed=1).apply(frame).rgb
        np.testing.assert_array_equal(output[:, :, 1], frame[:, :, 1])
        np.testing.assert_array_equal(
            output[:, :, 0],
            (
                frame[:, :, 0].astype(np.uint16)
                + np.roll(frame[:, :, 0], 1, axis=1)
            ) // 2,
        )
        np.testing.assert_array_equal(
            output[:, :, 2],
            (
                frame[:, :, 2].astype(np.uint16)
                + np.roll(frame[:, :, 2], -1, axis=1)
            ) // 2,
        )

        base = np.full((1, 2, 3), 100, dtype=np.uint8)
        gradient = np.full((1, 2), 96, dtype=np.int32)
        with mock.patch.object(
            effects,
            "_sobel",
            return_value=(gradient, np.zeros_like(gradient)),
        ):
            highlighted = EffectProcessor("prism").apply(base).rgb
        self.assertTrue(np.all(highlighted == 140))

    def test_hologram_scanline_palette_and_sparkles_are_exact(self):
        frame = np.full((4, 3, 3), 100, dtype=np.uint8)
        maximum_hashes = np.full((4, 3), np.iinfo(np.uint64).max, dtype=np.uint64)
        with mock.patch.object(
            effects, "_hash_grid", return_value=maximum_hashes
        ):
            output = EffectProcessor("hologram").apply(frame, 0.0).rgb
        np.testing.assert_array_equal(output[0, 0], (12, 60, 79))
        np.testing.assert_array_equal(output[1, 0], (50, 33, 100))
        np.testing.assert_array_equal(output[2, 0], (20, 100, 132))
        np.testing.assert_array_equal(output[3, 0], (50, 33, 100))

        zero_hashes = np.zeros((4, 3), dtype=np.uint64)
        with mock.patch.object(effects, "_hash_grid", return_value=zero_hashes):
            sparkles = EffectProcessor("hologram").apply(frame, 0.0).rgb
        np.testing.assert_array_equal(
            sparkles, np.broadcast_to((220, 255, 255), frame.shape)
        )

    def test_glass_applies_cached_refraction_frost_and_pane_highlights(self):
        frame = np.full((9, 13, 3), 100, dtype=np.uint8)
        processor = EffectProcessor("glass", seed=44)
        output = processor.apply(frame).rgb
        layout = next(iter(processor._cache.values()))
        _source_rows, _source_cols, frost, edges = layout
        expected = np.clip(
            100 + frost.astype(np.int16)[:, :, None], 0, 255
        )
        expected = np.broadcast_to(expected, frame.shape).copy()
        expected[edges] = np.minimum(255, expected[edges] + 28)
        np.testing.assert_array_equal(output, expected.astype(np.uint8))
        changed_layout = effects._glass_layout(9, 13, 45)
        self.assertFalse(np.array_equal(layout[0], changed_layout[0]))

    def test_cell_reduction_averages_half_block_rows(self):
        frame = np.array(
            [
                [[10, 20, 30], [30, 40, 50]],
                [[20, 40, 60], [50, 60, 70]],
                [[100, 110, 120], [130, 140, 150]],
                [[200, 210, 220], [230, 240, 250]],
            ],
            dtype=np.uint8,
        )
        output = EffectProcessor("geometry").apply(
            frame, context(shape=(2, 2))
        )
        expected = np.array(
            [[[15, 30, 45], [40, 50, 60]], [[150, 160, 170], [180, 190, 200]]],
            dtype=np.uint8,
        )
        np.testing.assert_array_equal(output.cells.fg_rgb, expected)

    def test_ascii_and_unicode_glyph_schemas_are_safe(self):
        schemas = {}
        for name in GLYPH_EFFECT_NAMES:
            for mode in ("ascii", "unicode"):
                output = EffectProcessor(name, glyph_mode=mode).apply(self.frame)
                glyphs = output.cells.glyphs
                schemas[(name, mode)] = glyphs
                self.assertTrue(glyphs)
                if mode == "ascii":
                    self.assertTrue(all(ord(glyph) < 128 for glyph in glyphs))
                else:
                    self.assertTrue(any(ord(glyph) >= 128 for glyph in glyphs))
                    for glyph in glyphs:
                        self.assertFalse(unicodedata.combining(glyph))
                        self.assertNotIn(
                            unicodedata.east_asian_width(glyph), ("W", "F")
                        )
                        self.assertNotIn(unicodedata.category(glyph), ("Cc", "Cf"))
                self.assertLess(
                    int(output.cells.glyph_indices.max(initial=0)), len(glyphs)
                )
            self.assertNotEqual(schemas[(name, "ascii")], schemas[(name, "unicode")])

    def test_geometry_maps_luminance_endpoints(self):
        dark = EffectProcessor("geometry").apply(
            np.zeros((3, 3, 3), dtype=np.uint8)
        ).cells
        light = EffectProcessor("geometry").apply(
            np.full((3, 3, 3), 255, dtype=np.uint8)
        ).cells
        self.assertTrue(np.all(dark.glyph_indices == 0))
        self.assertTrue(np.all(light.glyph_indices == len(light.glyphs) - 1))

    def test_geometry_combines_tone_bands_and_sobel_orientation(self):
        ramp = np.repeat(
            np.array((0, 64, 128, 192, 255), dtype=np.uint8), 4
        )
        flat_bands = np.repeat(ramp[None, :, None], 3, axis=0)
        flat_bands = np.repeat(flat_bands, 3, axis=2)
        bands = EffectProcessor("geometry").apply(flat_bands).cells
        self.assertGreaterEqual(
            len(set(bands.glyph_indices[1, 2::4].tolist())), 4
        )
        mid_tone = EffectProcessor("geometry").apply(
            np.full((8, 8, 3), 96, dtype=np.uint8)
        ).cells.glyph_indices
        self.assertEqual(set(np.unique(mid_tone).tolist()), {2, 3, 4, 5})
        edge = np.zeros((9, 9, 3), dtype=np.uint8)
        edge[:, 5:] = 255
        plane = EffectProcessor("geometry").apply(edge).cells
        # Index 3 is the vertical stroke selected from the Sobel orientation.
        self.assertTrue(np.all(plane.glyph_indices[:, 4:6] == 3))

    def test_contour_glyph_suppresses_flat_tone_and_detects_edge(self):
        flat = np.full((9, 9, 3), 100, dtype=np.uint8)
        self.assertFalse(
            EffectProcessor("contour-glyph").apply(flat).cells.glyph_indices.any()
        )
        edge = np.zeros((9, 9, 3), dtype=np.uint8)
        edge[:, 5:] = 255
        indices = EffectProcessor("contour-glyph").apply(edge).cells.glyph_indices
        self.assertTrue(indices[:, 4:6].any())
        self.assertFalse(indices[:, :3].any())
        self.assertTrue(set(np.unique(indices)) <= {0, 2})

    def test_hatch_tone_and_seed_choose_direction(self):
        frame = np.full((8, 8, 3), 160, dtype=np.uint8)
        first = EffectProcessor("hatch", seed=0).apply(frame).cells.glyph_indices
        repeat = EffectProcessor("hatch", seed=0).apply(frame).cells.glyph_indices
        shifted = EffectProcessor("hatch", seed=1).apply(frame).cells.glyph_indices
        np.testing.assert_array_equal(first, repeat)
        np.testing.assert_array_equal(first, np.roll(shifted, 1, axis=1))
        dark = EffectProcessor("hatch").apply(
            np.zeros((2, 2, 3), dtype=np.uint8)
        ).cells
        self.assertTrue(np.all(dark.glyph_indices == len(dark.glyphs) - 1))

    def test_dotfield_has_exact_endpoints_and_seeded_midtones(self):
        endpoints = np.array([[[0, 0, 0], [255, 255, 255]]], dtype=np.uint8)
        plane = EffectProcessor("dotfield").apply(endpoints).cells
        self.assertEqual(int(plane.glyph_indices[0, 0]), 0)
        self.assertEqual(int(plane.glyph_indices[0, 1]), len(plane.glyphs) - 1)
        middle = np.full((20, 20, 3), 137, dtype=np.uint8)
        first = EffectProcessor("dotfield", seed=7).apply(middle).cells.glyph_indices
        repeat = EffectProcessor("dotfield", seed=7).apply(middle).cells.glyph_indices
        changed = EffectProcessor("dotfield", seed=8).apply(middle).cells.glyph_indices
        np.testing.assert_array_equal(first, repeat)
        self.assertFalse(np.array_equal(first, changed))

    def test_number_field_reports_exact_luminance_deciles(self):
        values = np.array(
            (0, 25, 26, 51, 52, 127, 128, 230, 231, 255),
            dtype=np.uint8,
        )
        frame = np.repeat(values[None, :, None], 3, axis=2)
        plane = EffectProcessor("number-field").apply(frame).cells
        self.assertEqual(plane.glyphs, "0123456789")
        np.testing.assert_array_equal(
            plane.glyph_indices,
            np.array([[0, 0, 1, 1, 2, 4, 5, 8, 9, 9]], dtype=np.uint8),
        )
        unicode_plane = EffectProcessor(
            "number-field", glyph_mode="unicode"
        ).apply(frame).cells
        self.assertEqual(unicode_plane.glyphs, "⓪①②③④⑤⑥⑦⑧⑨")
        np.testing.assert_array_equal(
            unicode_plane.glyph_indices, plane.glyph_indices
        )
        for processor, time_value in (
            (EffectProcessor("number-field", speed=0.25, seed=-10**30), 0.0),
            (EffectProcessor("number-field", speed=8.0, seed=10**30), 99.0),
        ):
            with self.subTest(speed=processor.speed, seed=processor.seed):
                np.testing.assert_array_equal(
                    processor.apply(frame, time_value).cells.glyph_indices,
                    plane.glyph_indices,
                )

    def test_glyph_grid_combines_tone_lattice_and_seed_phase(self):
        dark = EffectProcessor("glyph-grid").apply(
            np.zeros((8, 16, 3), dtype=np.uint8)
        ).cells.glyph_indices
        self.assertFalse(dark.any())

        light_frame = np.full((8, 16, 3), 100, dtype=np.uint8)
        light = EffectProcessor("glyph-grid", seed=0).apply(
            light_frame
        ).cells.glyph_indices
        self.assertEqual(set(np.unique(light).tolist()), {1, 2, 4, 6})
        self.assertTrue(np.all(light[0::4, 0::8] == 6))
        self.assertTrue(np.all(light[0::4, 1:8] == 2))
        self.assertTrue(np.all(light[1:4, 0::8] == 4))

        heavy = EffectProcessor("glyph-grid", seed=0).apply(
            np.full((8, 16, 3), 220, dtype=np.uint8)
        ).cells.glyph_indices
        self.assertEqual(set(np.unique(heavy).tolist()), {3, 5, 7, 8})

        shifted_row = EffectProcessor("glyph-grid", seed=1).apply(
            light_frame
        ).cells.glyph_indices
        shifted_col = EffectProcessor("glyph-grid", seed=4).apply(
            light_frame
        ).cells.glyph_indices
        wrapped = EffectProcessor("glyph-grid", seed=32).apply(
            light_frame
        ).cells.glyph_indices
        np.testing.assert_array_equal(light, np.roll(shifted_row, -1, axis=0))
        np.testing.assert_array_equal(light, np.roll(shifted_col, -1, axis=1))
        np.testing.assert_array_equal(light, wrapped)

    def test_glyph_grid_disables_degenerate_axes_on_tiny_planes(self):
        frame = np.full((3, 7, 3), 100, dtype=np.uint8)
        for seed in (0, 1, -1, 10**30):
            with self.subTest(seed=seed):
                indices = EffectProcessor("glyph-grid", seed=seed).apply(
                    frame
                ).cells.glyph_indices
                self.assertTrue(np.all(indices == 1))
        heavy = EffectProcessor("glyph-grid", seed=-10**30).apply(
            np.full((1, 1, 3), 255, dtype=np.uint8)
        ).cells.glyph_indices
        self.assertEqual(int(heavy[0, 0]), 8)

    def test_vector_field_quantizes_cardinal_and_diagonal_gradients(self):
        y, x = np.indices((9, 9), dtype=np.int16)
        cases = (
            (1, 0, 1),
            (-1, 0, 5),
            (0, 1, 3),
            (0, -1, 7),
            (1, 1, 2),
            (-1, 1, 4),
            (-1, -1, 6),
            (1, -1, 8),
        )
        for x_direction, y_direction, expected in cases:
            with self.subTest(
                x_direction=x_direction, y_direction=y_direction
            ):
                values = np.clip(
                    128
                    + (x - 4) * 20 * x_direction
                    + (y - 4) * 20 * y_direction,
                    0,
                    255,
                ).astype(np.uint8)
                frame = np.repeat(values[:, :, None], 3, axis=2)
                indices = EffectProcessor("vector-field").apply(
                    frame
                ).cells.glyph_indices
                self.assertEqual(int(indices[4, 4]), expected)

        flat = EffectProcessor("vector-field").apply(
            np.full((9, 9, 3), 127, dtype=np.uint8)
        ).cells.glyph_indices
        self.assertFalse(flat.any())
        tiny = EffectProcessor("vector-field").apply(
            np.full((1, 1, 3), 255, dtype=np.uint8)
        ).cells.glyph_indices
        self.assertFalse(tiny.any())

    def test_vector_field_threshold_is_exact(self):
        for value, expected in ((47, 0), (48, 1)):
            with self.subTest(value=value):
                luminance = np.zeros((3, 3), dtype=np.uint8)
                luminance[1, 2] = value
                frame = np.repeat(luminance[:, :, None], 3, axis=2)
                indices = EffectProcessor("vector-field").apply(
                    frame
                ).cells.glyph_indices
                self.assertEqual(int(indices[1, 1]), expected)

    def test_word_field_repeats_staggered_text_at_white_endpoint(self):
        frame = np.full((3, 8, 3), 255, dtype=np.uint8)
        plane = EffectProcessor(
            "word-field", effect_text="AB", seed=0
        ).apply(frame).cells
        self.assertEqual(plane.glyphs, " AB.")
        np.testing.assert_array_equal(
            plane.glyph_indices,
            np.array(
                (
                    (1, 2, 3, 0, 1, 2, 3, 0),
                    (3, 0, 1, 2, 3, 0, 1, 2),
                    (1, 2, 3, 0, 1, 2, 3, 0),
                ),
                dtype=np.uint8,
            ),
        )
        dark = EffectProcessor(
            "word-field", effect_text="AB"
        ).apply(np.zeros_like(frame)).cells
        self.assertFalse(dark.glyph_indices.any())

    def test_word_field_hash_threshold_seed_and_static_contract(self):
        frame = np.full((1, 2, 3), 100, dtype=np.uint8)
        hashes = np.array([[99 << 56, 100 << 56]], dtype=np.uint64)
        with mock.patch.object(effects, "_hash_grid", return_value=hashes):
            indices = EffectProcessor(
                "word-field", effect_text="A"
            ).apply(frame).cells.glyph_indices
        np.testing.assert_array_equal(indices, np.array([[1, 0]], dtype=np.uint8))

        frame = np.full((12, 20, 3), 137, dtype=np.uint8)
        first = EffectProcessor(
            "word-field", effect_text="AB", speed=0.25, seed=7
        ).apply(frame, 99.0).cells.glyph_indices
        repeat = EffectProcessor(
            "word-field", effect_text="AB", speed=8.0, seed=7
        ).apply(frame, -12.0).cells.glyph_indices
        changed = EffectProcessor(
            "word-field", effect_text="AB", seed=8
        ).apply(frame).cells.glyph_indices
        np.testing.assert_array_equal(first, repeat)
        self.assertFalse(np.array_equal(first, changed))

    def test_inscription_writes_decorated_text_only_on_contours(self):
        frame = np.full((2, 5, 3), 120, dtype=np.uint8)
        active = np.array(
            ((1, 1, 0, 1, 1), (0, 1, 1, 1, 0)), dtype=np.int32
        )
        with mock.patch.object(
            effects,
            "_sobel",
            return_value=(active * 96, np.zeros_like(active)),
        ):
            plane = EffectProcessor(
                "inscription", effect_text="AB", seed=0
            ).apply(frame).cells
        self.assertEqual(plane.glyphs, " AB[]")
        np.testing.assert_array_equal(
            plane.glyph_indices,
            np.array(((3, 1, 0, 2, 4), (0, 0, 3, 1, 0)), dtype=np.uint8),
        )
        np.testing.assert_array_equal(plane.fg_rgb, frame)

    def test_inscription_threshold_flat_seed_and_static_contract(self):
        frame = np.full((1, 2, 3), 100, dtype=np.uint8)
        for strength, expected_active in ((95, False), (96, True)):
            gradients = np.full((1, 2), strength, dtype=np.int32)
            with self.subTest(strength=strength), mock.patch.object(
                effects,
                "_sobel",
                return_value=(gradients, np.zeros_like(gradients)),
            ):
                indices = EffectProcessor(
                    "inscription", effect_text="A"
                ).apply(frame).cells.glyph_indices
                self.assertEqual(bool(indices.any()), expected_active)

        flat = np.full((4, 6, 3), 127, dtype=np.uint8)
        self.assertFalse(
            EffectProcessor(
                "inscription", effect_text="AB"
            ).apply(flat).cells.glyph_indices.any()
        )
        self.assertFalse(
            EffectProcessor("inscription").apply(
                np.full((1, 1, 3), 255, dtype=np.uint8)
            ).cells.glyph_indices.any()
        )

        edge = np.zeros((8, 8, 3), dtype=np.uint8)
        edge[:, 4:] = 255
        first = EffectProcessor(
            "inscription", effect_text="AB", speed=0.25, seed=2
        ).apply(edge, -50.0).cells.glyph_indices
        repeat = EffectProcessor(
            "inscription", effect_text="AB", speed=8.0, seed=2
        ).apply(edge, 50.0).cells.glyph_indices
        changed = EffectProcessor(
            "inscription", effect_text="AB", seed=3
        ).apply(edge).cells.glyph_indices
        np.testing.assert_array_equal(first, repeat)
        self.assertFalse(np.array_equal(first, changed))

    def test_type_echo_has_exact_analytic_bands_and_scroll(self):
        frame = np.full((4, 8, 3), 255, dtype=np.uint8)
        hashes = np.zeros((4, 8), dtype=np.uint64)
        processor = EffectProcessor("type-echo", effect_text="AB", seed=0)
        with mock.patch.object(effects, "_hash_grid", return_value=hashes):
            tick_zero = processor.apply(frame, 0.0).cells
            tick_one = processor.apply(frame, 1 / 6).cells
        self.assertEqual(tick_zero.glyphs, " AB:")
        np.testing.assert_array_equal(
            tick_zero.glyph_indices,
            np.array(
                (
                    (1, 2, 3, 0, 1, 2, 3, 0),
                    (0, 0, 0, 0, 0, 0, 0, 0),
                    (3, 0, 1, 2, 3, 0, 1, 2),
                    (2, 3, 0, 1, 2, 3, 0, 1),
                ),
                dtype=np.uint8,
            ),
        )
        np.testing.assert_array_equal(
            tick_one.glyph_indices,
            np.array(
                (
                    (1, 2, 3, 0, 1, 2, 3, 0),
                    (0, 1, 2, 3, 0, 1, 2, 3),
                    (0, 0, 0, 0, 0, 0, 0, 0),
                    (2, 3, 0, 1, 2, 3, 0, 1),
                ),
                dtype=np.uint8,
            ),
        )

    def test_type_echo_density_color_timing_and_statelessness(self):
        frame = np.empty((4, 8, 3), dtype=np.uint8)
        frame[:] = (100, 150, 200)
        hashes = np.zeros((4, 8), dtype=np.uint64)
        processor = EffectProcessor("type-echo", effect_text="AB", seed=0)
        with mock.patch.object(effects, "_hash_grid", return_value=hashes):
            plane = processor.apply(frame, context(0.0, 9, (4, 8))).cells
        expected_rows = (
            (100, 150, 200),
            (0, 0, 0),
            (20, 30, 40),
            (60, 90, 120),
        )
        for row, expected in enumerate(expected_rows):
            np.testing.assert_array_equal(
                plane.fg_rgb[row], np.broadcast_to(expected, (8, 3))
            )

        before = processor.apply(frame, context(0.5, 10, (4, 8))).cells
        processor.reset("seek")
        replay = processor.apply(
            frame, context(0.5, 0, (4, 8), advance=False)
        ).cells
        fresh = EffectProcessor(
            "type-echo", effect_text="AB", seed=0
        ).apply(frame, context(0.5, 999, (4, 8))).cells
        np.testing.assert_array_equal(before.glyph_indices, replay.glyph_indices)
        np.testing.assert_array_equal(before.glyph_indices, fresh.glyph_indices)
        np.testing.assert_array_equal(before.fg_rgb, replay.fg_rgb)

        fast = EffectProcessor(
            "type-echo", effect_text="AB", speed=2.0
        ).apply(frame, 0.5).cells
        normal = EffectProcessor(
            "type-echo", effect_text="AB", speed=1.0
        ).apply(frame, 1.0).cells
        np.testing.assert_array_equal(fast.glyph_indices, normal.glyph_indices)
        for time_value in (-1e300, 1e300):
            with self.subTest(time=time_value):
                output = processor.apply(frame, time_value).cells
                self.assertEqual(output.glyph_indices.shape, (4, 8))

    def test_type_echo_hash_threshold_and_white_current_override(self):
        frame = np.full((3, 1, 3), 100, dtype=np.uint8)
        # At tick zero rows have ages 0, 2 and 1, whose thresholds are
        # respectively 100, 20 and 60.
        hashes = np.array([[100], [20], [60]], dtype=np.uint64) << np.uint64(56)
        with mock.patch.object(effects, "_hash_grid", return_value=hashes):
            indices = EffectProcessor(
                "type-echo", effect_text="A"
            ).apply(frame, 0.0).cells.glyph_indices
        self.assertFalse(indices.any())

        white = np.full((3, 1, 3), 255, dtype=np.uint8)
        maximum_hash = np.full((3, 1), 255, dtype=np.uint64) << np.uint64(56)
        with mock.patch.object(effects, "_hash_grid", return_value=maximum_hash):
            indices = EffectProcessor(
                "type-echo", effect_text="A"
            ).apply(white, 0.0).cells.glyph_indices
        self.assertNotEqual(int(indices[0, 0]), 0)
        self.assertEqual(int(indices[1, 0]), 0)
        self.assertEqual(int(indices[2, 0]), 0)

    def test_tile_mosaic_uses_integer_block_averages(self):
        frame = np.arange(5 * 7 * 3, dtype=np.uint8).reshape(5, 7, 3)
        output = EffectProcessor("tile-mosaic").apply(frame).rgb
        expected_first = frame[:4, :6].astype(np.uint32).mean(axis=(0, 1)).astype(np.uint8)
        np.testing.assert_array_equal(output[0, 0], expected_first)
        np.testing.assert_array_equal(
            output[:4, :6], np.broadcast_to(output[0, 0], (4, 6, 3))
        )
        np.testing.assert_array_equal(output[4, 6], frame[4, 6])
        self.assertFalse(np.array_equal(output, frame))

    def test_wave_lines_is_time_speed_and_seed_deterministic(self):
        frame = np.arange(24 * 64 * 3, dtype=np.uint8).reshape(24, 64, 3)
        first = EffectProcessor("wave-lines", speed=1.0, seed=4).apply(frame, 1.0).rgb
        repeat = EffectProcessor("wave-lines", speed=1.0, seed=4).apply(frame, 1.0).rgb
        next_tick = EffectProcessor("wave-lines", speed=1.0, seed=4).apply(frame, 1.1).rgb
        fast = EffectProcessor("wave-lines", speed=2.0, seed=4).apply(frame, 0.5).rgb
        changed_seed = EffectProcessor("wave-lines", speed=1.0, seed=5).apply(frame, 1.0).rgb
        np.testing.assert_array_equal(first, repeat)
        np.testing.assert_array_equal(first, fast)
        self.assertFalse(np.array_equal(first, next_tick))
        self.assertFalse(np.array_equal(first, changed_seed))
        flat = np.full((18, 32, 3), 90, dtype=np.uint8)
        flat_output = EffectProcessor("wave-lines").apply(flat, 0.0).rgb
        self.assertGreater(len(np.unique(flat_output[:, :, 0])), 1)

    def test_voronoi_layout_is_seeded_cached_and_resettable(self):
        processor = EffectProcessor("voronoi", seed=91)
        first = processor.apply(self.frame).rgb
        key = next(iter(processor._cache))
        layout = processor._cache[key]
        repeat = processor.apply(self.frame).rgb
        self.assertIs(processor._cache[key], layout)
        np.testing.assert_array_equal(first, repeat)
        changed = EffectProcessor("voronoi", seed=92).apply(self.frame).rgb
        self.assertFalse(np.array_equal(first, changed))
        processor.reset("seek")
        self.assertFalse(processor._cache)
        rebuilt = processor.apply(self.frame).rgb
        np.testing.assert_array_equal(first, rebuilt)
        self.assertIsNot(processor._cache[key], layout)

    def test_voronoi_marks_boundaries_even_on_a_flat_source(self):
        flat = np.full((20, 30, 3), 180, dtype=np.uint8)
        output = EffectProcessor("voronoi", seed=3).apply(flat).rgb
        values = set(np.unique(output[:, :, 0]).tolist())
        self.assertEqual(values, {72, 180})

    def test_selection_clears_voronoi_cache(self):
        processor = EffectProcessor("voronoi")
        processor.apply(self.frame)
        self.assertTrue(processor._cache)
        processor.select("tile-mosaic")
        self.assertFalse(processor._cache)

    def test_afterimage_trails_are_bounded_and_reset(self):
        bright = np.zeros((5, 7, 3), dtype=np.uint8)
        bright[2, 2] = (240, 180, 120)
        dark = np.zeros_like(bright)
        processor = EffectProcessor("afterimage", speed=1.0)
        np.testing.assert_array_equal(
            processor.apply(bright, context(0.0, 0, (5, 7))).rgb, bright
        )
        trail = processor.apply(dark, context(0.325, 1, (5, 7))).rgb
        self.assertTrue(np.all(trail[2, 2] > 0))
        self.assertTrue(np.all(trail[2, 2] < bright[2, 2]))
        self.assertGreaterEqual(float(processor._afterimage_accumulator.min()), 0.0)
        self.assertLessEqual(float(processor._afterimage_accumulator.max()), 255.0)
        processor.reset("seek")
        np.testing.assert_array_equal(
            processor.apply(dark, context(4.0, 0, (5, 7))).rgb, dark
        )

    def test_afterimage_pause_does_not_advance_history(self):
        first = np.zeros((4, 6, 3), dtype=np.uint8)
        first[:, :2] = 220
        second = np.zeros_like(first)
        second[:, 2:4] = 180
        third = np.zeros_like(first)
        third[:, 4:] = 140
        paused = EffectProcessor("afterimage")
        control = EffectProcessor("afterimage")
        for processor in (paused, control):
            processor.apply(first, context(0.0, 0, (4, 6)))
            processor.apply(second, context(0.2, 1, (4, 6)))
        before = paused._afterimage_accumulator.copy()
        held = paused.apply(second, context(0.2, 1, (4, 6), advance=False)).rgb
        held_again = paused.apply(
            second, context(0.2, 1, (4, 6), advance=False)
        ).rgb
        np.testing.assert_array_equal(held, held_again)
        np.testing.assert_array_equal(paused._afterimage_accumulator, before)
        resumed = paused.apply(third, context(0.5, 2, (4, 6))).rgb
        uninterrupted = control.apply(third, context(0.5, 2, (4, 6))).rgb
        np.testing.assert_array_equal(resumed, uninterrupted)

    def test_afterimage_same_sequence_is_idempotent(self):
        processor = EffectProcessor("afterimage")
        processor.apply(self.frame, context(1.0, 5))
        before = processor._afterimage_accumulator.copy()
        output = processor.apply(
            np.zeros_like(self.frame), context(2.0, 5)
        ).rgb
        np.testing.assert_array_equal(processor._afterimage_accumulator, before)
        np.testing.assert_array_equal(output, self.frame)

    def test_afterimage_ignores_non_increasing_timestamp(self):
        processor = EffectProcessor("afterimage")
        processor.apply(self.frame, context(2.0, 4))
        accumulator = processor._afterimage_accumulator.copy()
        previous = processor._afterimage_previous.copy()
        changed = np.zeros_like(self.frame)
        for time_value, sequence in ((2.0, 5), (1.5, 6)):
            with self.subTest(time=time_value, sequence=sequence):
                output = processor.apply(
                    changed, context(time_value, sequence)
                ).rgb
                np.testing.assert_array_equal(
                    processor._afterimage_accumulator, accumulator
                )
                np.testing.assert_array_equal(
                    processor._afterimage_previous, previous
                )
                np.testing.assert_array_equal(output, self.frame)
                self.assertEqual(processor._afterimage_time, 2.0)
                self.assertEqual(processor._afterimage_sequence, 4)

    def test_afterimage_tracks_motion_differences_not_static_frame_tone(self):
        original = np.full((6, 8, 3), 40, dtype=np.uint8)
        original[2, 2] = (240, 120, 60)
        moved = original.copy()
        moved[2, 2] = 40
        moved[2, 5] = (240, 120, 60)
        processor = EffectProcessor("afterimage")
        processor.apply(original, context(0.0, 0, (6, 8)))
        output = processor.apply(moved, context(0.1, 1, (6, 8))).rgb
        self.assertTrue(np.any(output[2, 2] > moved[2, 2]))
        self.assertTrue(np.all(processor._afterimage_accumulator[0, 0] == 0))


if __name__ == "__main__":
    unittest.main()

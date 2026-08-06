import argparse
import hashlib
import io
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE = runpy.run_path(str(ROOT / "yt-ascii"), run_name="yt_ascii_test")


class ChunkedStream:
    def __init__(self, data, chunks=(1, 2, 3)):
        self.data = memoryview(data)
        self.offset = 0
        self.chunks = chunks
        self.calls = 0
        self.max_request = 0

    def readinto(self, target):
        self.max_request = max(self.max_request, len(target))
        if self.offset >= len(self.data):
            return 0
        limit = self.chunks[self.calls % len(self.chunks)]
        self.calls += 1
        count = min(limit, len(target), len(self.data) - self.offset)
        target[:count] = self.data[self.offset:self.offset + count]
        self.offset += count
        return count


class FrameIOTests(unittest.TestCase):
    def test_read_exact_into_handles_short_reads(self):
        stream = ChunkedStream(b"abcdefgh")
        buffer = bytearray(8)
        self.assertEqual(CORE["read_exact_into"](stream, buffer), 8)
        self.assertEqual(bytes(buffer), b"abcdefgh")
        self.assertLessEqual(stream.max_request, len(buffer))

    def test_read_exact_into_reports_partial_eof(self):
        stream = ChunkedStream(b"abc")
        buffer = bytearray(5)
        self.assertEqual(CORE["read_exact_into"](stream, buffer), 3)
        self.assertEqual(bytes(buffer[:3]), b"abc")

    def test_discard_frames_is_bounded_and_counts_only_complete_frames(self):
        frame_size = 5
        stream = ChunkedStream(b"a" * (frame_size * 3 + 2), chunks=(2, 1, 3))
        scratch = bytearray(frame_size)
        self.assertEqual(CORE["discard_frames"](stream, 8, scratch), 3)
        self.assertLessEqual(stream.max_request, frame_size)

    def test_zero_discard_performs_no_read(self):
        stream = ChunkedStream(b"abc")
        self.assertEqual(CORE["discard_frames"](stream, 0, bytearray(2)), 0)
        self.assertEqual(stream.calls, 0)


class ProcessTests(unittest.TestCase):
    class FakeProcess:
        def __init__(self, name, events, timeout_once=False, exited=False):
            self.name = name
            self.pid = 100
            self.events = events
            self.timeout_once = timeout_once
            self.exited = exited
            self.waits = 0

        def poll(self):
            return 0 if self.exited else None

        def terminate(self):
            self.events.append((self.name, "terminate"))

        def wait(self, timeout=None):
            self.events.append((self.name, "wait"))
            self.waits += 1
            if self.timeout_once and self.waits == 1:
                raise subprocess.TimeoutExpired(self.name, timeout)
            self.exited = True
            return 0

        def kill(self):
            self.events.append((self.name, "kill"))

    def test_processes_terminate_together_before_waiting(self):
        events = []
        first = self.FakeProcess("video", events)
        second = self.FakeProcess("audio", events)
        globals_ = CORE["stop_procs"].__globals__
        with mock.patch.dict(globals_, {"_CAN_SUSPEND": False}):
            CORE["stop_procs"](first, second)
        first_wait = min(i for i, event in enumerate(events) if event[1] == "wait")
        terminations = [i for i, event in enumerate(events) if event[1] == "terminate"]
        self.assertTrue(all(index < first_wait for index in terminations))

    def test_suspended_processes_resume_before_termination(self):
        events = []
        first = self.FakeProcess("video", events)
        second = self.FakeProcess("audio", events)
        resume_token = object()
        globals_ = CORE["stop_procs"].__globals__
        with mock.patch.dict(globals_, {
            "_CAN_SUSPEND": True,
            "signal": SimpleNamespace(SIGCONT=resume_token),
            "signal_proc": lambda process, sig: events.append((process.name, sig)),
        }):
            CORE["stop_procs"](first, second)
        first_termination = min(
            i for i, event in enumerate(events) if event[1] == "terminate"
        )
        resumes = [i for i, event in enumerate(events) if event[1] is resume_token]
        self.assertEqual(len(resumes), 2)
        self.assertTrue(all(index < first_termination for index in resumes))

    def test_timed_out_process_is_killed_then_reaped(self):
        events = []
        process = self.FakeProcess("video", events, timeout_once=True)
        globals_ = CORE["stop_procs"].__globals__
        with mock.patch.dict(globals_, {"_CAN_SUSPEND": False}):
            CORE["stop_procs"](process)
        self.assertIn(("video", "kill"), events)
        self.assertEqual(events[-1], ("video", "wait"))

    def test_none_and_exited_processes_are_ignored(self):
        events = []
        CORE["stop_procs"](None, self.FakeProcess("done", events, exited=True))
        self.assertEqual(events, [])

    @unittest.skipUnless(
        os.name == "posix" and hasattr(signal, "SIGSTOP"),
        "requires POSIX job-control signals",
    )
    def test_suspended_child_is_resumed_and_reaped_quickly(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            os.kill(child.pid, signal.SIGSTOP)
            started = time.monotonic()
            CORE["stop_procs"](child)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


class CommandAndOutputTests(unittest.TestCase):
    def test_help_is_dependency_free_and_lists_styles_effects_and_updates(self):
        output = io.StringIO()
        globals_ = CORE["main"].__globals__
        dependency_check = mock.Mock()
        with mock.patch.object(
                    globals_["sys"], "argv", ["yt-ascii", "--help"]
                ), \
                mock.patch.object(globals_["sys"], "stdout", output), \
                mock.patch.object(globals_["sys"], "stderr", io.StringIO()), \
                mock.patch.dict(globals_, {"check_deps": dependency_check}):
            with self.assertRaises(SystemExit) as raised:
                CORE["main"]()
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--render", output.getvalue())
        self.assertIn("cells", output.getvalue())
        self.assertIn("half-block", output.getvalue())
        self.assertIn("--style", output.getvalue())
        self.assertIn("--effect", output.getvalue())
        self.assertIn("cycle character palette", output.getvalue())
        self.assertIn("--check-update", output.getvalue())
        self.assertIn("--update", output.getvalue())
        self.assertIn("--diagnostics-json", output.getvalue())
        self.assertIn("--diagnostics-duration", output.getvalue())
        dependency_check.assert_not_called()

    def test_version_defaults_to_source_and_skips_dependency_checks(self):
        output = io.StringIO()
        globals_ = CORE["main"].__globals__
        dependency_check = mock.Mock()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(
                    globals_["sys"], "argv", ["yt-ascii", "--version"]
                ), \
                mock.patch.object(globals_["sys"], "stdout", output), \
                mock.patch.object(globals_["sys"], "stderr", io.StringIO()), \
                mock.patch.dict(globals_, {"check_deps": dependency_check}):
            with self.assertRaises(SystemExit) as raised:
                CORE["main"]()
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), "yt-ascii 0.5.0 (source)\n")
        dependency_check.assert_not_called()

    def test_interactive_visual_selections_persist_to_the_next_video(self):
        args = SimpleNamespace(
            url=None, style="classic", effect="none", palette="dense",
            chars=" .@", self_test=False,
        )
        seen = []
        urls = iter(("first", "second"))
        globals_ = CORE["main"].__globals__

        def prompt():
            try:
                return next(urls)
            except StopIteration as error:
                raise SystemExit(0) from error

        def run(current):
            seen.append((
                current.url, current.palette, current.chars,
                current.style, current.effect,
            ))
            if current.url == "first":
                current.palette = "matrix"
                current.chars = None
                current.style = "posterize"
                current.effect = "pixelate"

        with mock.patch.dict(globals_, {
            "parse_args": lambda: args,
            "check_deps": lambda: None,
            "prompt_for_url": prompt,
            "run": run,
        }):
            with self.assertRaises(SystemExit) as raised:
                CORE["main"]()
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(
            seen,
            [
                ("first", "dense", " .@", "classic", "none"),
                ("second", "matrix", None, "posterize", "pixelate"),
            ],
        )

    def test_version_reports_valid_installer_refs(self):
        for install_ref in ("v0.3.0", "v12.34.567", "edge"):
            with self.subTest(install_ref=install_ref), mock.patch.dict(
                os.environ, {"YTASCII_INSTALL_REF": install_ref}
            ):
                self.assertEqual(
                    CORE["version_text"](),
                    f"yt-ascii 0.5.0 ({install_ref})",
                )

    def test_version_rejects_untrusted_installer_refs(self):
        unsafe = (
            "",
            "main",
            "v0.3",
            "v01.2.3",
            "v0.3.0-dev",
            "edge\x1b[2J",
            "v0.3.0\nforged",
            "v" + "1" * 200 + ".2.3",
        )
        for install_ref in unsafe:
            with self.subTest(install_ref=install_ref), mock.patch.dict(
                os.environ, {"YTASCII_INSTALL_REF": install_ref}
            ):
                self.assertEqual(
                    CORE["version_text"](),
                    "yt-ascii 0.5.0 (source)",
                )

    def test_spawn_video_emits_scaled_rgb24(self):
        info = {"video": "input.mp4"}
        globals_ = CORE["spawn_video"].__globals__
        with mock.patch.dict(globals_, {
            "find_ffmpeg": lambda: "ffmpeg",
            "RECONNECT_FLAGS": [],
        }), mock.patch.object(subprocess, "Popen") as popen:
            CORE["spawn_video"](info, 30, 80, 24, 0)
            command = popen.call_args.args[0]
        pixel_index = command.index("-pix_fmt")
        self.assertEqual(command[pixel_index + 1], "rgb24")
        self.assertEqual(
            command[command.index("-vf") + 1],
            "fps=30,scale=80:24:flags=fast_bilinear",
        )

    def test_spawn_video_diagnostics_capture_is_opt_in(self):
        stream = object()
        capture = SimpleNamespace(stream=stream)
        diagnostics = SimpleNamespace(
            new_ffmpeg_capture=mock.Mock(return_value=capture),
            consume_ffmpeg_capture=mock.Mock(),
        )
        process = SimpleNamespace()
        info = {"video": "https://media.example/stream"}
        globals_ = CORE["spawn_video"].__globals__
        with mock.patch.dict(globals_, {
            "find_ffmpeg": lambda: "ffmpeg",
            "RECONNECT_FLAGS": ["-reconnect", "1"],
        }), mock.patch.object(
            subprocess, "Popen", return_value=process
        ) as popen:
            result = CORE["spawn_video"](
                info, 60, 240, 68, 0, diagnostics
            )
        self.assertIs(result, process)
        self.assertEqual(
            popen.call_args.args[0],
            [
                "ffmpeg", "-loglevel", "info", "-nostats", "-benchmark",
                "-reconnect", "1", "-i", "https://media.example/stream",
                "-an",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-vf",
                "fps=60,scale=240:68:flags=fast_bilinear", "-",
            ],
        )
        self.assertIs(popen.call_args.kwargs["stderr"], stream)
        self.assertIs(process._ytascii_diagnostics_capture, capture)

    def test_decoder_diagnostics_capture_is_consumed_once_after_reap(self):
        capture = object()
        process = SimpleNamespace(_ytascii_diagnostics_capture=capture)
        diagnostics = SimpleNamespace(consume_ffmpeg_capture=mock.Mock())
        CORE["consume_video_diagnostics"](process, diagnostics)
        CORE["consume_video_diagnostics"](process, diagnostics)
        diagnostics.consume_ffmpeg_capture.assert_called_once_with(capture)
        self.assertIsNone(process._ytascii_diagnostics_capture)

    def test_terminal_frame_has_text_stream_fallback(self):
        output = io.StringIO()
        globals_ = CORE["write_terminal_frame"].__globals__
        with mock.patch.object(globals_["sys"], "stdout", output):
            CORE["write_terminal_frame"]("λ".encode("utf-8"), "status")
        self.assertEqual(output.getvalue(), "\x1b[Hλ\nstatus\x1b[K")

    def test_terminal_frame_writes_and_flushes_binary_buffer(self):
        class Buffer(io.BytesIO):
            def __init__(self):
                super().__init__()
                self.flushes = 0

            def flush(self):
                self.flushes += 1
                super().flush()

        binary = Buffer()
        output = mock.Mock(buffer=binary)
        globals_ = CORE["write_terminal_frame"].__globals__
        with mock.patch.object(globals_["sys"], "stdout", output):
            CORE["write_terminal_frame"]("λ".encode("utf-8"), "status")
        self.assertEqual(binary.getvalue(), b"\x1b[H\xce\xbb\nstatus\x1b[K")
        self.assertEqual(binary.flushes, 1)
        output.write.assert_not_called()

    def test_terminal_diagnostics_preserve_exact_frame_bytes(self):
        class Timer:
            def __enter__(self):
                events.append("start")

            def __exit__(self, *_args):
                events.append("stop")

        events = []
        binary = io.BytesIO()
        output = mock.Mock(buffer=binary)
        diagnostics = SimpleNamespace(
            timer=lambda stage: Timer(), increment=mock.Mock()
        )
        globals_ = CORE["write_terminal_frame"].__globals__
        with mock.patch.object(globals_["sys"], "stdout", output):
            size = CORE["write_terminal_frame"](
                "λ".encode("utf-8"), "status", diagnostics
            )
        expected = b"\x1b[H\xce\xbb\nstatus\x1b[K"
        self.assertEqual(binary.getvalue(), expected)
        self.assertEqual(size, len(expected))
        self.assertEqual(events, ["start", "stop"])
        diagnostics.increment.assert_not_called()

    def test_terminal_diagnostics_flag_empty_or_nul_frame_bodies(self):
        class Timer:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

        for body in (b"", b"A\x00B"):
            with self.subTest(body=body):
                binary = io.BytesIO()
                diagnostics = SimpleNamespace(
                    timer=lambda _stage: Timer(), increment=mock.Mock()
                )
                globals_ = CORE["write_terminal_frame"].__globals__
                with mock.patch.object(
                    globals_["sys"], "stdout", mock.Mock(buffer=binary)
                ):
                    CORE["write_terminal_frame"](body, "status", diagnostics)
                diagnostics.increment.assert_called_once_with(
                    "output_corruption"
                )

    def test_style_effect_and_palette_cli_contract(self):
        self.assertEqual(
            CORE["PALETTE_NAMES"],
            (
                "simple", "dense", "blocks", "binary", "numbers",
                "symbols", "matrix",
            ),
        )
        self.assertEqual(CORE["PALETTES"]["binary"], " 01")
        self.assertEqual(CORE["PALETTES"]["numbers"], " 123456789")
        self.assertEqual(CORE["PALETTES"]["symbols"], " .,:;!|?*#@")
        self.assertEqual(CORE["PALETTES"]["matrix"], " 01:=+*<>|/")

        globals_ = CORE["parse_args"].__globals__
        with mock.patch.object(globals_["sys"], "argv", ["yt-ascii"]):
            args = CORE["parse_args"]()
            self.assertEqual(args.render, "chars")
            self.assertEqual(args.style, "classic")
            self.assertEqual(args.effect, "none")
            self.assertEqual(args.effect_glyphs, "ascii")
            self.assertEqual(args.effect_speed, 1.0)
            self.assertEqual(args.effect_seed, 0)
            self.assertEqual(args.effect_text, "YTASCII")
            self.assertEqual(args.rain_chars, "ascii")
            self.assertTrue(
                all(ord(char) < 128 for char in CORE["RAIN_CHARSETS"]["ascii"])
            )
            self.assertFalse(args.update)
            self.assertFalse(args.check_update)
            self.assertFalse(args.no_update_check)
        cycle = CORE["_next_palette_name"]
        self.assertEqual(cycle("custom"), "simple")
        self.assertEqual(cycle("simple"), "dense")
        self.assertEqual(cycle("matrix"), "simple")
        with self.assertRaisesRegex(ValueError, "unknown palette"):
            cycle("missing")
        for style in CORE["STYLE_NAMES"]:
            with self.subTest(style=style), mock.patch.object(
                globals_["sys"], "argv", ["yt-ascii", "--style", style]
            ):
                self.assertEqual(CORE["parse_args"]().style, style)
        for effect in CORE["EFFECT_NAMES"]:
            with self.subTest(effect=effect), mock.patch.object(
                globals_["sys"], "argv", ["yt-ascii", "--effect", effect]
            ):
                self.assertEqual(CORE["parse_args"]().effect, effect)
        with mock.patch.object(
            globals_["sys"],
            "argv",
            [
                "yt-ascii", "--effect", "terminal-hud",
                "--effect-glyphs", "unicode",
                "--effect-speed", "1.5", "--effect-seed", "-9",
                "--effect-text", "YT λ",
            ],
        ):
            args = CORE["parse_args"]()
        self.assertEqual(
            (
                args.effect,
                args.effect_glyphs,
                args.effect_speed,
                args.effect_seed,
                args.effect_text,
            ),
            ("terminal-hud", "unicode", 1.5, -9, "YT λ"),
        )

    def test_render_cli_aliases_fallback_and_effect_compatibility(self):
        globals_ = CORE["parse_args"].__globals__
        for render_mode in CORE["RENDER_MODES"]:
            with self.subTest(render_mode=render_mode), mock.patch.object(
                globals_["sys"],
                "argv",
                ["yt-ascii", "--render", render_mode],
            ):
                args = CORE["parse_args"]()
                self.assertEqual(args.render, render_mode)
        with mock.patch.object(
            globals_["sys"], "argv", ["yt-ascii", "--pixels"]
        ):
            args = CORE["parse_args"]()
        self.assertEqual(args.render, "half-block")
        self.assertTrue(args.pixels)

        for alias, canonical in CORE["STYLE_ALIASES"].items():
            with self.subTest(style_alias=alias), mock.patch.object(
                globals_["sys"],
                "argv",
                ["yt-ascii", "--style", alias],
            ):
                self.assertEqual(CORE["parse_args"]().style, canonical)

        for alias, canonical in CORE["EFFECT_ALIASES"].items():
            with self.subTest(alias=alias), mock.patch.object(
                globals_["sys"],
                "argv",
                ["yt-ascii", "--effect", alias],
            ):
                self.assertEqual(CORE["parse_args"]().effect, canonical)

        migrations = (
            (["--style", "glitch"], "classic", "glitch"),
            (["--effect", "posterize"], "posterize", "none"),
            (["--effect", "edge-glow"], "edge-glow", "none"),
            (["--effect", "ordered-dither"], "ordered-dither", "none"),
            (["--effect", "error-diffusion"], "error-diffusion", "none"),
            (["--effect", "duotone"], "two-tone", "none"),
            (["--effect", "poster-press"], "posterize", "none"),
        )
        for options, style, effect in migrations:
            with self.subTest(migration=options), mock.patch.object(
                globals_["sys"], "argv", ["yt-ascii", *options]
            ):
                args = CORE["parse_args"]()
                self.assertEqual((args.style, args.effect), (style, effect))

        invalid = (
            ["--pixels", "--render", "cells"],
            ["--render", "cells", "--effect", "digital-rain"],
            ["--render", "half-block", "--effect", "terminal-hud"],
            ["--style", "glitch", "--effect", "wave"],
            ["--style", "riso", "--effect", "posterize"],
        )
        for options in invalid:
            with self.subTest(options=options), mock.patch.object(
                globals_["sys"], "argv", ["yt-ascii", *options]
            ), mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    CORE["parse_args"]()
                self.assertEqual(raised.exception.code, 2)

        # The explicitly documented no-color path has already resolved these
        # graphical requests to chars, so text-specific output stays visible.
        with mock.patch.object(
            globals_["sys"],
            "argv",
            [
                "yt-ascii", "--render", "cells", "--no-color",
                "--effect", "digital-rain",
            ],
        ):
            args = CORE["parse_args"]()
        self.assertEqual(args.render, "cells")
        self.assertEqual(args.effect, "digital-rain")
        self.assertEqual(
            CORE["_effective_render_mode"](args.render, not args.no_color),
            "chars",
        )

    def test_diagnostics_cli_requires_a_new_report_and_explicit_playback(self):
        globals_ = CORE["parse_args"].__globals__
        with tempfile.TemporaryDirectory() as directory:
            report = str(Path(directory) / "report.json")
            with mock.patch.object(
                globals_["sys"],
                "argv",
                [
                    "yt-ascii", "https://example.test/watch",
                    "--diagnostics-json", report,
                    "--diagnostics-warmup", "1.5",
                    "--diagnostics-duration", "2.25",
                ],
            ):
                args = CORE["parse_args"]()
            self.assertEqual(args.diagnostics_json, report)
            self.assertEqual(args.diagnostics_warmup, 1.5)
            self.assertEqual(args.diagnostics_duration, 2.25)

            existing = Path(directory) / "existing.json"
            existing.touch()
            invalid = (
                ["--diagnostics-duration", "1"],
                ["--diagnostics-json", report],
                [
                    "https://example.test/watch", "--self-test",
                    "--diagnostics-json", report,
                ],
                [
                    "https://example.test/watch", "--update",
                    "--diagnostics-json", report,
                ],
                [
                    "https://example.test/watch", "--diagnostics-json",
                    str(existing),
                ],
            )
            for options in invalid:
                with self.subTest(options=options), mock.patch.object(
                    globals_["sys"], "argv", ["yt-ascii", *options]
                ), mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        CORE["parse_args"]()
                    self.assertEqual(raised.exception.code, 2)

    def test_diagnostics_cli_rejects_nonfinite_and_out_of_range_times(self):
        globals_ = CORE["parse_args"].__globals__
        with tempfile.TemporaryDirectory() as directory:
            report = str(Path(directory) / "report.json")
            cases = (
                ("--diagnostics-warmup", "-1"),
                ("--diagnostics-warmup", "nan"),
                ("--diagnostics-warmup", "inf"),
                ("--diagnostics-duration", "0"),
                ("--diagnostics-duration", "-1"),
                ("--diagnostics-duration", "nan"),
                ("--diagnostics-duration", "inf"),
            )
            for option, value in cases:
                argv = [
                    "yt-ascii", "https://example.test/watch",
                    "--diagnostics-json", report, option, value,
                ]
                with self.subTest(option=option, value=value), \
                        mock.patch.object(globals_["sys"], "argv", argv), \
                        mock.patch.object(
                            globals_["sys"], "stderr", io.StringIO()
                        ):
                    with self.assertRaises(SystemExit) as raised:
                        CORE["parse_args"]()
                    self.assertEqual(raised.exception.code, 2)

    def test_diagnostics_module_is_not_imported_on_the_disabled_path(self):
        args = SimpleNamespace(diagnostics_json=None)
        worker = mock.Mock(return_value="end_of_stream")
        globals_ = CORE["run"].__globals__
        with mock.patch.dict(globals_, {"_run_playback": worker}), \
                mock.patch.dict(
                    sys.modules, {"yt_ascii_diagnostics": None}
                ):
            result = CORE["run"](args, "update-handle")
        self.assertIsNone(result)
        worker.assert_called_once_with(args, "update-handle")

    def test_diagnostics_finalize_after_playback_and_on_errors(self):
        args = SimpleNamespace(
            diagnostics_json="new-report.json",
            diagnostics_warmup=1.0,
            diagnostics_duration=2.0,
            fps=30,
            width=80,
            height=24,
            max_res=480,
            no_color=False,
            pixels=False,
            no_audio=True,
            eight_bit=False,
            style="classic",
            effect="none",
            effect_glyphs="ascii",
            scatter=False,
            rain=False,
        )
        calls = []

        class RecordingDiagnostics:
            def __init__(self, path, warmup, duration, **metadata):
                calls.append(
                    ("init", path, warmup, duration, metadata)
                )

            def finalize(self, **result):
                calls.append(("finalize", result))

        module = SimpleNamespace(PlaybackDiagnostics=RecordingDiagnostics)
        globals_ = CORE["run"].__globals__

        def playback(*_args, **_kwargs):
            calls.append(("cleanup_complete",))
            return "duration"

        with mock.patch.dict(sys.modules, {"yt_ascii_diagnostics": module}), \
                mock.patch.dict(globals_, {"_run_playback": playback}):
            CORE["run"](args)
        self.assertEqual(calls[-2:], [
            ("cleanup_complete",),
            ("finalize", {"exit_reason": "duration"}),
        ])
        metadata = calls[0][-1]
        self.assertNotIn("url", metadata["source"])
        self.assertNotIn("effect_text", metadata["config"])
        self.assertEqual(
            metadata["config"]["effect_text_sha256"],
            hashlib.sha256(b"YTASCII").hexdigest(),
        )
        self.assertEqual(metadata["config"]["render_backend"], "chars")
        self.assertEqual(
            metadata["config"]["effective_render_backend"], "chars"
        )
        self.assertNotIn("pixels", metadata["config"])
        self.assertNotIn("presentation", metadata["config"])
        self.assertEqual(metadata["profile"], "ordinary")

        # Redirection is a sink even when the parent shell happens to be tmux.
        config_globals = CORE["_diagnostics_config"].__globals__
        with mock.patch.dict(os.environ, {"TMUX": "session"}), \
                mock.patch.object(
                    config_globals["sys"], "stdout",
                    SimpleNamespace(isatty=lambda: False),
                ):
            self.assertEqual(
                CORE["_diagnostics_config"](args)["output_environment"],
                "sink",
            )
        with mock.patch.dict(os.environ, {"TMUX": "session"}), \
                mock.patch.object(
                    config_globals["sys"], "stdout",
                    SimpleNamespace(isatty=lambda: True),
                ):
            self.assertEqual(
                CORE["_diagnostics_config"](args)["output_environment"],
                "tmux",
            )

        calls.clear()
        args.fps, args.width, args.height = 60, 240, 68
        with mock.patch.dict(sys.modules, {"yt_ascii_diagnostics": module}), \
                mock.patch.dict(globals_, {"_run_playback": playback}):
            CORE["run"](args)
        self.assertEqual(calls[0][-1]["profile"], "stress")

        calls.clear()
        def failing_playback(*_args, **_kwargs):
            calls.append(("cleanup_complete",))
            raise RuntimeError("playback failure")

        with mock.patch.dict(sys.modules, {"yt_ascii_diagnostics": module}), \
                mock.patch.dict(globals_, {"_run_playback": failing_playback}):
            with self.assertRaisesRegex(RuntimeError, "playback failure"):
                CORE["run"](args)
        self.assertEqual(calls[-1], (
            "finalize",
            {"exit_reason": "error", "error_type": "RuntimeError"},
        ))

    def test_effect_text_cli_validation_is_mode_aware_and_strict(self):
        globals_ = CORE["parse_args"].__globals__
        valid = (
            (["--effect-text", "A B"], "A B"),
            (["--effect-glyphs", "unicode", "--effect-text", "λ"], "λ"),
            (["--effect-text", "A" * 253], "A" * 253),
        )
        for options, expected in valid:
            with self.subTest(options=options), mock.patch.object(
                globals_["sys"], "argv", ["yt-ascii", *options]
            ):
                self.assertEqual(CORE["parse_args"]().effect_text, expected)

        invalid = (
            ["--effect-text", ""],
            ["--effect-text", "   "],
            ["--effect-text", "A" * 254],
            ["--effect-text", "λ"],
            ["--effect-glyphs", "unicode", "--effect-text", "e\u0301"],
            ["--effect-glyphs", "unicode", "--effect-text", "A\nB"],
            ["--effect-glyphs", "unicode", "--effect-text", "A\u200dB"],
            ["--effect-glyphs", "unicode", "--effect-text", "界"],
            ["--effect-glyphs", "unicode", "--effect-text", "א"],
        )
        for options in invalid:
            with self.subTest(options=options), mock.patch.object(
                globals_["sys"], "argv", ["yt-ascii", *options]
            ), mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    CORE["parse_args"]()
                self.assertEqual(raised.exception.code, 2)

    def test_update_cli_actions_are_explicit_and_mutually_exclusive(self):
        globals_ = CORE["parse_args"].__globals__
        cases = (
            ("--update", (True, False, False)),
            ("--check-update", (False, True, False)),
            ("--no-update-check", (False, False, True)),
        )
        for option, expected in cases:
            with self.subTest(option=option), mock.patch.object(
                globals_["sys"], "argv", ["yt-ascii", option]
            ):
                args = CORE["parse_args"]()
                self.assertEqual(
                    (args.update, args.check_update, args.no_update_check),
                    expected,
                )
        with mock.patch.object(
            globals_["sys"],
            "argv",
            ["yt-ascii", "--update", "--check-update"],
        ), mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                CORE["parse_args"]()
        self.assertEqual(raised.exception.code, 2)

    def test_explicit_update_runs_before_dependency_checks(self):
        args = SimpleNamespace(
            url=None,
            update=True,
            check_update=False,
            self_test=False,
        )
        globals_ = CORE["main"].__globals__
        update_action = mock.Mock(return_value=7)
        dependency_check = mock.Mock()
        with mock.patch.dict(globals_, {
            "parse_args": lambda: args,
            "run_update_action": update_action,
            "check_deps": dependency_check,
        }):
            with self.assertRaises(SystemExit) as raised:
                CORE["main"]()
        self.assertEqual(raised.exception.code, 7)
        update_action.assert_called_once_with(args)
        dependency_check.assert_not_called()

    def test_normal_launch_starts_one_best_effort_update_check(self):
        args = SimpleNamespace(
            url="fixture",
            update=False,
            check_update=False,
            self_test=False,
        )
        handle = object()
        globals_ = CORE["main"].__globals__
        starter = mock.Mock(return_value=handle)
        runner = mock.Mock()
        with mock.patch.dict(globals_, {
            "parse_args": lambda: args,
            "start_automatic_update": starter,
            "check_deps": lambda: None,
            "run": runner,
        }):
            CORE["main"]()
        starter.assert_called_once_with(args)
        runner.assert_called_once_with(args, handle)

    def test_self_test_does_not_start_an_automatic_update_check(self):
        args = SimpleNamespace(
            url=None,
            update=False,
            check_update=False,
            self_test=True,
        )
        globals_ = CORE["main"].__globals__
        starter = mock.Mock()
        self_test = mock.Mock()
        with mock.patch.dict(globals_, {
            "parse_args": lambda: args,
            "start_automatic_update": starter,
            "check_deps": lambda: None,
            "self_test": self_test,
        }):
            CORE["main"]()
        starter.assert_not_called()
        self_test.assert_called_once_with()

    def test_automatic_update_opt_outs_do_not_import_the_updater(self):
        args = SimpleNamespace(no_update_check=True)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(CORE["start_automatic_update"](args))
        args.no_update_check = False
        with mock.patch.dict(
            os.environ, {"YTASCII_NO_UPDATE_CHECK": "1"}, clear=True
        ):
            self.assertIsNone(CORE["start_automatic_update"](args))

    def test_ready_automatic_update_notice_is_plain_stderr_text(self):
        status = SimpleNamespace(display="update available: v0.4.0 -> v0.5.0")
        check = SimpleNamespace(consume=mock.Mock(return_value=status))
        output = io.StringIO()
        globals_ = CORE["report_automatic_update"].__globals__
        with mock.patch.object(globals_["sys"], "stderr", output):
            CORE["report_automatic_update"](check)
        self.assertEqual(
            output.getvalue(),
            "yt-ascii: update available: v0.4.0 -> v0.5.0; "
            "run yt-ascii --update\n",
        )
        check.consume.assert_called_once_with()

    def test_manual_update_check_uses_managed_install_and_long_timeout(self):
        class FakeUpdateError(RuntimeError):
            pass

        installation = object()
        status = SimpleNamespace(
            ok=True,
            supported=True,
            display="up to date (v0.4.0)",
        )
        updater = SimpleNamespace(
            UpdateError=FakeUpdateError,
            discover_install=mock.Mock(return_value=installation),
            check_for_update=mock.Mock(return_value=status),
        )
        args = SimpleNamespace(url=None, check_update=True)
        output = io.StringIO()
        errors = io.StringIO()
        globals_ = CORE["run_update_action"].__globals__
        with mock.patch.dict(sys.modules, {"yt_ascii_update": updater}), \
                mock.patch.object(globals_["sys"], "stdout", output), \
                mock.patch.object(globals_["sys"], "stderr", errors):
            result = CORE["run_update_action"](args)
        self.assertEqual(result, 0)
        self.assertEqual(
            output.getvalue(),
            "yt-ascii: up to date (v0.4.0)\n",
        )
        self.assertEqual(errors.getvalue(), "")
        updater.discover_install.assert_called_once_with()
        updater.check_for_update.assert_called_once_with(
            installation, timeout=10.0
        )

    def test_manual_update_delegates_and_propagates_installer_status(self):
        class FakeUpdateError(RuntimeError):
            pass

        installation = object()
        updater = SimpleNamespace(
            UpdateError=FakeUpdateError,
            discover_install=mock.Mock(return_value=installation),
            delegate_update=mock.Mock(return_value=9),
        )
        args = SimpleNamespace(url=None, check_update=False)
        with mock.patch.dict(sys.modules, {"yt_ascii_update": updater}):
            result = CORE["run_update_action"](args)
        self.assertEqual(result, 9)
        updater.delegate_update.assert_called_once_with(installation)

    def test_incomplete_updater_module_is_a_clean_explicit_failure(self):
        args = SimpleNamespace(url=None, check_update=True)
        errors = io.StringIO()
        globals_ = CORE["run_update_action"].__globals__
        with mock.patch.dict(
            sys.modules, {"yt_ascii_update": SimpleNamespace()}
        ), mock.patch.object(globals_["sys"], "stderr", errors):
            result = CORE["run_update_action"](args)
        self.assertEqual(result, 1)
        self.assertIn("updater failed", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_automatic_update_starts_with_short_timeout(self):
        class FakeUpdateError(RuntimeError):
            pass

        installation = object()
        handle = object()
        updater = SimpleNamespace(
            UpdateError=FakeUpdateError,
            discover_install=mock.Mock(return_value=installation),
            start_auto_check=mock.Mock(return_value=handle),
        )
        args = SimpleNamespace(no_update_check=False)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.dict(
            sys.modules, {"yt_ascii_update": updater}
        ):
            result = CORE["start_automatic_update"](args)
        self.assertIs(result, handle)
        updater.discover_install.assert_called_once_with()
        updater.start_auto_check.assert_called_once_with(
            installation, timeout=2.0
        )

    def test_automatic_update_setup_failures_never_abort_playback(self):
        class FakeUpdateError(RuntimeError):
            pass

        updater = SimpleNamespace(
            UpdateError=FakeUpdateError,
            discover_install=mock.Mock(return_value=object()),
            start_auto_check=mock.Mock(
                side_effect=RuntimeError("thread unavailable")
            ),
        )
        args = SimpleNamespace(no_update_check=False)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.dict(
            sys.modules, {"yt_ascii_update": updater}
        ):
            self.assertIsNone(CORE["start_automatic_update"](args))

    def test_effect_speed_rejects_nonpositive_and_nonfinite_values(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                CORE["effect_speed_type"](value)

    def test_geometry_reveal_duration_and_rain_glyphs_are_strict(self):
        globals_ = CORE["parse_args"].__globals__
        invalid_options = (
            ("--width", "0"),
            ("--width", "-1"),
            ("--height", "0"),
            ("--max-res", "-1"),
            ("--scatter-secs", "0"),
            ("--scatter-secs", "nan"),
            ("--rain-secs", "-1"),
            ("--rain-secs", "inf"),
            ("--rain-chars", ""),
            ("--rain-chars", "🚀"),
            ("--rain-chars", "e\u0301"),
            ("--rain-chars", "א"),
        )
        for option, value in invalid_options:
            with self.subTest(option=option, value=value), \
                    mock.patch.object(
                        globals_["sys"], "argv", ["yt-ascii", option, value]
                    ), mock.patch.object(
                        globals_["sys"], "stderr", io.StringIO()
                    ):
                with self.assertRaises(SystemExit) as raised:
                    CORE["parse_args"]()
                self.assertEqual(raised.exception.code, 2)

        with mock.patch.object(
            globals_["sys"],
            "argv",
            [
                "yt-ascii", "--width", "1", "--height", "1",
                "--max-res", "1", "--scatter-secs", "0.1",
                "--rain-secs", "0.1", "--rain-chars", "λЖ",
            ],
        ):
            args = CORE["parse_args"]()
        self.assertEqual(
            (
                args.width, args.height, args.max_res,
                args.scatter_secs, args.rain_secs, args.rain_chars,
            ),
            (1, 1, 1, 0.1, 0.1, "λЖ"),
        )

    def test_status_shows_style_effect_and_controls(self):
        for width in (120, 80):
            with self.subTest(width=width):
                status = CORE["build_status"](
                    30,
                    12.0,
                    {"duration": 60.0, "title": "fixture"},
                    width,
                    paused=False,
                    style="riso",
                    effect="pixelate",
                )
                visible = status.removeprefix("\x1b[0m")
                self.assertLessEqual(len(visible), width)
                self.assertIn("style:riso", visible)
                self.assertIn("effect:pixelate", visible)
                self.assertIn("s:style", visible)
                self.assertIn("e:effect", visible)

        palette_status = CORE["build_status"](
            30,
            12.0,
            {"duration": 60.0, "title": "fixture"},
            240,
            paused=False,
            style="riso",
            effect="pixelate",
            palette="simple",
        )
        self.assertIn("palette:simple", palette_status)
        self.assertIn("p:palette", palette_status)
        graphical_status = CORE["build_status"](
            30,
            12.0,
            {"duration": 60.0, "title": "fixture"},
            240,
            paused=False,
            style="riso",
            effect="pixelate",
            render_backend="cells",
        )
        self.assertNotIn("palette:", graphical_status)
        self.assertNotIn("p:palette", graphical_status)

    def test_status_accounts_for_wide_combining_and_control_characters(self):
        title = "界e\u0301🙂\x1b[2J" * 20
        status = CORE["build_status"](
            30,
            12.0,
            {"duration": 60.0, "title": title},
            120,
            paused=False,
            style="duotone",
        )
        visible = status.removeprefix("\x1b[0m")
        self.assertLessEqual(CORE["_terminal_cell_width"](visible), 120)
        self.assertIn("界e\u0301🙂", visible)
        self.assertNotIn("\x1b", visible)

    def test_status_labels_pixel_to_character_fallback(self):
        status = CORE["build_status"](
            30,
            12.0,
            {"duration": 60.0, "title": "fixture"},
            160,
            paused=False,
            style="classic",
            effect="pixelate",
            pixel_fallback=True,
        )
        self.assertIn("render:half-block→chars", status)
        self.assertIn("effect:pixelate", status)

    def test_keyboard_backends_accept_only_lowercase_palette_style_effect_keys(self):
        globals_ = CORE["_read_keys_windows"].__globals__

        class FakeMsvcrt:
            def __init__(self):
                self.chars = iter(("p", "P", "s", "S", "e", "E", "1"))
                self.pending = True

            def kbhit(self):
                return self.pending

            def getwch(self):
                try:
                    return next(self.chars)
                except StopIteration:
                    self.pending = False
                    return "S"

        fake = FakeMsvcrt()
        with mock.patch.dict(globals_, {"msvcrt": fake}):
            self.assertEqual(
                CORE["_read_keys_windows"](), ["p", "s", "e", "1"]
            )

        readiness = iter((([0], [], []), ([], [], [])))
        fake_select = SimpleNamespace(select=lambda *_args: next(readiness))
        posix_globals = CORE["_read_keys_posix"].__globals__
        with mock.patch.dict(posix_globals, {"select": fake_select}), \
                mock.patch.object(
                    posix_globals["os"], "read", return_value=b"pPsSeE1"
                ):
            self.assertEqual(
                CORE["_read_keys_posix"](0), ["p", "s", "e", "1"]
            )


class NonSuspendResizeTests(unittest.TestCase):
    class FrameStream:
        def readinto(self, target):
            target[:] = b"\x00" * len(target)
            return len(target)

    class FakeProcess:
        next_pid = 1000

        def __init__(self, video=False):
            self.pid = self.next_pid
            type(self).next_pid += 1
            self.stdout = NonSuspendResizeTests.FrameStream() if video else None
            self.exited = False

        def poll(self):
            return 0 if self.exited else None

        def terminate(self):
            self.exited = True

        def wait(self, timeout=None):
            self.exited = True
            return 0

        def kill(self):
            self.exited = True

    @staticmethod
    def args(**overrides):
        values = {
            "url": "fixture",
            "no_color": True,
            "no_audio": True,
            "eight_bit": False,
            "pixels": False,
            "scatter": False,
            "rain": False,
            "scatter_secs": 4.0,
            "rain_secs": 7.5,
            "rain_chars": "matrix",
            "fps": 10,
            "width": 2,
            "height": 2,
            "max_res": 480,
            "palette": "simple",
            "chars": None,
            "style": "classic",
            "effect": "none",
            "effect_glyphs": "ascii",
            "effect_speed": 1.0,
            "effect_seed": 0,
            "effect_text": "YTASCII",
            "diagnostics_json": None,
            "diagnostics_warmup": None,
            "diagnostics_duration": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def info():
        return {
            "title": "fixture",
            "width": 16,
            "height": 9,
            "duration": 0.0,
            "video": "fixture",
            "audio": None,
        }

    def test_diagnostic_duration_stops_after_a_presented_frame_then_finalizes(self):
        # Keep native-extension modules present in ``sys.modules`` when the
        # temporary diagnostics-module patch restores its snapshot.
        import numpy  # noqa: F401
        import yt_ascii_effects  # noqa: F401
        import yt_ascii_frames  # noqa: F401
        import yt_ascii_renderer  # noqa: F401
        import yt_ascii_styles  # noqa: F401

        calls = []
        instances = []

        class Timer:
            def __init__(self, stage):
                self.stage = stage

            def __enter__(self):
                calls.append(("timer_start", self.stage))

            def __exit__(self, *_args):
                calls.append(("timer_end", self.stage))

        class RecordingDiagnostics:
            def __init__(self, *_args, **_kwargs):
                self.frames = 0
                instances.append(self)

            def timer(self, stage, **_kwargs):
                return Timer(stage)

            def register_child(self, process, role):
                calls.append(("child_start", role, process.pid))

            def unregister_child(self, process):
                if process is not None:
                    calls.append(("child_stop", process.pid))

            def set_source_metadata(self, **metadata):
                calls.append(("source", metadata))

            def set_output_geometry(self, **metadata):
                calls.append(("output_geometry", metadata))
                return "ordinary"

            def event(self, name, **fields):
                calls.append(("event", name, fields))

            def increment(self, name, amount=1):
                calls.append(("increment", name, amount))

            def mark_first_frame(self):
                calls.append(("first_frame",))

            def record_frame(self, **metrics):
                self.frames += 1
                calls.append(("frame", metrics))

            def record_timing(self, name, seconds, **_kwargs):
                calls.append(("timing", name, seconds))

            def tick(self):
                calls.append(("tick",))
                return self.frames >= 1

            def sample_resources(self, *, force=False):
                calls.append(("resources", force))

            def record_cleanup(self, seconds, **result):
                calls.append(("cleanup", seconds, result))

            def finalize(self, **result):
                calls.append(("finalize", result))

        process = self.FakeProcess(video=True)
        args = self.args(
            diagnostics_json="new-report.json",
            diagnostics_warmup=0.0,
            diagnostics_duration=0.01,
        )
        globals_ = CORE["run"].__globals__
        module = SimpleNamespace(PlaybackDiagnostics=RecordingDiagnostics)
        replacements = {
            "IS_WINDOWS": True,
            "probe": lambda *_args: self.info(),
            "spawn_video": lambda *_args: process,
            "find_ffplay": lambda: None,
        }
        with mock.patch.dict(sys.modules, {"yt_ascii_diagnostics": module}), \
                mock.patch.dict(globals_, replacements), \
                mock.patch.object(globals_["os"], "isatty", return_value=False), \
                mock.patch.object(
                    globals_["time"], "monotonic", return_value=0.0
                ), \
                mock.patch.object(globals_["time"], "sleep"), \
                mock.patch.object(
                    globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)
                ), \
                mock.patch.object(globals_["sys"], "stdout", io.StringIO()), \
                mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
            CORE["run"](args)

        self.assertEqual(len(instances), 1)
        self.assertIn(("child_start", "video", process.pid), calls)
        self.assertIn(("child_stop", process.pid), calls)
        frames = [item for item in calls if item[0] == "frame"]
        self.assertEqual(len(frames), 1)
        self.assertGreater(frames[0][1]["output_bytes"], 0)
        self.assertEqual(frames[0][1]["dropped"], 0)
        source = next(item[1] for item in calls if item[0] == "source")
        self.assertEqual(
            (source["width"], source["height"], source["duration_seconds"]),
            (16, 9, 0.0),
        )
        self.assertIsNone(source["live"])
        self.assertIsNone(source["fps"])
        self.assertEqual(calls[-1], (
            "finalize", {"exit_reason": "duration"}
        ))
        cleanup_index = next(
            index for index, item in enumerate(calls) if item[0] == "cleanup"
        )
        self.assertTrue(calls[cleanup_index][2]["terminal_restored"])
        self.assertTrue(calls[cleanup_index][2]["children_reaped"])
        self.assertLess(cleanup_index, len(calls) - 1)

    def test_one_frame_cells_playback_uses_graphical_backend_end_to_end(self):
        process = self.FakeProcess(video=True)
        spawned = []
        key_reads = 0

        def spawn_video(*args, **_kwargs):
            spawned.append(args)
            return process

        def read_keys(_fd):
            nonlocal key_reads
            key_reads += 1
            return [] if key_reads == 1 else ["q"]

        args = self.args(
            no_color=False,
            render="cells",
            effect="wave",
            width=2,
            height=2,
        )
        output = io.StringIO()
        globals_ = CORE["run"].__globals__
        replacements = {
            "_CAN_SUSPEND": False,
            "IS_WINDOWS": True,
            "probe": lambda *_args: self.info(),
            "spawn_video": spawn_video,
            "find_ffplay": lambda: None,
            "read_keys": read_keys,
        }
        with mock.patch.dict(globals_, replacements), \
                mock.patch.object(globals_["os"], "isatty", return_value=True), \
                mock.patch.object(
                    globals_["time"], "monotonic", return_value=0.0
                ), \
                mock.patch.object(globals_["time"], "sleep"), \
                mock.patch.object(
                    globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)
                ), \
                mock.patch.object(globals_["sys"], "stdout", output), \
                mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
            CORE["run"](args)

        self.assertTrue(process.exited)
        self.assertEqual(len(spawned), 1)
        # Cells decode at one source row per terminal row, unlike half-block.
        self.assertEqual(spawned[0][2:4], (2, 2))
        payload = output.getvalue()
        self.assertIn("\x1b[48;2;", payload)
        self.assertNotIn("▀", payload)
        self.assertIn("render:cells", payload)

    def test_style_cycle_redraws_paused_frame_without_restarting_reveal(self):
        import yt_ascii_effects
        import yt_ascii_renderer
        import yt_ascii_styles
        from yt_ascii_frames import EffectFrame

        base_renderer = yt_ascii_renderer.AnsiRenderer
        globals_ = CORE["run"].__globals__

        def exercise(key_batch):
            apply_calls = []
            reveal_fractions = []
            reveal_resets = []
            effect_resets = []
            key_call = 0

            class RecordingStyleProcessor:
                def __init__(self, name):
                    self.name = name

                def reset(self):
                    pass

                def cycle(self):
                    self.name = "posterize"
                    return self.name

                def apply(self, frame):
                    apply_calls.append(self.name)
                    return frame

            class RecordingRenderer(base_renderer):
                def reset_reveal(self):
                    reveal_resets.append(True)
                    return super().reset_reveal()

                def render_scatter(self, frame, fraction):
                    reveal_fractions.append(fraction)
                    return super().render_scatter(frame, fraction)

            class RecordingEffectProcessor:
                def __init__(
                    self, name, *, glyph_mode, speed, seed, effect_text
                ):
                    self.name = name

                def reset(self, reason):
                    effect_resets.append(reason)

                def cycle(self):
                    raise AssertionError("unexpected effect cycle")

                def apply(self, frame, _context):
                    return EffectFrame(frame)

            def read_keys(_fd):
                nonlocal key_call
                key_call += 1
                if key_call == 1:
                    return []
                if key_call == 2:
                    return key_batch
                return ["q"]

            process = self.FakeProcess(video=True)
            args = self.args(scatter=True)
            replacements = {
                "_CAN_SUSPEND": False,
                "IS_WINDOWS": True,
                "probe": lambda *_args: self.info(),
                "spawn_video": lambda *_args: process,
                "find_ffplay": lambda: None,
                "read_keys": read_keys,
            }
            with mock.patch.dict(globals_, replacements), \
                    mock.patch.object(globals_["os"], "isatty", return_value=True), \
                    mock.patch.object(globals_["time"], "monotonic", return_value=0.0), \
                    mock.patch.object(globals_["time"], "sleep"), \
                    mock.patch.object(yt_ascii_renderer, "AnsiRenderer", RecordingRenderer), \
                    mock.patch.object(yt_ascii_styles, "StyleProcessor", RecordingStyleProcessor), \
                    mock.patch.object(
                        yt_ascii_effects,
                        "EffectProcessor",
                        RecordingEffectProcessor,
                    ), \
                    mock.patch.object(globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)), \
                    mock.patch.object(globals_["sys"], "stdout", io.StringIO()), \
                    mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
                CORE["run"](args)
            return (
                args,
                apply_calls,
                reveal_fractions,
                reveal_resets,
                effect_resets,
            )

        for key_batch in (["space", "s"], ["s", "space"]):
            with self.subTest(key_batch=key_batch):
                (
                    args,
                    apply_calls,
                    reveal_fractions,
                    reveal_resets,
                    effect_resets,
                ) = exercise(key_batch)
                self.assertEqual(args.style, "posterize")
                self.assertEqual(apply_calls, ["classic", "posterize"])
                self.assertEqual(len(reveal_fractions), 2)
                self.assertEqual(reveal_fractions[0], reveal_fractions[1])
                self.assertEqual(len(reveal_resets), 1)
                self.assertEqual(effect_resets, ["source", "style"])

    def test_palette_cycle_redraws_pause_without_resetting_pipeline(self):
        import yt_ascii_effects
        import yt_ascii_renderer
        from yt_ascii_frames import EffectFrame

        base_renderer = yt_ascii_renderer.AnsiRenderer
        globals_ = CORE["run"].__globals__

        def exercise(key_batch, custom_chars=None):
            contexts = []
            effect_resets = []
            palette_updates = []
            reveal_fractions = []
            reveal_resets = []
            spawned = []
            key_call = 0

            class RecordingRenderer(base_renderer):
                def set_palette(self, chars):
                    palette_updates.append(chars)
                    return super().set_palette(chars)

                def reset_reveal(self):
                    reveal_resets.append(True)
                    return super().reset_reveal()

                def render_scatter(self, frame, fraction):
                    reveal_fractions.append(fraction)
                    return super().render_scatter(frame, fraction)

            class RecordingEffectProcessor:
                def __init__(
                    self, name, *, glyph_mode, speed, seed, effect_text
                ):
                    self.name = name

                def reset(self, reason):
                    effect_resets.append(reason)

                def apply(self, frame, context):
                    contexts.append(context)
                    return EffectFrame(frame)

            def spawn_video(*_args):
                process = self.FakeProcess(video=True)
                spawned.append(process)
                return process

            def read_keys(_fd):
                nonlocal key_call
                key_call += 1
                if key_call == 1:
                    return []
                if key_call == 2:
                    return key_batch
                return ["q"]

            args = self.args(
                scatter=True, palette="simple", chars=custom_chars
            )
            replacements = {
                "_CAN_SUSPEND": False,
                "IS_WINDOWS": True,
                "probe": lambda *_args: self.info(),
                "spawn_video": spawn_video,
                "find_ffplay": lambda: None,
                "read_keys": read_keys,
            }
            with mock.patch.dict(globals_, replacements), \
                    mock.patch.object(globals_["os"], "isatty", return_value=True), \
                    mock.patch.object(globals_["time"], "monotonic", return_value=0.0), \
                    mock.patch.object(globals_["time"], "sleep"), \
                    mock.patch.object(
                        yt_ascii_renderer, "AnsiRenderer", RecordingRenderer
                    ), \
                    mock.patch.object(
                        yt_ascii_effects,
                        "EffectProcessor",
                        RecordingEffectProcessor,
                    ), \
                    mock.patch.object(
                        globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)
                    ), \
                    mock.patch.object(globals_["sys"], "stdout", io.StringIO()), \
                    mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
                CORE["run"](args)
            return {
                "args": args,
                "contexts": contexts,
                "effect_resets": effect_resets,
                "palette_updates": palette_updates,
                "reveal_fractions": reveal_fractions,
                "reveal_resets": reveal_resets,
                "spawned": spawned,
            }

        cases = (
            (["space", "p"], None, "dense"),
            (["p", "space"], None, "dense"),
            (["space", "p"], " .@", "simple"),
        )
        for key_batch, custom_chars, expected_name in cases:
            with self.subTest(
                key_batch=key_batch, custom_chars=custom_chars
            ):
                result = exercise(key_batch, custom_chars)
                self.assertEqual(result["args"].palette, expected_name)
                self.assertIsNone(result["args"].chars)
                self.assertEqual(
                    result["palette_updates"],
                    [CORE["PALETTES"][expected_name]],
                )
                self.assertEqual(result["effect_resets"], ["source"])
                self.assertEqual(len(result["spawned"]), 1)
                self.assertEqual(len(result["reveal_resets"]), 1)
                self.assertEqual(len(result["reveal_fractions"]), 2)
                self.assertEqual(
                    result["reveal_fractions"][0],
                    result["reveal_fractions"][1],
                )
                self.assertEqual(len(result["contexts"]), 2)
                self.assertTrue(result["contexts"][0].advance_state)
                self.assertFalse(result["contexts"][1].advance_state)
                self.assertEqual(
                    result["contexts"][0].video_time,
                    result["contexts"][1].video_time,
                )

    def test_palette_hotkey_respects_effective_glyph_ownership(self):
        import yt_ascii_renderer

        base_renderer = yt_ascii_renderer.AnsiRenderer
        globals_ = CORE["run"].__globals__

        def exercise(**overrides):
            palette_updates = []
            key_call = 0

            class RecordingRenderer(base_renderer):
                def set_palette(self, chars):
                    palette_updates.append(chars)
                    return super().set_palette(chars)

            def read_keys(_fd):
                nonlocal key_call
                key_call += 1
                if key_call == 1:
                    return []
                if key_call == 2:
                    return ["p"]
                return ["q"]

            args = self.args(palette="simple", **overrides)
            process = self.FakeProcess(video=True)
            replacements = {
                "IS_WINDOWS": True,
                "probe": lambda *_args: self.info(),
                "spawn_video": lambda *_args: process,
                "find_ffplay": lambda: None,
                "read_keys": read_keys,
            }
            with mock.patch.dict(globals_, replacements), \
                    mock.patch.object(globals_["os"], "isatty", return_value=True), \
                    mock.patch.object(globals_["time"], "monotonic", return_value=0.0), \
                    mock.patch.object(globals_["time"], "sleep"), \
                    mock.patch.object(
                        yt_ascii_renderer, "AnsiRenderer", RecordingRenderer
                    ), \
                    mock.patch.object(
                        globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)
                    ), \
                    mock.patch.object(globals_["sys"], "stdout", io.StringIO()), \
                    mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
                CORE["run"](args)
            return args, palette_updates

        cases = (
            ({"render": "chars", "no_color": False}, True),
            ({"render": "cells", "no_color": True}, True),
            ({"render": "half-block", "no_color": True}, True),
            ({"render": "cells", "no_color": False}, False),
            ({"render": "half-block", "no_color": False}, False),
            ({"render": "chars", "effect": "digital-rain"}, False),
            ({"render": "chars", "effect": "terminal-hud"}, False),
        )
        for overrides, supported in cases:
            with self.subTest(overrides=overrides):
                args, updates = exercise(**overrides)
                if supported:
                    self.assertEqual(updates, [CORE["PALETTES"]["dense"]])
                    self.assertEqual(args.palette, "dense")
                else:
                    self.assertEqual(updates, [])
                    self.assertEqual(args.palette, "simple")

    def test_effect_cycle_redraws_pause_without_advancing_effect_state(self):
        import yt_ascii_effects
        from yt_ascii_frames import EffectFrame

        contexts = []
        resets = []
        configs = []
        key_call = 0

        class RecordingEffectProcessor:
            def __init__(self, name, *, glyph_mode, speed, seed, effect_text):
                self.name = name
                self.config = (glyph_mode, speed, seed, effect_text)
                configs.append(self.config)

            def reset(self, reason):
                resets.append(reason)

            def cycle(self, render_mode="chars"):
                self.name = "pixelate"
                resets.append("select")
                return self.name

            def apply(self, frame, context):
                contexts.append(context)
                return EffectFrame(frame)

        def read_keys(_fd):
            nonlocal key_call
            key_call += 1
            if key_call == 1:
                return []
            if key_call == 2:
                return ["space", "e"]
            return ["q"]

        process = self.FakeProcess(video=True)
        args = self.args(
            effect="none", effect_glyphs="unicode", effect_speed=1.5,
            effect_seed=9, effect_text="YT λ",
        )
        globals_ = CORE["run"].__globals__
        replacements = {
            "_CAN_SUSPEND": False,
            "IS_WINDOWS": True,
            "probe": lambda *_args: self.info(),
            "spawn_video": lambda *_args: process,
            "find_ffplay": lambda: None,
            "read_keys": read_keys,
        }
        with mock.patch.dict(globals_, replacements), \
                mock.patch.object(globals_["os"], "isatty", return_value=True), \
                mock.patch.object(globals_["time"], "monotonic", return_value=0.0), \
                mock.patch.object(globals_["time"], "sleep"), \
                mock.patch.object(
                    yt_ascii_effects, "EffectProcessor", RecordingEffectProcessor
                ), \
                mock.patch.object(
                    globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)
                ), \
                mock.patch.object(globals_["sys"], "stdout", io.StringIO()), \
                mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
            CORE["run"](args)

        self.assertEqual(args.effect, "pixelate")
        self.assertEqual(configs, [("unicode", 1.5, 9, "YT λ")])
        self.assertEqual(resets, ["source", "select"])
        self.assertEqual(len(contexts), 2)
        self.assertTrue(contexts[0].advance_state)
        self.assertFalse(contexts[1].advance_state)
        self.assertEqual(contexts[0].video_time, contexts[1].video_time)

    def test_seek_resets_effect_history_and_presentation_sequence(self):
        import yt_ascii_effects
        from yt_ascii_frames import EffectFrame

        resets = []
        contexts = []
        spawned = []
        key_call = 0

        class RecordingEffectProcessor:
            def __init__(self, name, *, glyph_mode, speed, seed, effect_text):
                self.name = name

            def reset(self, reason):
                resets.append(reason)

            def cycle(self):
                raise AssertionError("unexpected effect cycle")

            def apply(self, frame, context):
                contexts.append(context)
                return EffectFrame(frame)

        def spawn_video(*_args):
            process = self.FakeProcess(video=True)
            spawned.append(process)
            return process

        def read_keys(_fd):
            nonlocal key_call
            key_call += 1
            if key_call == 1:
                return []
            if key_call == 2:
                return ["right"]
            return ["q"]

        info = self.info()
        info["duration"] = 60.0
        globals_ = CORE["run"].__globals__
        replacements = {
            "_CAN_SUSPEND": False,
            "IS_WINDOWS": True,
            "probe": lambda *_args: dict(info),
            "spawn_video": spawn_video,
            "find_ffplay": lambda: None,
            "read_keys": read_keys,
        }
        with mock.patch.dict(globals_, replacements), \
                mock.patch.object(globals_["os"], "isatty", return_value=True), \
                mock.patch.object(globals_["time"], "monotonic", return_value=0.0), \
                mock.patch.object(globals_["time"], "sleep"), \
                mock.patch.object(
                    yt_ascii_effects, "EffectProcessor", RecordingEffectProcessor
                ), \
                mock.patch.object(
                    globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)
                ), \
                mock.patch.object(globals_["sys"], "stdout", io.StringIO()), \
                mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
            CORE["run"](self.args())

        self.assertEqual(len(spawned), 2)
        self.assertEqual(resets, ["source", "seek"])
        self.assertEqual(
            [(item.frame_sequence, item.video_time) for item in contexts],
            [(0, 0.0), (0, 5.0)],
        )

    def test_same_source_reconnect_preserves_effect_history_and_sequence(self):
        import yt_ascii_effects
        from yt_ascii_frames import EffectFrame

        resets = []
        contexts = []
        spawned = []
        key_call = 0

        class OneFrameThenEof:
            def __init__(self):
                self.remaining = 1

            def readinto(self, target):
                if not self.remaining:
                    return 0
                self.remaining -= 1
                target[:] = b"\x00" * len(target)
                return len(target)

        class RecordingEffectProcessor:
            def __init__(self, name, *, glyph_mode, speed, seed, effect_text):
                self.name = name

            def reset(self, reason):
                resets.append(reason)

            def cycle(self):
                raise AssertionError("unexpected effect cycle")

            def apply(self, frame, context):
                contexts.append(context)
                return EffectFrame(frame)

        def spawn_video(*_args):
            process = self.FakeProcess(video=True)
            if not spawned:
                process.stdout = OneFrameThenEof()
            spawned.append(process)
            return process

        def read_keys(_fd):
            nonlocal key_call
            key_call += 1
            return ["q"] if key_call == 4 else []

        info = self.info()
        info["duration"] = 60.0
        globals_ = CORE["run"].__globals__
        replacements = {
            "_CAN_SUSPEND": False,
            "IS_WINDOWS": True,
            "probe": lambda *_args: dict(info),
            "spawn_video": spawn_video,
            "find_ffplay": lambda: None,
            "read_keys": read_keys,
        }
        with mock.patch.dict(globals_, replacements), \
                mock.patch.object(globals_["os"], "isatty", return_value=True), \
                mock.patch.object(globals_["time"], "monotonic", return_value=0.0), \
                mock.patch.object(globals_["time"], "sleep"), \
                mock.patch.object(
                    yt_ascii_effects, "EffectProcessor", RecordingEffectProcessor
                ), \
                mock.patch.object(
                    globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)
                ), \
                mock.patch.object(globals_["sys"], "stdout", io.StringIO()), \
                mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
            CORE["run"](self.args())

        self.assertEqual(len(spawned), 2)
        self.assertEqual(resets, ["source"])
        self.assertEqual(
            [(item.frame_sequence, item.video_time) for item in contexts],
            [(0, 0.0), (1, 0.1)],
        )

    def test_new_media_preserves_effect_selection_but_resets_its_state(self):
        import yt_ascii_effects

        instances = []

        class RecordingEffectProcessor:
            def __init__(self, name, *, glyph_mode, speed, seed, effect_text):
                self.initial_name = name
                self.name = name
                self.resets = []
                instances.append(self)

            def reset(self, reason):
                self.resets.append(reason)

            def cycle(self, render_mode="chars"):
                self.name = "pixelate"
                return self.name

            def apply(self, _frame, _context):
                raise AssertionError("quit batches should not render")

        args = self.args()
        key_batches = iter((("e", "q"), ("q",)))
        globals_ = CORE["run"].__globals__
        replacements = {
            "_CAN_SUSPEND": False,
            "IS_WINDOWS": True,
            "probe": lambda *_args: self.info(),
            "spawn_video": lambda *_args: self.FakeProcess(video=True),
            "find_ffplay": lambda: None,
        }
        with mock.patch.dict(globals_, replacements), \
                mock.patch.object(globals_["os"], "isatty", return_value=True), \
                mock.patch.object(globals_["time"], "monotonic", return_value=0.0), \
                mock.patch.object(globals_["time"], "sleep"), \
                mock.patch.object(
                    yt_ascii_effects, "EffectProcessor", RecordingEffectProcessor
                ), \
                mock.patch.object(
                    globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)
                ), \
                mock.patch.object(globals_["sys"], "stdout", io.StringIO()), \
                mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
            with mock.patch.dict(globals_, {
                "read_keys": lambda _fd: list(next(key_batches)),
            }):
                CORE["run"](args)
                CORE["run"](args)

        self.assertEqual(args.effect, "pixelate")
        self.assertEqual(
            [item.initial_name for item in instances], ["none", "pixelate"]
        )
        self.assertEqual([item.resets for item in instances], [["source"], ["source"]])

    def test_style_stage_is_timestamp_free(self):
        import yt_ascii_styles

        apply_calls = []
        key_call = 0

        class RecordingStyleProcessor:
            def __init__(self, name):
                self.name = name

            def reset(self):
                pass

            def cycle(self):
                raise AssertionError("unexpected style cycle")

            def apply(self, frame):
                apply_calls.append(True)
                return frame

        def read_keys(_fd):
            nonlocal key_call
            key_call += 1
            return ["q"] if key_call == 3 else []

        process = self.FakeProcess(video=True)
        args = self.args(style="riso", fps=10)
        globals_ = CORE["run"].__globals__
        replacements = {
            "_CAN_SUSPEND": False,
            "IS_WINDOWS": True,
            "probe": lambda *_args: self.info(),
            "spawn_video": lambda *_args: process,
            "find_ffplay": lambda: None,
            "read_keys": read_keys,
        }
        with mock.patch.dict(globals_, replacements), \
                mock.patch.object(globals_["os"], "isatty", return_value=True), \
                mock.patch.object(globals_["time"], "monotonic", return_value=10.0), \
                mock.patch.object(globals_["time"], "sleep"), \
                mock.patch.object(yt_ascii_styles, "StyleProcessor", RecordingStyleProcessor), \
                mock.patch.object(globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)), \
                mock.patch.object(globals_["sys"], "stdout", io.StringIO()), \
                mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
            CORE["run"](args)

        self.assertEqual(apply_calls, [True, True])

    def test_pause_resize_resume_spawns_exactly_one_replacement(self):
        spawned = []
        reveal_fractions = []
        key_call = 0
        winch = None

        import yt_ascii_effects
        import yt_ascii_renderer
        import yt_ascii_styles
        from yt_ascii_frames import EffectFrame

        base_renderer = yt_ascii_renderer.AnsiRenderer
        base_style_processor = yt_ascii_styles.StyleProcessor
        style_resets = []
        effect_resets = []
        effect_contexts = []

        class RecordingStyleProcessor(base_style_processor):
            def reset(self):
                style_resets.append(self.name)
                return super().reset()

        class RecordingRenderer(base_renderer):
            def render_scatter(self, frame, fraction):
                reveal_fractions.append(fraction)
                return super().render_scatter(frame, fraction)

        class RecordingEffectProcessor:
            def __init__(self, name, *, glyph_mode, speed, seed, effect_text):
                self.name = name

            def reset(self, reason):
                effect_resets.append(reason)

            def cycle(self):
                raise AssertionError("unexpected effect cycle")

            def apply(self, frame, context):
                effect_contexts.append(context)
                return EffectFrame(frame)

        class FakeSignal:
            SIGWINCH = object()

            @staticmethod
            def signal(_kind, handler):
                nonlocal winch
                winch = handler

        def spawn_video(*_args, **_kwargs):
            process = self.FakeProcess(video=True)
            spawned.append(process)
            return process

        def read_keys(_fd):
            nonlocal key_call
            key_call += 1
            if key_call == 1:
                return []                         # show one frame first
            if key_call == 2:
                winch()                           # resize in the paused iteration
                return ["space"]
            if key_call == 3:
                return ["space"]                 # resume
            return ["q"]

        sizes = iter(((80, 24), (100, 30)))
        last_size = [os.terminal_size((100, 30))]

        def terminal_size(_fallback):
            try:
                last_size[0] = os.terminal_size(next(sizes))
            except StopIteration:
                pass
            return last_size[0]

        clock = [0.0]

        def monotonic():
            clock[0] += 0.01
            return clock[0]

        def sleep(duration):
            # Simulate a long pause without slowing the test. Pacing sleeps are
            # left alone; only the paused-loop 50ms sleep advances the clock.
            if duration == 0.05:
                clock[0] += 10.0

        args = SimpleNamespace(
            url="fixture",
            no_color=True,
            no_audio=True,
            eight_bit=False,
            pixels=False,
            scatter=True,
            rain=False,
            scatter_secs=4.0,
            rain_secs=7.5,
            rain_chars="matrix",
            fps=30,
            width=2,
            height=2,
            max_res=480,
            palette="simple",
            chars=None,
            style="classic",
            effect="none",
            effect_glyphs="ascii",
            effect_speed=1.0,
            effect_seed=0,
            effect_text="YTASCII",
        )
        info = {
            "title": "fixture",
            "width": 16,
            "height": 9,
            "duration": 0.0,
            "video": "fixture",
            "audio": None,
        }
        output = io.StringIO()
        globals_ = CORE["run"].__globals__
        replacements = {
            "_CAN_SUSPEND": False,
            "IS_WINDOWS": True,
            "signal": FakeSignal,
            "probe": lambda *_args: dict(info),
            "spawn_video": spawn_video,
            "find_ffplay": lambda: None,
            "read_keys": read_keys,
        }
        with mock.patch.dict(globals_, replacements), \
                mock.patch.object(globals_["os"], "isatty", return_value=True), \
                mock.patch.object(globals_["shutil"], "get_terminal_size", terminal_size), \
                mock.patch.object(globals_["time"], "monotonic", monotonic), \
                mock.patch.object(globals_["time"], "sleep", sleep), \
                mock.patch.object(yt_ascii_renderer, "AnsiRenderer", RecordingRenderer), \
                mock.patch.object(yt_ascii_styles, "StyleProcessor", RecordingStyleProcessor), \
                mock.patch.object(
                    yt_ascii_effects,
                    "EffectProcessor",
                    RecordingEffectProcessor,
                ), \
                mock.patch.object(globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)), \
                mock.patch.object(globals_["sys"], "stdout", output), \
                mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
            CORE["run"](args)

        # Initial decoder + one decoder on resume. A paused resize must not
        # create a third process that is then overwritten and leaked.
        self.assertEqual(len(spawned), 2)
        self.assertTrue(all(process.exited for process in spawned))
        self.assertEqual(style_resets, ["classic", "classic"])
        self.assertEqual(effect_resets, ["source", "resize"])
        self.assertEqual(
            [context.frame_sequence for context in effect_contexts], [0, 0]
        )
        self.assertGreaterEqual(len(reveal_fractions), 2)
        self.assertLess(max(reveal_fractions), 0.25)


if __name__ == "__main__":
    unittest.main()

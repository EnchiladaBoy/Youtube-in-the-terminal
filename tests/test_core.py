import argparse
import io
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
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
        self.assertIn("--style", output.getvalue())
        self.assertIn("--effect", output.getvalue())
        self.assertIn("--check-update", output.getvalue())
        self.assertIn("--update", output.getvalue())
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

    def test_interactive_style_selection_persists_to_the_next_video(self):
        args = SimpleNamespace(
            url=None, style="classic", effect="none", self_test=False
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
            seen.append((current.url, current.style, current.effect))
            if current.url == "first":
                current.style = "bayer"
                current.effect = "geometry"

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
                ("first", "classic", "none"),
                ("second", "bayer", "geometry"),
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

    def test_style_effect_and_palette_cli_contract(self):
        self.assertEqual(CORE["PALETTES"]["binary"], " 01")
        self.assertEqual(CORE["PALETTES"]["numbers"], " 123456789")
        self.assertEqual(CORE["PALETTES"]["symbols"], " .,:;!|?*#@")
        self.assertEqual(CORE["PALETTES"]["matrix"], " 01:=+*<>|/")

        globals_ = CORE["parse_args"].__globals__
        with mock.patch.object(globals_["sys"], "argv", ["yt-ascii"]):
            args = CORE["parse_args"]()
            self.assertEqual(args.style, "classic")
            self.assertEqual(args.effect, "none")
            self.assertEqual(args.effect_glyphs, "ascii")
            self.assertEqual(args.effect_speed, 1.0)
            self.assertEqual(args.effect_seed, 0)
            self.assertFalse(args.update)
            self.assertFalse(args.check_update)
            self.assertFalse(args.no_update_check)
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
                "yt-ascii", "--effect", "voronoi",
                "--effect-glyphs", "unicode",
                "--effect-speed", "1.5", "--effect-seed", "-9",
            ],
        ):
            args = CORE["parse_args"]()
        self.assertEqual(
            (args.effect, args.effect_glyphs, args.effect_speed, args.effect_seed),
            ("voronoi", "unicode", 1.5, -9),
        )

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
                    effect="geometry",
                )
                visible = status.removeprefix("\x1b[0m")
                self.assertLessEqual(len(visible), width)
                self.assertIn("style:riso", visible)
                self.assertIn("effect:geometry", visible)
                self.assertIn("s:style", visible)
                self.assertIn("e:effect", visible)

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
            effect="geometry",
            pixel_fallback=True,
        )
        self.assertIn("effect:geometry pixels→chars", status)

    def test_keyboard_backends_accept_only_lowercase_style_and_effect_keys(self):
        globals_ = CORE["_read_keys_windows"].__globals__

        class FakeMsvcrt:
            def __init__(self):
                self.chars = iter(("s", "S", "e", "E", "1"))
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
            self.assertEqual(CORE["_read_keys_windows"](), ["s", "e", "1"])

        readiness = iter((([0], [], []), ([], [], [])))
        fake_select = SimpleNamespace(select=lambda *_args: next(readiness))
        posix_globals = CORE["_read_keys_posix"].__globals__
        with mock.patch.dict(posix_globals, {"select": fake_select}), \
                mock.patch.object(
                    posix_globals["os"], "read", return_value=b"sSeE1"
                ):
            self.assertEqual(CORE["_read_keys_posix"](0), ["s", "e", "1"])


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
                    self.name = "bayer"
                    return self.name

                def apply(self, frame, time_seconds=0.0):
                    apply_calls.append((self.name, time_seconds))
                    return frame

            class RecordingRenderer(base_renderer):
                def reset_reveal(self):
                    reveal_resets.append(True)
                    return super().reset_reveal()

                def render_scatter(self, frame, fraction):
                    reveal_fractions.append(fraction)
                    return super().render_scatter(frame, fraction)

            class RecordingEffectProcessor:
                def __init__(self, name, *, glyph_mode, speed, seed):
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
                self.assertEqual(args.style, "bayer")
                self.assertEqual(
                    apply_calls, [("classic", 0.0), ("bayer", 0.0)]
                )
                self.assertEqual(len(reveal_fractions), 2)
                self.assertEqual(reveal_fractions[0], reveal_fractions[1])
                self.assertEqual(len(reveal_resets), 1)
                self.assertEqual(effect_resets, ["source", "style"])

    def test_effect_cycle_redraws_pause_without_advancing_effect_state(self):
        import yt_ascii_effects
        from yt_ascii_frames import EffectFrame

        contexts = []
        resets = []
        key_call = 0

        class RecordingEffectProcessor:
            def __init__(self, name, *, glyph_mode, speed, seed):
                self.name = name
                self.config = (glyph_mode, speed, seed)

            def reset(self, reason):
                resets.append(reason)

            def cycle(self):
                self.name = "geometry"
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
            effect_seed=9,
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

        self.assertEqual(args.effect, "geometry")
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
            def __init__(self, name, *, glyph_mode, speed, seed):
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
            def __init__(self, name, *, glyph_mode, speed, seed):
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
            def __init__(self, name, *, glyph_mode, speed, seed):
                self.initial_name = name
                self.name = name
                self.resets = []
                instances.append(self)

            def reset(self, reason):
                self.resets.append(reason)

            def cycle(self):
                self.name = "geometry"
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

        self.assertEqual(args.effect, "geometry")
        self.assertEqual(
            [item.initial_name for item in instances], ["none", "geometry"]
        )
        self.assertEqual([item.resets for item in instances], [["source"], ["source"]])

    def test_style_receives_decoded_frame_timestamps(self):
        import yt_ascii_styles

        apply_times = []
        key_call = 0

        class RecordingStyleProcessor:
            def __init__(self, name):
                self.name = name

            def reset(self):
                pass

            def cycle(self):
                raise AssertionError("unexpected style cycle")

            def apply(self, frame, time_seconds=0.0):
                apply_times.append(time_seconds)
                return frame

        def read_keys(_fd):
            nonlocal key_call
            key_call += 1
            return ["q"] if key_call == 3 else []

        process = self.FakeProcess(video=True)
        args = self.args(style="glitch", fps=10)
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

        self.assertEqual(apply_times, [0.0, 0.1])

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
            def __init__(self, name, *, glyph_mode, speed, seed):
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

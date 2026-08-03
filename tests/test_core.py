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
    def test_help_is_dependency_free_and_lists_styles(self):
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
        self.assertEqual(output.getvalue(), "yt-ascii 0.4.0 (source)\n")
        dependency_check.assert_not_called()

    def test_interactive_style_selection_persists_to_the_next_video(self):
        args = SimpleNamespace(url=None, style="classic", self_test=False)
        seen = []
        urls = iter(("first", "second"))
        globals_ = CORE["main"].__globals__

        def prompt():
            try:
                return next(urls)
            except StopIteration as error:
                raise SystemExit(0) from error

        def run(current):
            seen.append((current.url, current.style))
            if current.url == "first":
                current.style = "bayer"

        with mock.patch.dict(globals_, {
            "parse_args": lambda: args,
            "check_deps": lambda: None,
            "prompt_for_url": prompt,
            "run": run,
        }):
            with self.assertRaises(SystemExit) as raised:
                CORE["main"]()
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(seen, [("first", "classic"), ("second", "bayer")])

    def test_version_reports_valid_installer_refs(self):
        for install_ref in ("v0.3.0", "v12.34.567", "edge"):
            with self.subTest(install_ref=install_ref), mock.patch.dict(
                os.environ, {"YTASCII_INSTALL_REF": install_ref}
            ):
                self.assertEqual(
                    CORE["version_text"](),
                    f"yt-ascii 0.4.0 ({install_ref})",
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
                    "yt-ascii 0.4.0 (source)",
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

    def test_style_and_palette_cli_contract(self):
        self.assertEqual(CORE["PALETTES"]["binary"], " 01")
        self.assertEqual(CORE["PALETTES"]["numbers"], " 123456789")
        self.assertEqual(CORE["PALETTES"]["symbols"], " .,:;!|?*#@")
        self.assertEqual(CORE["PALETTES"]["matrix"], " 01:=+*<>|/")

        globals_ = CORE["parse_args"].__globals__
        with mock.patch.object(globals_["sys"], "argv", ["yt-ascii"]):
            self.assertEqual(CORE["parse_args"]().style, "classic")
        for style in CORE["STYLE_NAMES"]:
            with self.subTest(style=style), mock.patch.object(
                globals_["sys"], "argv", ["yt-ascii", "--style", style]
            ):
                self.assertEqual(CORE["parse_args"]().style, style)

    def test_status_shows_style_and_style_control(self):
        for width in (120, 80):
            with self.subTest(width=width):
                status = CORE["build_status"](
                    30,
                    12.0,
                    {"duration": 60.0, "title": "fixture"},
                    width,
                    paused=False,
                    style="riso",
                )
                visible = status.removeprefix("\x1b[0m")
                self.assertLessEqual(len(visible), width)
                self.assertIn("style:riso", visible)
                self.assertIn("s:style", visible)

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

    def test_keyboard_backends_accept_only_lowercase_style_key(self):
        globals_ = CORE["_read_keys_windows"].__globals__

        class FakeMsvcrt:
            def __init__(self):
                self.chars = iter(("s", "S", "1"))
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
            self.assertEqual(CORE["_read_keys_windows"](), ["s", "1"])

        readiness = iter((([0], [], []), ([], [], [])))
        fake_select = SimpleNamespace(select=lambda *_args: next(readiness))
        posix_globals = CORE["_read_keys_posix"].__globals__
        with mock.patch.dict(posix_globals, {"select": fake_select}), \
                mock.patch.object(posix_globals["os"], "read", return_value=b"sS1"):
            self.assertEqual(CORE["_read_keys_posix"](0), ["s", "1"])


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
        import yt_ascii_renderer
        import yt_ascii_styles

        base_renderer = yt_ascii_renderer.AnsiRenderer
        globals_ = CORE["run"].__globals__

        def exercise(key_batch):
            apply_calls = []
            reveal_fractions = []
            reveal_resets = []
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
                    mock.patch.object(globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)), \
                    mock.patch.object(globals_["sys"], "stdout", io.StringIO()), \
                    mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
                CORE["run"](args)
            return args, apply_calls, reveal_fractions, reveal_resets

        for key_batch in (["space", "s"], ["s", "space"]):
            with self.subTest(key_batch=key_batch):
                args, apply_calls, reveal_fractions, reveal_resets = exercise(key_batch)
                self.assertEqual(args.style, "bayer")
                self.assertEqual(
                    apply_calls, [("classic", 0.0), ("bayer", 0.0)]
                )
                self.assertEqual(len(reveal_fractions), 2)
                self.assertEqual(reveal_fractions[0], reveal_fractions[1])
                self.assertEqual(len(reveal_resets), 1)

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

        import yt_ascii_renderer
        import yt_ascii_styles

        base_renderer = yt_ascii_renderer.AnsiRenderer
        base_style_processor = yt_ascii_styles.StyleProcessor
        style_resets = []

        class RecordingStyleProcessor(base_style_processor):
            def reset(self):
                style_resets.append(self.name)
                return super().reset()

        class RecordingRenderer(base_renderer):
            def render_scatter(self, frame, fraction):
                reveal_fractions.append(fraction)
                return super().render_scatter(frame, fraction)

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
                mock.patch.object(globals_["sys"], "stdin", SimpleNamespace(fileno=lambda: 0)), \
                mock.patch.object(globals_["sys"], "stdout", output), \
                mock.patch.object(globals_["sys"], "stderr", io.StringIO()):
            CORE["run"](args)

        # Initial decoder + one decoder on resume. A paused resize must not
        # create a third process that is then overwritten and leaked.
        self.assertEqual(len(spawned), 2)
        self.assertTrue(all(process.exited for process in spawned))
        self.assertEqual(style_resets, ["classic", "classic"])
        self.assertGreaterEqual(len(reveal_fractions), 2)
        self.assertLess(max(reveal_fractions), 0.25)


if __name__ == "__main__":
    unittest.main()

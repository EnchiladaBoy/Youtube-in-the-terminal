import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error

import install
import yt_ascii_update as update


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.read_size = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        self.read_size = size
        return self.payload


class ManagedFixture:
    def __init__(self, *, channel="stable", ref="v0.4.0", edge_build=None):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "managed"
        self.generation = self.root / "versions" / "generation-abc"
        self.app = self.generation / "app"
        self.venv_bin = self.generation / "venv" / (
            "Scripts" if os.name == "nt" else "bin"
        )
        self.bin_dir = self.base / "bin"
        self.app.mkdir(parents=True)
        self.venv_bin.mkdir(parents=True)
        self.bin_dir.mkdir()
        self.module = self.app / "yt_ascii_update.py"
        self.module.write_text("# installed updater\n", encoding="utf-8")
        self.installer = self.app / "install.py"
        self.installer.write_text("# installed installer\n", encoding="utf-8")
        self.python = self.venv_bin / (
            "python.exe" if os.name == "nt" else "python"
        )
        self.python.write_text("python\n", encoding="utf-8")
        if os.name != "nt":
            self.python.chmod(0o755)
        (self.root / update.ROOT_MARKER).write_text(
            update.ROOT_MARKER_CONTENT, encoding="ascii", newline="\n"
        )
        self.ref = ref
        self.write_current()
        self.launcher, content = update._expected_launcher(self.root, self.bin_dir)
        self.launcher.write_text(content, encoding="utf-8", newline="\n")
        if os.name != "nt":
            self.launcher.chmod(0o755)
        self.state = {
            "schema": 1,
            "root": str(self.root),
            "current": str(self.generation),
            "previous": None,
            "ref": ref,
            "launcher": str(self.launcher),
            "bin_dir": str(self.bin_dir),
            "path_files": [],
            "windows_path_added": False,
            "channel": channel,
            "edge_build": edge_build,
        }
        self.write_state()

    def write_current(self, text=None):
        if text is None:
            text = f"{self.generation}\n{self.ref}\n"
        (self.root / update.CURRENT_FILE).write_text(
            text, encoding="utf-8", newline="\n"
        )

    def write_state(self, text=None):
        if text is None:
            text = json.dumps(self.state, sort_keys=True) + "\n"
        (self.root / update.STATE_FILE).write_text(
            text, encoding="utf-8", newline="\n"
        )

    def close(self):
        self.temporary.cleanup()


class MarkerParsingTests(unittest.TestCase):
    def test_stable_tag_is_strict(self):
        for payload in (b"v0.4.0", b"v0.4.0\n", "v12.34.567\r\n"):
            with self.subTest(payload=payload):
                self.assertEqual(
                    update.parse_stable_tag(payload),
                    str(payload, "ascii").strip() if isinstance(payload, bytes) else payload.strip(),
                )
        invalid = (
            b"0.4.0",
            b"v01.4.0",
            b"v0.04.0",
            b"v0.4",
            b"v0.4.0-rc1",
            b" v0.4.0\n",
            b"v0.4.0 \n",
            b"v0.4.0\nextra\n",
            b"v0.4.0\xff",
            b"x" * 129,
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(update.UpdateError):
                    update.parse_stable_tag(payload)

    def test_edge_build_is_canonical_and_positive(self):
        for payload, expected in ((b"1", 1), (b"42\n", 42), ("999\r\n", 999)):
            with self.subTest(payload=payload):
                self.assertEqual(update.parse_edge_build(payload), expected)
        for payload in (
            b"0", b"01", b"+1", b"-1", b" 1", b"1 ", b"1\n2", b"one", b""
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(update.UpdateError):
                    update.parse_edge_build(payload)


class InstallerContractTests(unittest.TestCase):
    def test_updater_and_installer_share_launcher_and_marker_contracts(self):
        root = Path("/tmp/yt ascii-contract")
        bin_dir = Path("/tmp/custom-bin")
        self.assertEqual(update.ROOT_MARKER, install.ROOT_MARKER)
        self.assertEqual(update.ROOT_MARKER_CONTENT, install.ROOT_MARKER_CONTENT)
        self.assertEqual(update.CURRENT_FILE, install.CURRENT_FILE)
        self.assertEqual(update.STATE_FILE, install.STATE_FILE)
        self.assertEqual(update._posix_launcher(root), install.posix_launcher(root))
        self.assertEqual(
            update._windows_launcher(root, bin_dir),
            install.windows_launcher(root, bin_dir),
        )


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ManagedFixture()

    def tearDown(self):
        self.fixture.close()

    def test_discovers_exact_managed_install(self):
        found = update.discover_install(self.fixture.module)
        self.assertEqual(found.root, self.fixture.root)
        self.assertEqual(found.generation, self.fixture.generation)
        self.assertEqual(found.app_dir, self.fixture.app)
        self.assertEqual(found.current_ref, "v0.4.0")
        self.assertEqual(found.channel, "stable")
        self.assertIsNone(found.edge_build)
        self.assertEqual(found.bin_dir, self.fixture.bin_dir)
        self.assertEqual(found.launcher, self.fixture.launcher)
        self.assertEqual(found.python, self.fixture.python)
        self.assertEqual(found.installer, self.fixture.installer)

    def test_discovers_edge_and_legacy_channels(self):
        cases = (
            ("edge", "edge", 7, "edge", 7),
            (None, "edge", None, "edge", None),
            (None, "v1.2.3", None, "pinned", None),
            (None, "source", None, "source", None),
        )
        for channel, ref, edge_build, expected_channel, expected_build in cases:
            with self.subTest(channel=channel, ref=ref):
                fixture = ManagedFixture(
                    channel=channel or "stable", ref=ref, edge_build=edge_build
                )
                try:
                    if channel is None:
                        fixture.state.pop("channel")
                        fixture.state.pop("edge_build")
                        fixture.write_state()
                    found = update.discover_install(fixture.module)
                    self.assertEqual(found.channel, expected_channel)
                    self.assertEqual(found.edge_build, expected_build)
                finally:
                    fixture.close()

    def test_rejects_invalid_channel_metadata(self):
        mutations = (
            lambda state: state.update(channel="nightly"),
            lambda state: state.pop("edge_build"),
            lambda state: state.update(channel="edge", ref="v0.4.0", edge_build=1),
            lambda state: state.update(channel="stable", ref="edge"),
            lambda state: state.update(channel="source", ref="v0.4.0"),
            lambda state: state.update(edge_build=1),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                state = dict(self.fixture.state)
                mutate(state)
                self.fixture.write_state(json.dumps(state) + "\n")
                with self.assertRaises(update.UpdateError):
                    update.discover_install(self.fixture.module)
        for value in (0, -1, True, "1"):
            with self.subTest(edge_build=value):
                state = dict(self.fixture.state, channel="edge", ref="edge", edge_build=value)
                self.fixture.ref = "edge"
                self.fixture.write_current()
                self.fixture.write_state(json.dumps(state) + "\n")
                with self.assertRaises(update.UpdateError):
                    update.discover_install(self.fixture.module)

    def test_rejects_current_and_state_ownership_mismatches(self):
        self.fixture.write_current(f"{self.fixture.generation}\nv0.4.0\nextra\n")
        with self.assertRaisesRegex(update.UpdateError, "current pointer"):
            update.discover_install(self.fixture.module)
        self.fixture.write_current()
        for key, value in (
            ("root", str(self.fixture.base / "other")),
            ("current", str(self.fixture.base / "other")),
            ("ref", "v9.9.9"),
            ("bin_dir", "relative-bin"),
            ("launcher", str(self.fixture.base / "other-launcher")),
            ("schema", 2),
        ):
            with self.subTest(key=key):
                state = dict(self.fixture.state, **{key: value})
                self.fixture.write_state(json.dumps(state) + "\n")
                with self.assertRaises(update.UpdateError):
                    update.discover_install(self.fixture.module)

    def test_rejects_noncanonical_crlf_current_pointer(self):
        self.fixture.write_current()
        current = self.fixture.root / update.CURRENT_FILE
        current.write_bytes(
            f"{self.fixture.generation}\r\nv0.4.0\r\n".encode("utf-8")
        )
        with self.assertRaisesRegex(update.UpdateError, "current pointer"):
            update.discover_install(self.fixture.module)

    def test_rejects_duplicate_state_keys(self):
        text = json.dumps(self.fixture.state)
        self.fixture.write_state(text[:-1] + ',"root":"duplicate"}\n')
        with self.assertRaisesRegex(update.UpdateError, "duplicate key"):
            update.discover_install(self.fixture.module)

    def test_rejects_recursively_invalid_state_without_leaking_exception(self):
        with mock.patch.object(update.json, "loads", side_effect=RecursionError):
            with self.assertRaisesRegex(update.UpdateError, "installer state"):
                update.discover_install(self.fixture.module)

    def test_rejects_modified_launcher_and_missing_installer_or_python(self):
        self.fixture.launcher.write_text("# user launcher\n", encoding="utf-8")
        with self.assertRaisesRegex(update.UpdateError, "launcher"):
            update.discover_install(self.fixture.module)
        _, content = update._expected_launcher(self.fixture.root, self.fixture.bin_dir)
        self.fixture.launcher.write_text(
            content, encoding="utf-8", newline="\n"
        )
        if os.name != "nt":
            self.fixture.launcher.chmod(0o644)
            with self.assertRaisesRegex(update.UpdateError, "executable"):
                update.discover_install(self.fixture.module)
            self.fixture.launcher.chmod(0o755)
        self.fixture.installer.unlink()
        with self.assertRaisesRegex(update.UpdateError, "bundled installer"):
            update.discover_install(self.fixture.module)
        self.fixture.installer.write_text("# installer\n", encoding="utf-8")
        self.fixture.python.unlink()
        with self.assertRaisesRegex(update.UpdateError, "Python"):
            update.discover_install(self.fixture.module)

    def test_rejects_symlinked_ownership_files(self):
        targets = (
            self.fixture.root / update.ROOT_MARKER,
            self.fixture.root / update.CURRENT_FILE,
            self.fixture.root / update.STATE_FILE,
            self.fixture.launcher,
            self.fixture.installer,
        )
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        for path in targets:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                target = path.with_name(path.name + ".target")
                path.unlink()
                target.write_bytes(original)
                try:
                    path.symlink_to(target)
                except OSError:
                    self.skipTest("symlinks unavailable")
                with self.assertRaises(update.UpdateError):
                    update.discover_install(self.fixture.module)
                path.unlink()
                path.write_bytes(original)
                if path == self.fixture.launcher and os.name != "nt":
                    path.chmod(0o755)
                target.unlink()

    def test_rejects_source_checkout_layout_and_wrong_module_name(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "yt_ascii_update.py"
            module.write_text("# source\n", encoding="utf-8")
            with self.assertRaisesRegex(update.UpdateError, "managed"):
                update.discover_install(module)
        wrong = self.fixture.app / "renamed.py"
        wrong.write_text("# wrong\n", encoding="utf-8")
        with self.assertRaisesRegex(update.UpdateError, "updater module"):
            update.discover_install(wrong)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ManagedFixture()
        self.install = update.discover_install(self.fixture.module)

    def tearDown(self):
        self.fixture.close()

    def opener_for(self, payload, calls=None):
        def opener(request, timeout):
            if calls is not None:
                calls.append((request, timeout))
            return FakeResponse(payload)
        return opener

    def test_stable_update_equal_and_newer_local(self):
        cases = (
            (b"v0.5.0\n", True, "update available"),
            (b"v0.4.0\n", False, "up to date"),
            (b"v0.3.0\n", False, "newer than"),
        )
        for payload, available, message in cases:
            with self.subTest(payload=payload):
                status = update.check_for_update(
                    self.install, opener=self.opener_for(payload)
                )
                self.assertTrue(status.ok)
                self.assertEqual(status.update_available, available)
                self.assertIn(message, status.display)
                self.assertEqual(status.current, "v0.4.0")

    def test_stable_comparison_is_semantic(self):
        fixture = ManagedFixture(ref="v0.9.0")
        try:
            install = update.discover_install(fixture.module)
            status = update.check_for_update(
                install, opener=self.opener_for(b"v0.10.0\n")
            )
            self.assertTrue(status.update_available)
        finally:
            fixture.close()

    def test_edge_update_equal_newer_local_and_unknown_legacy(self):
        fixture = ManagedFixture(channel="edge", ref="edge", edge_build=7)
        try:
            install = update.discover_install(fixture.module)
            for payload, expected in ((b"8\n", True), (b"7\n", False), (b"6\n", False)):
                with self.subTest(payload=payload):
                    status = update.check_for_update(
                        install, opener=self.opener_for(payload)
                    )
                    self.assertEqual(status.update_available, expected)
                    self.assertEqual(status.available, f"edge build {int(payload)}")
            fixture.state.pop("channel")
            fixture.state.pop("edge_build")
            fixture.write_state()
            legacy = update.discover_install(fixture.module)
            status = update.check_for_update(
                legacy, opener=self.opener_for(b"7\n")
            )
            self.assertTrue(status.update_available)
            self.assertEqual(status.current, "edge build unknown")
        finally:
            fixture.close()

    def test_request_is_bounded_identified_and_uses_timeout(self):
        calls = []
        response = FakeResponse(b"v0.5.0\n")

        def opener(request, timeout):
            calls.append((request, timeout))
            return response

        status = update.check_for_update(self.install, timeout=3.25, opener=opener)
        self.assertTrue(status.update_available)
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(request.full_url, update.STABLE_URL)
        self.assertEqual(request.get_header("User-agent"), update.USER_AGENT)
        self.assertEqual(timeout, 3.25)
        self.assertEqual(response.read_size, update.MAX_MARKER_BYTES + 1)

    def test_network_parse_size_and_type_errors_are_results(self):
        cases = (
            self.opener_for(b"x" * 129),
            self.opener_for(b"not-a-tag\n"),
            self.opener_for("v0.5.0\n"),
            mock.Mock(side_effect=urllib.error.URLError("offline")),
            mock.Mock(side_effect=OSError("socket closed")),
        )
        for opener in cases:
            with self.subTest(opener=opener):
                status = update.check_for_update(self.install, opener=opener)
                self.assertFalse(status.ok)
                self.assertFalse(status.update_available)
                self.assertIsNone(status.available)
                self.assertIn("update check failed", status.display)

    def test_invalid_timeout_is_result_without_network(self):
        for timeout in (0, -1, float("inf"), float("nan"), True, "2"):
            with self.subTest(timeout=timeout):
                opener = mock.Mock(side_effect=AssertionError("network used"))
                status = update.check_for_update(
                    self.install, timeout=timeout, opener=opener
                )
                self.assertFalse(status.ok)
                opener.assert_not_called()

    def test_pinned_and_source_are_actionable_without_network(self):
        cases = (("pinned", "v0.4.0"), ("source", "source"))
        for channel, ref in cases:
            with self.subTest(channel=channel):
                fixture = ManagedFixture(channel=channel, ref=ref)
                try:
                    install = update.discover_install(fixture.module)
                    opener = mock.Mock(side_effect=AssertionError("network used"))
                    status = update.check_for_update(install, opener=opener)
                    self.assertTrue(status.ok)
                    self.assertFalse(status.supported)
                    self.assertFalse(status.update_available)
                    self.assertIn(channel if channel == "source" else "pinned", status.display)
                    opener.assert_not_called()
                finally:
                    fixture.close()


class AutomaticCheckTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ManagedFixture()
        self.install = update.discover_install(self.fixture.module)

    def tearDown(self):
        self.fixture.close()

    def wait_done(self, check):
        self.assertTrue(check._done.wait(2), "background check did not finish")

    def test_returns_one_actionable_notice(self):
        check = update.start_auto_check(
            self.install, opener=lambda *_args, **_kwargs: FakeResponse(b"v0.5.0\n")
        )
        self.assertTrue(check._thread.daemon)
        self.wait_done(check)
        status = check.consume()
        self.assertIsNotNone(status)
        self.assertTrue(status.update_available)
        self.assertIsNone(check.consume())

    def test_current_and_errors_are_silent(self):
        openers = (
            lambda *_args, **_kwargs: FakeResponse(b"v0.4.0\n"),
            mock.Mock(side_effect=OSError("offline")),
        )
        for opener in openers:
            with self.subTest(opener=opener):
                check = update.start_auto_check(self.install, opener=opener)
                self.wait_done(check)
                self.assertIsNone(check.consume())

    def test_unsupported_channel_finishes_without_thread_or_network(self):
        fixture = ManagedFixture(channel="pinned", ref="v0.4.0")
        try:
            install = update.discover_install(fixture.module)
            opener = mock.Mock(side_effect=AssertionError("network used"))
            check = update.start_auto_check(install, opener=opener)
            self.assertTrue(check.done)
            self.assertIsNone(check._thread)
            self.assertIsNone(check.consume())
            opener.assert_not_called()
        finally:
            fixture.close()

    def test_consume_never_waits_for_slow_network(self):
        release = threading.Event()

        def opener(*_args, **_kwargs):
            release.wait(2)
            return FakeResponse(b"v0.5.0\n")

        check = update.start_auto_check(self.install, opener=opener)
        started = time.monotonic()
        self.assertIsNone(check.consume())
        self.assertLess(time.monotonic() - started, 0.1)
        release.set()
        self.wait_done(check)
        self.assertTrue(check.consume().update_available)


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ManagedFixture()
        self.install = update.discover_install(self.fixture.module)

    def tearDown(self):
        self.fixture.close()

    def test_delegates_exact_managed_paths_and_propagates_status(self):
        calls = []

        def runner(command, check):
            calls.append((command, check))
            return SimpleNamespace(returncode=23)

        self.assertEqual(update.delegate_update(self.install, runner=runner), 23)
        self.assertEqual(
            calls,
            [
                (
                    [
                        str(self.fixture.python),
                        str(self.fixture.installer),
                        "--update",
                        "--install-root",
                        str(self.fixture.root),
                        "--bin-dir",
                        str(self.fixture.bin_dir),
                    ],
                    False,
                )
            ],
        )

    def test_refuses_unsupported_channels_before_running(self):
        for channel in ("pinned", "source"):
            with self.subTest(channel=channel):
                install = update.ManagedInstall(
                    **{**self.install.__dict__, "channel": channel}
                )
                runner = mock.Mock()
                with self.assertRaisesRegex(update.UpdateError, channel):
                    update.delegate_update(install, runner=runner)
                runner.assert_not_called()

    def test_rediscovery_detects_tampering(self):
        self.fixture.launcher.write_text("tampered\n", encoding="utf-8")
        runner = mock.Mock()
        with self.assertRaises(update.UpdateError):
            update.delegate_update(self.install, runner=runner)
        runner.assert_not_called()

    def test_runner_failures_are_reported(self):
        with self.assertRaisesRegex(update.UpdateError, "could not start"):
            update.delegate_update(
                self.install, runner=mock.Mock(side_effect=OSError("missing"))
            )
        with self.assertRaisesRegex(update.UpdateError, "exit status"):
            update.delegate_update(
                self.install, runner=mock.Mock(return_value=SimpleNamespace())
            )


if __name__ == "__main__":
    unittest.main()

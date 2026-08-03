import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import stat
import tempfile
import unittest
from unittest import mock
import zipfile

import install


def args(**overrides):
    values = {
        "edge": False,
        "version": None,
        "no_modify_path": True,
        "uninstall": False,
        "source_dir": None,
        "install_root": None,
        "bin_dir": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def zip_payload(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in entries:
            if isinstance(value, zipfile.ZipInfo):
                archive.writestr(value, b"target")
            else:
                archive.writestr(name, value)
    return output.getvalue()


def minimal_source(root):
    root.mkdir(parents=True)
    (root / "yt-ascii").write_text("print('ok')\n", encoding="utf-8")
    (root / "yt_ascii_renderer.py").write_text("# renderer\n", encoding="utf-8")
    (root / "yt_ascii_styles.py").write_text("# styles\n", encoding="utf-8")
    (root / "requirements.txt").write_text("# none\n", encoding="utf-8")
    return root


def expected_launcher_content(root, bin_dir):
    if os.name == "nt":
        return install.windows_launcher(root, bin_dir)
    return install.posix_launcher(root)


class VersionTests(unittest.TestCase):
    def test_strict_stable_tags(self):
        for value in ("v0.3.0", "v1.0.0", "v12.34.567"):
            with self.subTest(value=value):
                self.assertEqual(install.validate_tag(value), value)
        for value in (
            "0.3.0",
            "v01.2.3",
            "v1.02.3",
            "v1.2",
            "v1.2.3-rc1",
            "v1.2.3/../../x",
            "v1.2.3\nnext",
            "v0.2.0",
            "v0.0.0",
            "",
        ):
            with self.subTest(value=value):
                with self.assertRaises(install.InstallerError):
                    install.validate_tag(value)

    def test_stable_file_and_archive_urls(self):
        self.assertEqual(install.stable_tag(), "v0.4.0")
        self.assertTrue(install.archive_url("v0.4.0").endswith("/refs/tags/v0.4.0.zip"))
        self.assertTrue(install.archive_url("ignored", edge=True).endswith("/refs/heads/main.zip"))

class ArchiveTests(unittest.TestCase):
    def test_safe_archive_extracts_one_root(self):
        payload = zip_payload([
            ("repo/yt-ascii", b"entry"),
            ("repo/dir/file.txt", b"data"),
        ])
        with tempfile.TemporaryDirectory() as directory:
            root = install.safe_extract_zip(payload, Path(directory))
            self.assertEqual((root / "yt-ascii").read_bytes(), b"entry")
            self.assertEqual((root / "dir" / "file.txt").read_bytes(), b"data")

    def test_archive_rejects_traversal_absolute_backslash_and_symlink(self):
        link = zipfile.ZipInfo("repo/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        backslash = zipfile.ZipInfo("repo/outside")
        # ZipInfo normally rewrites os.sep while it is constructed.  Assign
        # the stored name afterward so this is a malformed archive member on
        # Windows too, rather than a normalized test fixture.
        backslash.filename = r"repo\outside"
        backslash.orig_filename = backslash.filename
        cases = [
            [("../outside", b"bad")],
            [("/absolute", b"bad")],
            [("D:/outside", b"bad")],
            [(backslash.filename, backslash)],
            [("repo/CON.txt", b"bad")],
            [("repo/CON .txt", b"bad")],
            [("repo/CONIN$", b"bad")],
            [("repo/COM¹.txt", b"bad")],
            [("repo/has?mark", b"bad")],
            [("repo/control\x01name", b"bad")],
            [("repo/trailing. ", b"bad")],
            [("repo/link", link)],
        ]
        for entries in cases:
            with self.subTest(entries=entries[0][0]):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(install.InstallerError):
                        install.safe_extract_zip(zip_payload(entries), Path(directory))

    def test_archive_rejects_case_collisions(self):
        entries = [("repo/File.txt", b"one"), ("repo/file.TXT", b"two")]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(install.InstallerError):
                install.safe_extract_zip(zip_payload(entries), Path(directory))

    def test_archive_rejects_multiple_roots_and_expansion_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(install.InstallerError):
                install.safe_extract_zip(
                    zip_payload([("one/file", b"x"), ("two/file", b"y")]),
                    Path(directory),
                )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            install, "MAX_EXTRACTED", 2
        ):
            with self.assertRaises(install.InstallerError):
                install.safe_extract_zip(
                    zip_payload([("repo/file", b"123")]), Path(directory)
                )

    def test_local_source_copy_excludes_generated_trees(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            (source / ".git").mkdir()
            (source / ".git" / "secret").write_text("x", encoding="utf-8")
            (source / "build").mkdir()
            (source / "build" / "large").write_text("x", encoding="utf-8")
            app = install.stage_source(base / "version", source_dir=source)
            self.assertTrue((app / "yt-ascii").is_file())
            self.assertFalse((app / ".git").exists())
            self.assertFalse((app / "build").exists())

    def test_source_requires_style_module(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            (source / "yt_ascii_styles.py").unlink()
            with self.assertRaisesRegex(install.InstallerError, "yt_ascii_styles.py"):
                install.stage_source(base / "version", source_dir=source)

    def test_v03_archive_remains_installable_without_style_module(self):
        payload = zip_payload([
            ("repo/yt-ascii", b"entry"),
            ("repo/yt_ascii_renderer.py", b"# renderer\n"),
            ("repo/requirements.txt", b"# requirements\n"),
        ])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            install, "fetch_bytes", return_value=payload
        ):
            version_dir = Path(directory) / "version"
            version_dir.mkdir()
            app = install.stage_source(version_dir, ref="v0.3.0")
            self.assertTrue((app / "yt-ascii").is_file())
            self.assertFalse((app / "yt_ascii_styles.py").exists())

    def test_v04_and_edge_archives_require_style_module(self):
        payload = zip_payload([
            ("repo/yt-ascii", b"entry"),
            ("repo/yt_ascii_renderer.py", b"# renderer\n"),
            ("repo/requirements.txt", b"# requirements\n"),
        ])
        cases = [
            {"ref": "v0.4.0"},
            {"ref": "edge", "edge": True},
        ]
        for index, options in enumerate(cases):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory, \
                 mock.patch.object(install, "fetch_bytes", return_value=payload):
                version_dir = Path(directory) / f"version-{index}"
                version_dir.mkdir()
                with self.assertRaisesRegex(
                    install.InstallerError, "yt_ascii_styles.py"
                ):
                    install.stage_source(version_dir, **options)


class LauncherAndPathTests(unittest.TestCase):
    def test_launchers_use_the_atomic_current_pointer(self):
        root = Path("/tmp/path with spaces/yt-ascii")
        posix = install.posix_launcher(root)
        windows = install.windows_launcher(Path(r"C:\Users\Name\yt-ascii"))
        windows_default = install.windows_launcher(
            Path(r"C:\Users\Náme\yt-ascii"),
            Path(r"C:\Users\Náme\yt-ascii\bin"),
        )
        self.assertIn(install.MANAGED_MARKER, posix)
        self.assertIn("IFS= read -r YTASCII_INSTALL_REF", posix)
        self.assertNotIn("sed ", posix)
        self.assertIn(install.MANAGED_MARKER, windows)
        self.assertIn(r"%YTASCII_CURRENT%\venv\Scripts\python.exe", windows)
        self.assertNotIn("\v", windows.replace(r"\venv", ""))
        self.assertIn(r"%~dp0..", windows_default)
        self.assertNotIn("Náme", windows_default)

    def test_profile_block_is_idempotent_and_removable(self):
        original = "export EXISTING=1\n"
        bin_dir = Path("/tmp/bin with spaces")
        markers = install.posix_path_markers(bin_dir)
        block = install.posix_path_block(bin_dir, "posix")
        once = install.replace_marked_block(original, block)
        twice = install.replace_marked_block(once, block)
        self.assertEqual(once, twice)
        self.assertEqual(once.count(markers[0]), 1)
        removed = install.replace_marked_block(once, "", markers)
        self.assertEqual(removed, original)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_atomic_profile_update_preserves_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile"
            path.write_text("old\n", encoding="utf-8")
            path.chmod(0o600)
            install.write_atomic(path, "new\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX symlink test")
    def test_atomic_write_does_not_follow_predictable_temp_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            destination = base / "state"
            sentinel = base / "sentinel"
            sentinel.write_text("keep\n", encoding="utf-8")
            predictable = base / f".{destination.name}.{os.getpid()}.tmp"
            predictable.symlink_to(sentinel)

            install.write_atomic(destination, "managed\n")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(destination.read_text(encoding="utf-8"), "managed\n")
            self.assertTrue(predictable.is_symlink())

    @unittest.skipIf(os.name == "nt", "POSIX profile test")
    def test_posix_profile_management_preserves_existing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profile = home / ".bashrc"
            profile.write_text("export KEEP=1\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"HOME": str(home), "SHELL": "/bin/bash", "PATH": "/usr/bin"},
                clear=False,
            ):
                changed = install.add_posix_path(home / ".local" / "bin")
                install.add_posix_path(home / ".local" / "bin")
                self.assertEqual(changed, [profile])
                start, _end = install.posix_path_markers(home / ".local" / "bin")
                self.assertEqual(profile.read_text().count(start), 1)
                install.remove_posix_path(changed, home / ".local" / "bin")
            self.assertEqual(profile.read_text(encoding="utf-8"), "export KEEP=1\n")

    @unittest.skipIf(os.name == "nt", "POSIX profile test")
    def test_non_utf8_profile_reports_an_installer_error(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profile = home / ".bashrc"
            profile.write_bytes(b"\xff\xfe")
            with mock.patch.dict(
                os.environ,
                {"HOME": str(home), "SHELL": "/bin/bash", "PATH": "/usr/bin"},
                clear=False,
            ):
                with self.assertRaises(install.InstallerError):
                    install.add_posix_path(home / ".local" / "bin")
            self.assertEqual(profile.read_bytes(), b"\xff\xfe")

    @unittest.skipIf(os.name == "nt", "POSIX profile test")
    def test_posix_path_blocks_are_owned_per_bin_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            first_bin = home / "first-bin"
            second_bin = home / "second-bin"
            profile = home / ".bashrc"
            with mock.patch.dict(
                os.environ,
                {"HOME": str(home), "SHELL": "/bin/bash", "PATH": "/usr/bin"},
                clear=False,
            ):
                install.add_posix_path(first_bin)
                install.add_posix_path(second_bin)
                install.remove_posix_path([profile], first_bin)

            content = profile.read_text(encoding="utf-8")
            self.assertNotIn(install.posix_path_markers(first_bin)[0], content)
            self.assertIn(install.posix_path_markers(second_bin)[0], content)
            self.assertIn(str(second_bin), content)

    def test_windows_path_add_remove_is_case_insensitive(self):
        values = [r"C:\Windows;C:\Users\A\Bin"]

        def set_value(value, _kind=None):
            values[0] = value

        with mock.patch.object(install, "windows_user_path", side_effect=lambda: (values[0], 2)), \
             mock.patch.object(install, "set_windows_user_path", side_effect=set_value):
            self.assertFalse(install.add_windows_path(Path(r"c:\users\a\bin")))
            self.assertTrue(install.add_windows_path(Path(r"C:\Tools\YT")))
            self.assertIn(r"C:\Tools\YT", values[0])
            install.remove_windows_path(Path(r"c:\tools\yt"))
            self.assertNotIn("Tools", values[0])

    def test_windows_path_removal_reports_registry_failure(self):
        with mock.patch.object(
            install,
            "windows_user_path",
            side_effect=install.InstallerError("synthetic registry failure"),
        ):
            self.assertFalse(install.remove_windows_path(Path(r"C:\Tools\YT")))

    def test_windows_path_configuration_reports_ownership(self):
        bin_dir = Path("managed-bin")
        with mock.patch.object(install.os, "name", "nt"), mock.patch.object(
            install, "add_windows_path", return_value=True
        ) as add:
            self.assertEqual(install.configure_path(bin_dir, False), ([], True))
            self.assertEqual(install.configure_path(bin_dir, True), ([], False))
        add.assert_called_once_with(bin_dir)


class LifecycleTests(unittest.TestCase):
    def test_installer_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            with install.installer_lock(root):
                with self.assertRaises(install.InstallerError):
                    with install.installer_lock(root):
                        pass
            with install.installer_lock(root):
                pass

    def test_unmanaged_launcher_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            bin_dir = base / "bin"
            bin_dir.mkdir()
            launcher = install.launcher_path(bin_dir)
            launcher.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
            with self.assertRaises(install.InstallerError):
                install.install(args(
                    source_dir=source,
                    install_root=base / "data",
                    bin_dir=bin_dir,
                ))
            self.assertEqual(launcher.read_text(), "#!/bin/sh\necho mine\n")
            self.assertFalse((base / "data").exists())

    def test_launcher_owned_by_another_root_is_never_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            first_root = base / "first"
            second_root = base / "second"
            bin_dir = base / "bin"
            bin_dir.mkdir()
            launcher = install.launcher_path(bin_dir)
            first_content = expected_launcher_content(first_root, bin_dir)
            launcher.write_text(first_content, encoding="utf-8")

            with self.assertRaises(install.InstallerError):
                install.install(args(
                    source_dir=source,
                    install_root=second_root,
                    bin_dir=bin_dir,
                ))

            self.assertEqual(launcher.read_text(encoding="utf-8"), first_content)
            self.assertFalse(second_root.exists())

    def test_uninstall_does_not_remove_another_roots_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first_root = base / "first"
            second_root = base / "second"
            bin_dir = base / "bin"
            (second_root / "versions").mkdir(parents=True)
            (second_root / install.ROOT_MARKER).write_text(
                install.ROOT_MARKER_CONTENT, encoding="ascii"
            )
            bin_dir.mkdir()
            launcher = install.launcher_path(bin_dir)
            first_content = expected_launcher_content(first_root, bin_dir)
            launcher.write_text(first_content, encoding="utf-8")

            install.uninstall(args(install_root=second_root, bin_dir=bin_dir))

            self.assertEqual(launcher.read_text(encoding="utf-8"), first_content)

    @unittest.skipIf(os.name == "nt", "POSIX symlink test")
    def test_symlinked_marker_is_not_followed_or_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            root = base / "data"
            root.mkdir()
            target = base / "target"
            target.write_text("keep\n", encoding="utf-8")
            (root / install.ROOT_MARKER).symlink_to(target)
            with self.assertRaises(install.InstallerError):
                install.install(args(
                    source_dir=source,
                    install_root=root,
                    bin_dir=base / "bin",
                ))
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")
            install.uninstall(args(install_root=root, bin_dir=base / "bin"))
            self.assertTrue((root / install.ROOT_MARKER).is_symlink())

    def test_uninstall_requires_exact_root_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "data"
            versions = root / "versions"
            versions.mkdir(parents=True)
            sentinel = versions / "keep"
            sentinel.write_text("user", encoding="utf-8")
            (root / install.ROOT_MARKER).write_text(
                "not the installer marker\n", encoding="utf-8"
            )
            install.uninstall(args(install_root=root, bin_dir=base / "bin"))
            self.assertTrue(sentinel.is_file())

    @unittest.skipIf(os.name == "nt", "POSIX symlink test")
    def test_uninstall_rejects_unexpected_versions_path_before_detaching(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "data"
            bin_dir = base / "bin"
            root.mkdir()
            bin_dir.mkdir()
            (root / install.ROOT_MARKER).write_text(
                install.ROOT_MARKER_CONTENT, encoding="ascii"
            )
            target = base / "outside"
            target.mkdir()
            sentinel = target / "keep"
            sentinel.write_text("user", encoding="utf-8")
            (root / "versions").symlink_to(target, target_is_directory=True)
            launcher = install.launcher_path(bin_dir)
            launcher.write_text(install.posix_launcher(root), encoding="utf-8")

            with self.assertRaises(install.InstallerError):
                install.uninstall(args(install_root=root, bin_dir=bin_dir))

            self.assertTrue(sentinel.is_file())
            self.assertTrue(launcher.is_file())
            self.assertTrue((root / install.ROOT_MARKER).is_file())

    def test_failed_first_install_leaves_no_managed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            root = base / "data"
            bin_dir = base / "bin"
            with mock.patch.object(
                install,
                "build_environment",
                side_effect=install.InstallerError("synthetic failure"),
            ):
                with self.assertRaises(install.InstallerError):
                    install.install(args(
                        source_dir=source,
                        install_root=root,
                        bin_dir=bin_dir,
                    ))
            self.assertFalse(root.exists())
            self.assertFalse(install.launcher_path(bin_dir).exists())

    def test_state_write_failure_rolls_back_path_pointer_and_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            root = base / "data"
            bin_dir = base / "bin"
            with mock.patch.object(
                install, "build_environment", return_value=None
            ), mock.patch.object(
                install, "configure_path", return_value=([], True)
            ), mock.patch.object(
                install, "write_state", side_effect=OSError("synthetic state failure")
            ), mock.patch.object(
                install, "remove_windows_path", return_value=True
            ) as remove_path:
                with self.assertRaises(install.InstallerError):
                    install.install(args(
                        source_dir=source,
                        install_root=root,
                        bin_dir=bin_dir,
                    ))

            remove_path.assert_called_once_with(bin_dir.resolve())
            self.assertFalse(root.exists())
            self.assertFalse(install.launcher_path(bin_dir).exists())

    def test_failed_path_rollback_retains_recovery_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            root = base / "data"
            bin_dir = base / "bin"
            real_write_state = install.write_state
            attempts = [0]

            def fail_once(*call_args, **call_kwargs):
                attempts[0] += 1
                if attempts[0] == 1:
                    raise OSError("synthetic state failure")
                return real_write_state(*call_args, **call_kwargs)

            with mock.patch.object(
                install, "build_environment", return_value=None
            ), mock.patch.object(
                install, "configure_path", return_value=([], True)
            ), mock.patch.object(
                install, "write_state", side_effect=fail_once
            ), mock.patch.object(
                install, "remove_windows_path", return_value=False
            ):
                with self.assertRaises(install.InstallerError):
                    install.install(args(
                        source_dir=source,
                        install_root=root,
                        bin_dir=bin_dir,
                    ))

            current, current_ref = install.read_current(root)
            self.assertEqual(current_ref, "source")
            self.assertTrue(current.is_dir())
            self.assertTrue(install.launcher_path(bin_dir).is_file())
            self.assertTrue(install.valid_root_marker(root / install.ROOT_MARKER))
            self.assertTrue(install.read_state(root)["windows_path_added"])

    def test_local_source_and_destinations_cannot_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            source = minimal_source(Path(directory) / "source")
            with self.assertRaises(install.InstallerError):
                install.install(args(
                    source_dir=source,
                    install_root=source / ".installed",
                    bin_dir=Path(directory) / "bin",
                ))

    def test_install_upgrade_and_uninstall_preserve_unknown_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            root = base / "data"
            bin_dir = base / "bin"
            options = args(source_dir=source, install_root=root, bin_dir=bin_dir)
            with mock.patch.object(install, "build_environment", return_value=None):
                install.install(options)
                first, first_ref = install.read_current(root)
                self.assertEqual(first_ref, "source")
                self.assertTrue(first.is_dir())
                self.assertTrue(install.managed_launcher(install.launcher_path(bin_dir)))
                state = json.loads((root / install.STATE_FILE).read_text())
                self.assertEqual(state["ref"], "source")

                install.install(options)
                second, second_ref = install.read_current(root)
                self.assertEqual(second_ref, "source")
                self.assertNotEqual(first, second)
                self.assertTrue(first.exists())

                install.install(options)
                third, third_ref = install.read_current(root)
                self.assertEqual(third_ref, "source")
                self.assertNotEqual(second, third)
                self.assertFalse(first.exists())
                self.assertTrue(second.exists())

            keep = root / "keep.txt"
            keep.write_text("user data", encoding="utf-8")
            install.uninstall(args(install_root=root, bin_dir=bin_dir))
            self.assertTrue(keep.is_file())
            self.assertFalse((root / "versions").exists())
            self.assertFalse(install.launcher_path(bin_dir).exists())

    def test_failed_upgrade_keeps_previous_pointer_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            root = base / "data"
            bin_dir = base / "bin"
            options = args(source_dir=source, install_root=root, bin_dir=bin_dir)
            with mock.patch.object(install, "build_environment", return_value=None):
                install.install(options)
            first, first_ref = install.read_current(root)

            with mock.patch.object(
                install,
                "build_environment",
                side_effect=install.InstallerError("synthetic upgrade failure"),
            ):
                with self.assertRaises(install.InstallerError):
                    install.install(options)

            current, current_ref = install.read_current(root)
            self.assertEqual((current, current_ref), (first, first_ref))
            self.assertTrue(first.is_dir())
            versions = list((root / "versions").iterdir())
            self.assertEqual(len(versions), 1)
            self.assertTrue(versions[0].samefile(first))

    def test_existing_install_rejects_bin_directory_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            root = base / "data"
            first_bin = base / "first-bin"
            second_bin = base / "second-bin"
            first_options = args(
                source_dir=source, install_root=root, bin_dir=first_bin
            )
            with mock.patch.object(install, "build_environment", return_value=None):
                install.install(first_options)
            previous = install.read_current(root)

            with mock.patch.object(install, "build_environment") as build:
                with self.assertRaises(install.InstallerError):
                    install.install(args(
                        source_dir=source,
                        install_root=root,
                        bin_dir=second_bin,
                    ))

            build.assert_not_called()
            self.assertEqual(install.read_current(root), previous)
            self.assertTrue(install.launcher_path(first_bin).is_file())
            self.assertFalse(install.launcher_path(second_bin).exists())

    @unittest.skipIf(os.name == "nt", "POSIX executable mode test")
    def test_upgrade_repairs_owned_launcher_execute_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            root = base / "data"
            bin_dir = base / "bin"
            options = args(source_dir=source, install_root=root, bin_dir=bin_dir)
            with mock.patch.object(install, "build_environment", return_value=None):
                install.install(options)
                launcher = install.launcher_path(bin_dir)
                launcher.chmod(0o600)
                install.install(options)

            self.assertTrue(launcher.stat().st_mode & stat.S_IXUSR)

    def test_uninstall_retains_metadata_when_path_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            root = base / "data"
            bin_dir = base / "bin"
            options = args(source_dir=source, install_root=root, bin_dir=bin_dir)
            with mock.patch.object(install, "build_environment", return_value=None):
                install.install(options)

            remover = "remove_windows_path" if os.name == "nt" else "remove_posix_path"
            if os.name == "nt":
                state = install.read_state(root)
                state["windows_path_added"] = True
                install.write_atomic(
                    root / install.STATE_FILE,
                    json.dumps(state, indent=2, sort_keys=True) + "\n",
                )
            with mock.patch.object(install, remover, return_value=False):
                with self.assertRaises(install.InstallerError):
                    install.uninstall(args(install_root=root, bin_dir=bin_dir))

            self.assertTrue((root / install.STATE_FILE).is_file())
            self.assertTrue((root / install.ROOT_MARKER).is_file())
            self.assertTrue(install.launcher_path(bin_dir).is_file())

            with mock.patch.object(install, remover, return_value=True):
                install.uninstall(args(install_root=root, bin_dir=bin_dir))
            self.assertFalse((root / install.ROOT_MARKER).exists())
            self.assertFalse(install.launcher_path(bin_dir).exists())

    def test_windows_path_ownership_survives_an_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = minimal_source(base / "source")
            root = base / "data"
            bin_dir = base / "bin"
            options = args(source_dir=source, install_root=root, bin_dir=bin_dir)
            with mock.patch.object(install, "build_environment", return_value=None), \
                 mock.patch.object(
                     install, "configure_path", side_effect=[([], True), ([], False)]
                 ):
                install.install(options)
                install.install(options)
            state = install.read_state(root)
            self.assertIs(state["windows_path_added"], True)


if __name__ == "__main__":
    unittest.main()

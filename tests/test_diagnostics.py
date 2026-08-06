import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from yt_ascii_diagnostics import (
    DiagnosticsReportError,
    MAX_EVENTS,
    MAX_MINUTE_SAMPLES,
    PlaybackDiagnostics,
    TIMING_BUCKETS_MS,
    TimingHistogram,
    parse_ffmpeg_benchmark,
)


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.cpu = 0.0

    def monotonic(self):
        return self.value

    def process_time(self):
        return self.cpu

    def advance(self, seconds, cpu=None):
        self.value += seconds
        self.cpu += seconds * 0.25 if cpu is None else cpu


class FakeResourceReader:
    def __init__(self):
        self.calls = 0
        self.child_calls = 0
        self.capabilities = {
            "rss": {"supported": True, "kind": "current", "provider": "test"},
            "fd": {"supported": True, "kind": "current", "provider": "test"},
            "child_rss": {"supported": True, "kind": "current", "provider": "test"},
            "child_fd": {"supported": True, "kind": "current", "provider": "test"},
        }

    def read(self):
        self.calls += 1
        return {
            "rss_bytes": 10_000_000 + self.calls * 1_000,
            "fd_count": 5,
        }

    def read_child(self, pid):
        self.child_calls += 1
        return {"rss_bytes": 2_000_000 + pid, "fd_count": 3}


class GrowingResourceReader(FakeResourceReader):
    def read(self):
        self.calls += 1
        return {
            "rss_bytes": 10_000_000 + self.calls * 200_000,
            "fd_count": 5 if self.calls < 10 else 9,
        }


class TimingHistogramTests(unittest.TestCase):
    def test_fixed_histogram_is_bounded_and_reports_quantiles(self):
        histogram = TimingHistogram()
        for index in range(20_000):
            histogram.add_seconds((index % 100) / 10_000.0)
        result = histogram.as_dict()
        self.assertEqual(result["count"], 20_000)
        self.assertLessEqual(
            len(result["histogram"]["buckets"]), len(TIMING_BUCKETS_MS)
        )
        self.assertLessEqual(result["p50_ms"], result["p95_ms"])
        self.assertLessEqual(result["p95_ms"], result["p99_ms"])
        self.assertAlmostEqual(result["maximum_ms"], 9.9)

    def test_histogram_rejects_invalid_samples(self):
        for value in (-1, float("inf"), float("nan"), "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TimingHistogram().add_seconds(value)


class PlaybackDiagnosticsTests(unittest.TestCase):
    CURATED_STYLES = (
        "classic", "bayer", "posterize", "contour", "edge-glow",
        "ordered-dither", "error-diffusion", "duotone", "two-tone", "riso",
    )
    CURATED_EFFECTS = (
        "none", "pixelate", "glitch", "crt", "chromatic-shift", "wave", "trails",
        "prism", "digital-rain", "terminal-hud",
    )

    def make_diagnostics(self, directory, **overrides):
        clock = overrides.pop("clock", FakeClock())
        reader = overrides.pop("resource_reader", FakeResourceReader())
        config = overrides.pop(
            "config", {"target_fps": 10, "width": 120, "height": 34}
        )
        diagnostics = PlaybackDiagnostics(
            Path(directory) / "report.json",
            config=config,
            clock=clock.monotonic,
            process_clock=clock.process_time,
            resource_reader=reader,
            **overrides,
        )
        return diagnostics, clock, reader

    def test_warmup_exclusion_duration_and_frame_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, clock, _ = self.make_diagnostics(
                directory, warmup_seconds=2, duration_seconds=1,
                profile="ordinary",
            )
            diagnostics.record_frame(
                output_bytes=100, dropped=2,
                lateness_seconds=0.001, loop_seconds=0.002,
            )
            diagnostics.record_timing("effect", 0.001)
            clock.advance(1.0)
            diagnostics.record_frame(output_bytes=100)
            self.assertFalse(diagnostics.tick())
            clock.advance(1.0)
            for _ in range(10):
                diagnostics.record_frame(
                    output_bytes=100,
                    lateness_seconds=0.001,
                    loop_seconds=0.002,
                )
                clock.advance(0.1)
            self.assertTrue(diagnostics.tick())
            diagnostics.record_cleanup(0.1)
            report = diagnostics.finalize("duration")

            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["counts"]["all"]["frames_presented"], 12)
            self.assertEqual(report["counts"]["measured"]["frames_presented"], 10)
            self.assertEqual(report["counts"]["maximum_drop_burst"], 2)
            self.assertGreaterEqual(report["timings_ms"]["excluded_samples"], 1)
            self.assertTrue(report["status"]["measurement_complete"])
            self.assertEqual(report["gates"]["results"]["lifecycle"]["status"], "pass")
            self.assertEqual(
                report["gates"]["results"]["measurement_duration"]["status"],
                "pass",
            )

    def test_duration_does_not_start_before_first_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, clock, _ = self.make_diagnostics(
                directory, duration_seconds=1
            )
            clock.advance(50)
            self.assertFalse(diagnostics.tick())
            diagnostics.mark_first_frame()
            clock.advance(0.99)
            self.assertFalse(diagnostics.should_stop)
            clock.advance(0.01)
            self.assertTrue(diagnostics.should_stop)

    def test_configuration_source_events_and_environment_are_redacted(self):
        secret = "PRIVATE-secret-title-user-path"
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            diagnostics = PlaybackDiagnostics(
                Path(directory) / "report.json",
                config={
                    "target_fps": 30,
                    "style": secret,
                    "effect_text_sha256": "b" * 64,
                    "url": "https://example.invalid/private",
                    "effect_text": secret,
                    "argv": [secret],
                },
                source={
                    "kind": secret, "title": secret,
                    "url": "https://example.invalid/signed",
                    "path": f"/tmp/{secret}",
                },
                environment={
                    "stdout_tty": True, "hostname": secret, "user": secret,
                    "output_environment": "tmux",
                },
                clock=clock.monotonic,
                process_clock=clock.process_time,
                resource_reader=FakeResourceReader(),
            )
            diagnostics.set_source_metadata(
                width=854, height=480, duration_seconds=60, live=True, fps=60
            )
            diagnostics.event(
                "control", reason=secret,
                title=secret, path=f"/tmp/{secret}", count=1,
            )
            diagnostics.event(
                "child_before_stop", reason="audio", success=True,
            )
            diagnostics.record_cleanup(0.01)
            report = diagnostics.finalize("probe_error", "PrivateError")
            serialized = json.dumps(report)
            self.assertNotIn(secret, serialized)
            self.assertNotIn("example.invalid", serialized)
            self.assertNotIn("/tmp/", serialized)
            self.assertEqual(report["source"]["live"], True)
            self.assertEqual(report["source"]["width"], 854)
            self.assertNotIn("url", report["source"])
            self.assertEqual(report["source"]["kind"], "redacted")
            self.assertEqual(report["config"]["style"], "redacted")
            self.assertEqual(
                report["config"]["effect_text_sha256"], "b" * 64
            )
            self.assertNotIn("hostname", report["environment"])
            self.assertEqual(
                report["events"]["records"][0]["fields"]["reason"],
                "redacted",
            )
            self.assertEqual(
                report["events"]["records"][1],
                {
                    "name": "child_before_stop",
                    "elapsed_seconds": 0.0,
                    "measured": False,
                    "fields": {"reason": "audio", "success": True},
                },
            )

    def test_renderer_metadata_uses_canonical_backend_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, _, _ = self.make_diagnostics(
                directory,
                config={
                    "target_fps": 30,
                    "color": False,
                    "render_backend": "cells",
                    "effective_render_backend": "chars",
                    "effect": "wave",
                    "glyph_mode": "ascii",
                    # Pre-pivot metadata must not survive into schema v1.
                    "pixels": True,
                    "presentation": "pixels-to-chars",
                    "effect_glyphs": "unicode",
                },
            )
            diagnostics.record_cleanup(0.01)
            report = diagnostics.finalize("normal")
            config = report["config"]

            self.assertEqual(config["render_backend"], "cells")
            self.assertEqual(config["effective_render_backend"], "chars")
            self.assertEqual(config["effect"], "wave")
            self.assertEqual(config["glyph_mode"], "ascii")
            self.assertNotIn("pixels", config)
            self.assertNotIn("presentation", config)
            self.assertNotIn("effect_glyphs", config)

    def test_palette_control_events_allow_names_but_never_custom_glyphs(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, _, _ = self.make_diagnostics(directory)
            diagnostics.event(
                "control", reason="palette", palette="blocks"
            )
            diagnostics.event(
                "control", reason="palette", palette="PRIVATE-λ-glyphs"
            )
            diagnostics.record_cleanup(0.01)
            report = diagnostics.finalize("normal")
            records = report["events"]["records"]
            self.assertEqual(
                records[0]["fields"],
                {"reason": "palette", "palette": "blocks"},
            )
            self.assertEqual(
                records[1]["fields"],
                {"reason": "palette", "palette": "redacted"},
            )

    def test_resolved_output_geometry_controls_profile_and_report_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, _, _ = self.make_diagnostics(
                directory,
                config={"target_fps": 60, "width": None, "height": None},
            )
            self.assertEqual(
                diagnostics.set_output_geometry(
                    width=240, height=68, target_fps=60
                ),
                "stress",
            )
            self.assertEqual(diagnostics.profile, "stress")
            self.assertEqual(diagnostics.config["profile"], "stress")
            self.assertEqual(diagnostics.config["width"], 240)
            self.assertEqual(diagnostics.config["height"], 68)
            self.assertEqual(diagnostics.config["target_fps"], 60.0)
            self.assertEqual(
                diagnostics.set_output_geometry(
                    width=120, height=34, target_fps=30
                ),
                "ordinary",
            )
            with self.assertRaises(ValueError):
                diagnostics.set_output_geometry(
                    width=0, height=34, target_fps=30
                )

    def test_renderer_and_effect_metadata_are_closed_token_sets(self):
        retired_effects = (
            "contour-glyph", "number-field", "glyph-grid", "word-field",
            "inscription", "type-echo", "type-collage",
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, effect in enumerate(self.CURATED_EFFECTS):
                diagnostics = PlaybackDiagnostics(
                    Path(directory) / f"accepted-{index}.json",
                    config={
                        "effect": effect,
                        "render_backend": "half-block",
                        "effective_render_backend": "half-block",
                    },
                    resource_reader=FakeResourceReader(),
                )
                self.assertEqual(diagnostics.config["effect"], effect)
                self.assertEqual(
                    diagnostics.config["render_backend"], "half-block"
                )
                self.assertEqual(
                    diagnostics.config["effective_render_backend"],
                    "half-block",
                )

            for index, style in enumerate(self.CURATED_STYLES):
                diagnostics = PlaybackDiagnostics(
                    Path(directory) / f"accepted-style-{index}.json",
                    config={"style": style},
                    resource_reader=FakeResourceReader(),
                )
                self.assertEqual(diagnostics.config["style"], style)

            for index, effect in enumerate(retired_effects):
                diagnostics = PlaybackDiagnostics(
                    Path(directory) / f"retired-{index}.json",
                    config={"effect": effect},
                    resource_reader=FakeResourceReader(),
                )
                self.assertEqual(diagnostics.config["effect"], "redacted")

            diagnostics = PlaybackDiagnostics(
                Path(directory) / "invalid-backends.json",
                config={
                    "render_backend": "kitty",
                    "effective_render_backend": "pixels-to-chars",
                },
                resource_reader=FakeResourceReader(),
            )
            self.assertEqual(diagnostics.config["render_backend"], "redacted")
            self.assertEqual(
                diagnostics.config["effective_render_backend"], "redacted"
            )

    def test_event_and_resource_history_stay_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, clock, reader = self.make_diagnostics(directory)
            diagnostics.register_child(123, "video")
            diagnostics.mark_first_frame()
            for index in range(MAX_EVENTS + 25):
                diagnostics.event("control", count=index)
            for _ in range((MAX_MINUTE_SAMPLES + 10) * 60):
                clock.advance(1.0)
                diagnostics.tick()
            diagnostics.unregister_child(123)
            diagnostics.record_cleanup(0.1)
            report = diagnostics.finalize("normal")
            self.assertEqual(len(report["events"]["records"]), MAX_EVENTS)
            self.assertGreater(report["events"]["overflow"], 0)
            parent = report["resources"]["parent"]
            self.assertLessEqual(
                len(parent["rss_bytes"]["minute_medians"]),
                MAX_MINUTE_SAMPLES,
            )
            self.assertGreater(reader.child_calls, 0)
            children = report["resources"]["children"]
            self.assertEqual(children["roles_seen"], ["video"])
            self.assertGreater(children["aggregate_rss_bytes"]["sample_count"], 0)
            self.assertIn("mean_percent", parent["cpu"]["sample_percent"])

    def test_one_hz_resource_sampling_skips_subsecond_ticks(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, clock, reader = self.make_diagnostics(directory)
            initial = reader.calls
            for _ in range(9):
                clock.advance(0.1)
                diagnostics.tick()
            self.assertEqual(reader.calls, initial)
            clock.advance(0.11)
            diagnostics.tick()
            self.assertEqual(reader.calls, initial + 1)

    def test_long_run_resource_growth_is_a_hard_diagnosis(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            diagnostics, _, _ = self.make_diagnostics(
                directory, clock=clock,
                resource_reader=GrowingResourceReader(),
            )
            diagnostics.mark_first_frame()
            diagnostics.record_frame(lateness_seconds=0, loop_seconds=0)
            for _ in range(301):
                clock.advance(1.0)
                diagnostics.tick()
            diagnostics.record_cleanup(0.1)
            report = diagnostics.finalize("normal")
            for name in ("rss_growth", "rss_slope", "fd_growth"):
                self.assertEqual(
                    report["gates"]["results"][name]["status"], "fail"
                )
            diagnosis = next(
                item for item in report["diagnoses"]
                if item["code"] == "possible_memory_growth"
            )
            self.assertEqual(diagnosis["severity"], "error")

    def test_gates_are_target_relative_and_stress_capacity_is_nonhard(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, clock, _ = self.make_diagnostics(
                directory, duration_seconds=1, profile="stress"
            )
            diagnostics.mark_first_frame()
            for _ in range(8):
                clock.advance(0.125)
                diagnostics.record_timing("effect", 0.06)
                diagnostics.record_timing("ansi", 0.001)
                diagnostics.record_frame(
                    lateness_seconds=0.2, loop_seconds=0.01
                )
            diagnostics.record_cleanup(0.1)
            report = diagnostics.finalize("duration")
            fps_gate = report["gates"]["results"]["presented_fps"]
            self.assertEqual(fps_gate["threshold"], 9.5)
            self.assertEqual(fps_gate["status"], "fail")
            self.assertFalse(fps_gate["hard"])
            self.assertTrue(report["gates"]["hard_pass"])
            overload = next(
                item for item in report["diagnoses"]
                if item["code"] == "sustained_overload"
            )
            self.assertEqual(overload["severity"], "warning")
            self.assertEqual(
                set(overload),
                {"code", "severity", "observed", "threshold", "remediation"},
            )

    def test_scheduler_oversleep_is_counted_as_a_visible_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, clock, _ = self.make_diagnostics(
                directory, duration_seconds=1, profile="ordinary"
            )
            diagnostics.mark_first_frame()
            for index in range(10):
                clock.advance(0.1)
                diagnostics.record_frame(
                    lateness_seconds=0.0, loop_seconds=0.01
                )
                if index == 4:
                    diagnostics.record_timing("sleep_overshoot", 0.6)
            diagnostics.record_cleanup(0.1)
            report = diagnostics.finalize("duration")
            freeze = report["gates"]["results"]["maximum_freeze"]
            self.assertEqual(freeze["value"], 600.0)
            self.assertEqual(freeze["status"], "fail")
            self.assertTrue(freeze["hard"])

    def test_bottleneck_diagnoses_include_observations_and_remediation(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, clock, _ = self.make_diagnostics(
                directory, duration_seconds=1, profile="ordinary"
            )
            diagnostics.set_matched_sink_write_p95(1.0)
            diagnostics.mark_first_frame()
            for _ in range(10):
                clock.advance(0.1)
                diagnostics.record_timing("effect", 0.06)
                diagnostics.record_timing("ansi", 0.001)
                diagnostics.record_timing("terminal_write", 0.03)
                diagnostics.record_timing("decoder_read", 0.25)
                diagnostics.record_frame(
                    dropped=2, lateness_seconds=0.2, loop_seconds=0.2
                )
            diagnostics.increment("reconnects")
            diagnostics.record_cleanup(0.1)
            report = diagnostics.finalize("duration")
            codes = {item["code"] for item in report["diagnoses"]}
            self.assertIn("terminal_backpressure", codes)
            self.assertIn("decoder_or_network_stall", codes)
            self.assertIn("sustained_overload", codes)
            for item in report["diagnoses"]:
                self.assertTrue(item["remediation"])
                self.assertIn("observed", item)
                self.assertIn("threshold", item)

    def test_style_cost_is_included_in_visual_pipeline_diagnosis(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, clock, _ = self.make_diagnostics(
                directory, duration_seconds=1, profile="ordinary"
            )
            diagnostics.mark_first_frame()
            for _ in range(10):
                clock.advance(0.1)
                diagnostics.record_timing("style", 0.06)
                diagnostics.record_timing("effect", 0.001)
                diagnostics.record_timing("ansi", 0.001)
                diagnostics.record_timing("terminal_write", 0.001)
                diagnostics.record_frame(
                    lateness_seconds=0.0, loop_seconds=0.01
                )
            diagnostics.record_cleanup(0.1)
            report = diagnostics.finalize("duration")
            diagnosis = next(
                item for item in report["diagnoses"]
                if item["code"] == "visual_pipeline_cpu"
            )
            self.assertGreater(
                diagnosis["observed"]["visual_pipeline_p95_ms"], 50.0
            )
            self.assertGreater(
                diagnosis["observed"]["style_p95_ms"], 50.0
            )

    def test_output_integrity_gate_fails_when_player_reports_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, clock, _ = self.make_diagnostics(
                directory, duration_seconds=1, profile="ordinary"
            )
            diagnostics.mark_first_frame()
            clock.advance(1.0)
            diagnostics.record_frame(lateness_seconds=0.0, loop_seconds=0.01)
            diagnostics.increment("output_corruption")
            diagnostics.record_cleanup(0.1)
            report = diagnostics.finalize("duration")
            integrity = report["gates"]["results"]["output_integrity"]
            self.assertEqual(integrity["status"], "fail")
            self.assertEqual(integrity["value"], 1)
            self.assertIn(
                "lifecycle_failure",
                {item["code"] for item in report["diagnoses"]},
            )

    def test_probe_failure_finalizes_without_a_first_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, _, _ = self.make_diagnostics(directory)
            diagnostics.event("probe_failure")
            diagnostics.record_cleanup(0.01)
            report = diagnostics.finalize("error", "SystemExit")
            self.assertFalse(report["status"]["first_frame_presented"])
            self.assertFalse(report["status"]["measurement_complete"])
            codes = {item["code"] for item in report["diagnoses"]}
            self.assertIn("external_source_unavailable", codes)
            self.assertIn("lifecycle_failure", codes)

    def test_constructor_and_finalizer_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text("existing", encoding="utf-8")
            with self.assertRaises(DiagnosticsReportError):
                PlaybackDiagnostics(path)

            path.unlink()
            diagnostics, _, _ = self.make_diagnostics(directory)
            diagnostics.record_cleanup(0.01)
            original_link = os.link

            def collide(source, target):
                Path(target).write_text("racer", encoding="utf-8")
                return original_link(source, target)

            with mock.patch("yt_ascii_diagnostics.os.link", side_effect=collide):
                with self.assertRaises(DiagnosticsReportError):
                    diagnostics.finalize("normal")
            self.assertEqual(path.read_text(encoding="utf-8"), "racer")
            self.assertEqual(
                list(Path(directory).glob(".yt-ascii-diagnostics-*.tmp")), []
            )

    def test_atomic_finalize_is_valid_json_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics, _, _ = self.make_diagnostics(directory)
            diagnostics.mark_first_frame()
            diagnostics.record_frame(lateness_seconds=0, loop_seconds=0)
            diagnostics.record_cleanup(0.01)
            first = diagnostics.finalize("normal")
            second = diagnostics.finalize("error", "IgnoredError")
            self.assertIs(first, second)
            on_disk = json.loads((Path(directory) / "report.json").read_text())
            self.assertEqual(on_disk["status"]["exit_reason"], "normal")

    def test_context_manager_finalizes_error_without_message_leakage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with self.assertRaisesRegex(RuntimeError, "private message"):
                with PlaybackDiagnostics(path) as diagnostics:
                    diagnostics.record_cleanup(0.01)
                    raise RuntimeError("private message")
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"]["error_type"], "RuntimeError")
            self.assertNotIn("private message", json.dumps(report))


class FFmpegBenchmarkTests(unittest.TestCase):
    def test_parser_ignores_all_but_numeric_benchmark_totals(self):
        raw = (
            b"Input #0 from https://signed.invalid/private\n"
            b"bench: utime=1.25s stime=0.50s rtime=2.00s\n"
            b"bench: maxrss=1234kB\n"
        )
        result = parse_ffmpeg_benchmark(raw)
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["user_cpu_seconds"], 1.25)
        self.assertEqual(result["system_cpu_seconds"], 0.5)
        self.assertEqual(result["real_seconds"], 2.0)
        self.assertEqual(result["maximum_rss_bytes"], 1234 * 1024)

    def test_capture_is_consumed_closed_and_raw_text_is_never_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = PlaybackDiagnostics(Path(directory) / "report.json")
            capture = diagnostics.new_ffmpeg_capture()
            capture.stream.write(
                b"https://signed.invalid/private\n"
                b"bench: utime=0.10s stime=0.20s rtime=0.30s\n"
                b"bench: maxrss=99kB\n"
            )
            diagnostics.consume_ffmpeg_capture(capture)
            self.assertTrue(capture.stream.closed)
            diagnostics.record_cleanup(0.01)
            report = diagnostics.finalize("normal")
            self.assertEqual(report["ffmpeg"]["benchmark_samples"], 1)
            self.assertEqual(report["ffmpeg"]["maximum_rss_bytes"], 99 * 1024)
            self.assertNotIn("signed.invalid", json.dumps(report))


if __name__ == "__main__":
    unittest.main()

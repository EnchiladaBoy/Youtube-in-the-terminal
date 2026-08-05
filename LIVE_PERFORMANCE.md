# Short live performance smoke

Live testing is intentionally narrow. The exhaustive style/effect/backend/color
matrix belongs to deterministic unit, integration, and in-memory benchmark
coverage. A user can judge real terminal responsiveness and visual direction
more efficiently than a broad automated live harness.

## Representative profiles

Use one stable, ordinary public video for both profiles. These commands use the
canonical style/effect split:

```sh
# Ordinary: 120x34 at 30 fps
yt-ascii URL --render cells --style edge-glow --effect none \
  --width 120 --height 34 --fps 30 --no-audio \
  --diagnostics-json ordinary-cells.json \
  --diagnostics-warmup 3 --diagnostics-duration 10

# Stress: 240x68 at 60 fps
yt-ascii URL --render half-block --style classic --effect wave \
  --width 240 --height 68 --fps 60 --no-audio \
  --diagnostics-json stress-half-block.json \
  --diagnostics-warmup 3 --diagnostics-duration 10
```

Diagnostic reports are new-file-only. Use a fresh filename for every run.

## What each path can prove

Do not treat the paths as a live Cartesian matrix or assume they are matched
workloads.

| Path | How | Useful evidence | Does not prove |
|---|---|---|---|
| sink | redirect stdout | Decode/scheduling/frame construction without terminal repaint | tmux or terminal repaint |
| tmux | short run in a normal pane | Correct classification and a representative multiplexer sample | matched comparison with sink |
| attached-class PTY | launch under a PTY | Classification, lifecycle, diagnostic finalization, cleanup | real emulator repaint or visual quality |
| real attached terminal | run manually in the target emulator | Responsiveness, sizing, controls, visual stability, screenshots | reproducible cross-machine timing |

A sink example:

```sh
yt-ascii URL --render cells --style edge-glow --effect none \
  --width 120 --height 34 --fps 30 --no-audio \
  --diagnostics-json ordinary-sink.json \
  --diagnostics-warmup 3 --diagnostics-duration 10 > /dev/null
```

On Windows, use a normal PowerShell redirection target instead of `/dev/null`.
The diagnostics report records detected path metadata; do not relabel a sink
or synthetic PTY report as real attached-terminal evidence.

## Efficient manual visual pass

Use one representative scene and an untimed attached playback. Pauses and
controls intentionally distort timing samples, so perform this separately from
the short diagnostic run.

1. Compare `chars`, `cells`, and `half-block` on the same scene.
2. Cycle all 10 styles with a neutral `none` effect; confirm each static
   treatment is visually distinct and none blanks the frame.
3. In colored `cells`, cycle `none`, `pixelate`, `glitch`, `crt`,
   `chromatic-shift`, `wave`, `trails`, and `prism`; repeat representative
   checks in `half-block` and quantized `chars`.
4. In `chars`, check that `digital-rain` and `terminal-hud` are structurally
   different. Confirm they are skipped during colored graphical-backend
   cycling and rejected when explicitly incompatible.
5. Confirm `--no-color --render cells` and
   `--no-color --render half-block` show visible ASCII, not blank spaces.
6. Exercise pause, seek, resize, style/effect cycling, scatter/rain reveal, and
   quit. Confirm `trails` resets at playback boundaries.
7. Confirm cursor/colors and decoder/audio child processes are restored.
8. After approving the visual direction, capture fresh screenshots from a real
   attached terminal for the three canonical renderers. Do not use a captured
   PTY byte stream as a screenshot substitute.

This manual pass replaces the oversized automated live qualification harness
that was explored earlier. Useful bounded diagnostics and classification
checks remain; exhaustive live combinations do not.

## Pass/fail thresholds

An ordinary diagnostic sample requires at least 98% of target fps, at most 1%
drops, p95 lateness within one frame, p99 within 1.5 frames, and no freeze over
250 ms. Stress characterization requires at least 95% of target fps, at most
5% drops, lateness within two frames, and no freeze over 500 ms.

A failure must be diagnosed as style/effect/ANSI CPU, terminal writes,
decoder/probe work, scheduling, resizing, or cleanup. Do not weaken a gate to
make a smoke pass. Network availability and terminal implementations make live
results inherently less reproducible than the deterministic matrix.

## Current representative evidence

On 2026-08-05 the public 320x240 “Me at the zoo” fixture completed four fresh,
four-second measured runs of the finalized architecture on Linux aarch64. All
reported zero hard failures, zero drops, a normal duration exit, restored
terminal state, and reaped child processes:

| Detected path/profile | Canonical selection | Presented | p95 / p99 late | Max freeze | Result |
|---|---|---:|---:|---:|---|
| sink / ordinary | cells; `edge-glow` + `none` | 30.0 fps | 4 / 8 ms | 5.71 ms | pass |
| sink / stress | half-block; `riso` + `wave` | 59.75 fps | 8 / 10 ms | 6.57 ms | pass |
| tmux / ordinary | cells; `ordered-dither` + `prism` | 30.0 fps | 10 / 12 ms | 8.67 ms | pass |
| attached-class PTY / ordinary | cells; `posterize` + `glitch` | 30.0 fps | 6 / 8 ms | 6.79 ms | pass |

The rows intentionally use different selections, so the timings are not a
matched sink/tmux/PTY comparison. The PTY row verifies attached classification
and cleanup only; it is not evidence of real terminal repaint.

These numbers are useful stability/classification evidence, not full live
qualification of the 10-style/10-effect matrix. A manual real-terminal visual
pass and screenshot capture remain pending review.

# Renderer performance and qualification

This document defines the post-pivot performance contract. Measurements from
the former glyph-heavy effect matrix do not qualify v0.5.0.

## Workload profiles

| Profile | Terminal cells | Target | Frame budget | Purpose |
|---|---:|---:|---:|---|
| ordinary | 120x34 | 30 fps | 33.33 ms | Normal playback must remain comfortably real-time |
| stress | 240x68 | 60 fps | 16.67 ms | Large/high-rate regression and characterization |

For `half-block`, the decoded/style/effect RGB field is twice the listed
height. Terminal output remains at the listed cell geometry.

## Deterministic in-memory matrix

The benchmark exhaustively composes the finalized architecture across:

| Dimension | Values |
|---|---|
| Style | all 10 canonical styles |
| Effect | all 10 canonical effects, filtered by backend capability |
| Requested backend/color path | chars color; chars grayscale; cells color; cells no-color fallback; half-block color; half-block no-color fallback |
| Profile | ordinary; stress |

The two no-color graphical requests have effective backend `chars`. This
creates four effective-character paths, where all 10 effects apply, and two
colored graphical paths, where the two text-specific effects are rejected.
Consequently each profile contains 560 applicable composed cases and 40
explicit incompatible cases:

```text
10 styles x ((4 character paths x 10 effects) +
             (2 graphical paths x 8 effects)) = 560
```

The matrix also benchmarks every style and effect in isolation and measures
direct renderer baselines. Registry coverage is derived from capability
metadata, so adding a future RGB backend does not require a hard-coded effect
list.

This benchmark is deliberately **in-memory**: it constructs complete terminal
frames but does not write them to a terminal, tmux pane, or redirected stdout.
It therefore gives deterministic CPU, byte-contract, and coverage evidence—not
output-path qualification. No execution-path metadata label can turn an
in-memory result into terminal evidence.

## Output-path evidence

Live evidence is a separate, deliberately small layer:

| Path | Evidence | Scope |
|---|---|---|
| sink | short redirected live playback | Decoder, style/effect, scheduling, and frame construction without repaint |
| tmux | short representative pane smoke | Classification and multiplexer/backpressure sample |
| attached-class PTY | short representative PTY smoke | Classification, lifecycle, and cleanup—not real terminal repaint |
| real attached terminal | manual playback | Repaint, responsiveness, width behavior, visual stability, and screenshots |

The short runs are classification samples, not matched path comparisons: they
may use different style/effect selections and do not establish that a tmux or
attached run has the same workload as a paired sink baseline. A broad live
Cartesian harness is intentionally out of scope; manual testing is faster and
more useful for visual direction. See
[`LIVE_PERFORMANCE.md`](LIVE_PERFORMANCE.md).

## CPU gates

The pivot retains strict local CPU gates instead of substituting the much
looser 30/60 fps deadlines:

| Stress-size measurement | Gate |
|---|---:|
| Character or cell ANSI composition | below 2.0 ms median |
| Half-block ANSI composition | below 3.0 ms median |
| Static style in isolation | below 2.0 ms median |
| Stateless/motion effect in isolation | below 2.0 ms median |
| `trails` in isolation | below 2.5 ms median |
| Style + effect + ANSI composition | below 6.0 ms median |
| Default `chars/color` `classic` + `none` overhead versus direct composition | no more than +3% median in ordinary and stress |
| Stress `half-block/color` `classic` + `none` overhead versus direct composition | no more than +3% median |

Ordinary cases must also fit their 33.33 ms frame budget. A short ordinary live
diagnostic requires at least 98% of target fps, no more than 1% drops, p95
lateness no greater than one frame, p99 no greater than 1.5 frames, and no
freeze over 250 ms. Stress live characterization uses at least 95% of target
fps, no more than 5% drops, lateness no greater than two frames, and no freeze
over 500 ms.

Timing noise can destabilize one `classic`/`none` ratio because both medians
are sub-millisecond. The benchmark therefore uses 300 calls per paired group
for this gate. Rerun and diagnose the raw values; do not weaken the 3% gate.

## Run the matrix

From an environment with `requirements.txt` installed:

```sh
python benchmarks/benchmark_renderer.py --profile ordinary --check-budgets
python benchmarks/benchmark_renderer.py --profile stress --check-budgets
```

Machine-readable evidence:

```sh
python benchmarks/benchmark_renderer.py --profile ordinary --json > ordinary.json
python benchmarks/benchmark_renderer.py --profile stress --json > stress.json
```

The ordinary command is the CI-friendly performance check. Stress is a local
release-review check because shared runners vary too much for stable
sub-millisecond comparisons.

## Current working-tree evidence

The earlier report of 80 composed cases was recorded before static treatments
were separated from structural effects. It is obsolete and intentionally not
carried forward as qualification evidence.

Fresh runs on 2026-08-05 used Python 3.14.6, NumPy 2.5.1, and Linux aarch64.
Both profiles passed every deterministic contract and applicable gate:

| Profile | Applicable / incompatible | Slowest renderer | Slowest style | Slowest effect | Slowest composition | Default `chars/color` overhead | Stress half-block overhead |
|---|---:|---:|---:|---:|---:|---:|---:|
| ordinary 120x34@30 | 560 / 40 | 0.375 ms | 0.265 ms | 0.336 ms | 0.976 ms | +1.49% | not gated |
| stress 240x68@60 | 560 / 40 | 1.567 ms | 1.144 ms | 1.178 ms | 3.566 ms | +0.89% | -0.01% |

The stress maxima were `half-block`, `riso`, `prism`, and
`half-block/color/error-diffusion/prism`, respectively. The complete unit/integration run
also passed 249 tests and the offline self-test passed. These results belong to
the uncommitted review candidate; they do not authorize a release, push, or
tag. A second full ordinary run reproduced the default-path overhead at
+1.33%, with no gate failures.

## What the deterministic benchmark proves

For every applicable composition it verifies:

- the style and effect results preserve `uint8` RGB geometry;
- a structured text plane occurs only for a text-specific effective-`chars`
  case;
- output is nonempty and contains no NUL padding;
- default `chars` and colored `cells` output decode as ASCII;
- `cells` contains background-color sequences and spaces, with no decorative
  glyph, foreground-only character payload, or half-block;
- non-identity styles materially alter the fixture;
- graphical effects materially alter the styled baseline;
- retained deterministic outputs have distinct contract signatures;
- incompatible renderer/effect pairs are explicitly accounted for;
- byte count and median/worst-group execution time are reported;
- benchmark coverage exactly matches style/effect/backend metadata.

Unit and integration tests separately cover CLI migration, cycling, no-color
resolution, reveal behavior, reset boundaries, installer contents, and live
diagnostics. The benchmark cannot prove subjective visual quality or real
terminal repaint performance.

## Playback performance model

```text
FFmpeg RGB decode -> NumPy style -> NumPy effect -> reusable ANSI byte grid -> write
```

The renderer uses decimal byte lookup tables and reusable `uint8` record
workspaces. Graphical methods operate on RGB/luminance arrays before
composition and avoid Python objects per terminal cell. `classic` and `none`
retain their zero-copy boundaries. `trails` retains one bounded frame history
and resets at defined playback boundaries.

Output bandwidth still matters. Truecolor `chars` and `cells` emit roughly one
color prefix per terminal cell; `half-block` emits foreground and background
colors. In-memory composition isolates CPU construction, while a short live
path smoke can reveal scheduling or write pressure. Only a manual real-terminal
pass can judge actual repaint and perceived stability.

## Regression diagnosis

Do not raise a gate merely because a case fails. Locate the regression in:

1. static style processing;
2. effect processing, including `trails` history;
3. ANSI backend composition;
4. combined style/effect/composition overhead;
5. terminal writes or repaint during a representative live run;
6. decoder/probe work;
7. scheduling, tmux, resize, or cleanup behavior.

Compare ordinary with stress, colored paths with grayscale fallbacks, and
in-memory construction with the relevant representative smoke. Because the
existing path smokes are not matched workloads, treat cross-path timing
differences as diagnostic leads, not causal proof.

## Release evidence

Before v0.5 review, retain the exact commands, Python/NumPy/platform metadata,
ordinary and stress matrix results, unit/integration/self-test totals, and the
short representative live reports. Manual attached-terminal comparison and
fresh screenshots remain explicit review inputs. Deterministic CPU checks do
not replace that visual review.

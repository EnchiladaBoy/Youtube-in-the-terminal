# Performance improvement roadmap

This document began as a source audit and profiling-based plan for `yt-ascii`
at commit `c5dcd45`. It now also records the first implemented optimization
pass. The remaining entries are ordered by implementation disruption, from a
large architectural overhaul to small local changes. They are **not** ordered
by return on engineering time; the recommended next steps are at the end.

All percentages are planning estimates, not promises. They describe the
specific metric named in the table, are workload-dependent, and are
non-additive. Playback is capped by `--fps`, so an optimization at the cap
normally appears as lower CPU use and fewer dropped frames rather than a higher
displayed FPS.

## Implemented first pass

The compatibility-preserving recommendations with the best measured return are
now implemented:

- a reusable NumPy `uint8` ANSI record renderer replaces all fixed-width
  Unicode concatenation, including scatter and rain;
- `--no-color` uses the new record renderer while retaining RGB24 and the exact
  legacy luminance formula;
- late frames are drained through one reusable frame buffer rather than a
  `frame_size * skip` allocation;
- paused children are resumed, terminated together against one deadline, and
  reaped; and
- deterministic differential tests, reveal/pipe/process tests, a renderer
  benchmark, and source plus installer CI smoke tests were added.

The same synthetic playback-loop benchmark used for the baseline produced the
following **parent Python process** CPU times (FFmpeg child CPU is not included):

| Mode at 240 x 68 / 60 fps | Before | After | CPU-time reduction | Equivalent throughput increase |
|---|---:|---:|---:|---:|
| truecolor characters | 5.91 ms/frame | 2.59 ms/frame | 56.2% | +128% (2.28x) |
| truecolor half-blocks | 8.91 ms/frame | 3.46 ms/frame | 61.2% | +158% (2.58x) |
| rain reveal | 6.43 ms/frame | 2.84 ms/frame | 55.8% | +126% (2.26x) |

The isolated deterministic benchmark removes pipe, pacing, process-control and
test-sink overhead. At 240 x 68 it measures about 5.6x renderer throughput for
truecolor, 5.5x for half-blocks, and 7.6x for grayscale composition. Run it with:

```sh
python benchmarks/benchmark_renderer.py --width 240 --height 68
```

Steady truecolor, half-block and grayscale output is byte-for-byte identical to
the old renderer in differential tests.

## v0.4 style pipeline

The v0.4 release adds a NumPy-only transform stage between FFmpeg's RGB frame
reshape and `AnsiRenderer`. Each transform accepts a `uint8` RGB array and
returns the same shape and dtype without mutating the decoded frame. `classic`
is a zero-copy identity path, so the default output and renderer cost remain
unchanged.

The transform operates on the already scaled terminal-resolution frame rather
than the source video. Palette selection, character or half-block encoding, and
the scatter/rain reveal masks remain downstream, which lets every style compose
with every renderer mode without duplicating ANSI construction. Time-dependent
styles derive their phase from the decoded playback timestamp, so pause and
seek behavior is reproducible rather than tied to wall-clock scheduling.

`benchmark_renderer.py` reports each style transform separately at a 240 x 136
RGB input size as well as the existing renderer cases. The local performance
budget is a median below 2 ms per transform; this timing is reported for
comparison and is deliberately not a CI gate because hardware variance would
make that gate flaky. Run the complete benchmark with:

```sh
python benchmarks/benchmark_renderer.py --width 240 --height 68
```

The Bayer, duotone, Riso, contour and glitch styles are independent
implementations of established image-processing techniques. Ladybug supplied
visual inspiration only; its code, shaders, assets and preset values are not
used.

## v0.5 structural-effect pipeline

The v0.5 edge candidate adds a structural stage after RGB styling and before
ANSI composition:

```text
decoded terminal-size RGB → style → structural effect → renderer → reveal
```

An effect may retain the styled RGB frame, replace it, and/or provide a
structured cell plane containing its glyph choices and foreground colors. The
renderer remains responsible for ANSI encoding, color/grayscale policy, row
suffixes, and reveal masks. This avoids a second terminal encoder and lets the
same effect definitions work with both portable ASCII and opt-in single-cell
Unicode glyph schemas. `none` must take the original v0.4 path without copying
the frame or changing its output bytes.

The completed cell/type stage adds `number-field`, `glyph-grid`,
`vector-field`, `word-field`, `inscription`, and `type-echo` through that same
cell-plane contract. They cover luminance deciles, a tone-weighted seeded
lattice, quantized Sobel directions, configurable word density, contour text,
and stateless time-derived type echoes; no new renderer or output path is
involved.

Procedural layouts are derived from an explicit integer seed. Animation uses
decoded video time rather than wall-clock time, and temporal or size-dependent
state resets on seek/jump, resize, style or effect selection, and new media. A
continuous same-source reconnect preserves the effect state and presentation
sequence. Caches must be proportional to terminal cells; effects must not
allocate Python objects per cell on every frame. The initial temporal effect
retains one reusable floating-point trail accumulator and one previous-frame
RGB buffer.

The benchmark reports every effect in two forms:

- **isolated**, measuring only structural processing of one terminal-size RGB
  input; and
- **composed**, measuring style processing, effect processing, and ANSI
  construction together, including structured glyph output.

For reproducibility, composed cases use the `duotone` style, ASCII effect
glyphs, the default `YTASCII` text, a fixed seed, and a requested half-block
presentation. Glyph effects therefore exercise their character fallback while
RGB effects retain the half-block renderer.

The candidate targets at a 240 x 136 RGB input are:

| Target | Budget |
|---|---:|
| `none` path | Byte-identical output and no more than 3% median regression |
| Each stateless effect in isolation | Below 2.0 ms median |
| `afterimage` in isolation | Below 2.5 ms median |
| Style plus effect processing | Below 4.0 ms median |
| Full composed processing and ANSI construction | Below 6.0 ms median |

On 5 August 2026, a 50-round-per-group run on the local four-core Apple AArch64
Linux host with Python 3.14.6 and NumPy 2.5.1 produced these median times. The
composed column includes `duotone`, the named effect, and ANSI construction:

| Effect | Isolated | Composed |
|---|---:|---:|
| `none` | 0.001 ms | 2.141 ms |
| `geometry` | 0.326 ms | 1.709 ms |
| `contour-glyph` | 0.212 ms | 1.580 ms |
| `hatch` | 0.125 ms | 1.489 ms |
| `dotfield` | 0.126 ms | 1.511 ms |
| `tile-mosaic` | 0.309 ms | 2.293 ms |
| `wave-lines` | 0.373 ms | 2.592 ms |
| `voronoi` | 0.439 ms | 2.497 ms |
| `afterimage` | 0.502 ms | 2.626 ms |
| `number-field` | 0.062 ms | 1.442 ms |
| `glyph-grid` | 0.126 ms | 1.491 ms |
| `vector-field` | 0.234 ms | 1.610 ms |
| `word-field` | 0.157 ms | 1.523 ms |
| `inscription` | 0.203 ms | 1.572 ms |
| `type-echo` | 0.254 ms | 1.654 ms |

The matching `duotone` plus half-block renderer control, without constructing
or applying an effect processor, measured 2.128 ms. The `none` path was 0.63%
slower in this run, within the 3% overhead budget. Every effect met the
candidate targets on this host. These results exclude
FFmpeg, terminal writes, terminal parsing, and pacing, and are not guarantees
for other machines. Absolute timings remain informational rather than CI gates
because host variance would make a millisecond threshold flaky; deterministic
output, shape/dtype contracts, bounded state, and legacy-byte equality are CI
gates. Future recorded results must likewise name the hardware and
NumPy/Python versions. The authoritative effect list and promotion rules are
in [`EFFECTS.md`](EFFECTS.md).

## Baseline that motivated the changes

The original hot path converted RGB channels into fixed-width Unicode arrays
and then built ANSI strings with repeated full-grid `np.char.add` operations:

- character mode performed three Unicode conversions and seven concatenation
  passes ([original `cells_chars`](https://github.com/EnchiladaBoy/Youtube-in-the-terminal/blob/c5dcd45a8cd805e6f972d176a234ea76ddb58cf2/yt-ascii#L870-L890));
- half-block mode performed six Unicode conversions and twelve concatenation
  passes ([original `cells_half`](https://github.com/EnchiladaBoy/Youtube-in-the-terminal/blob/c5dcd45a8cd805e6f972d176a234ea76ddb58cf2/yt-ascii#L845-L868)); and
- the result is joined into Python strings row by row before one batched terminal
  write ([original render functions](https://github.com/EnchiladaBoy/Youtube-in-the-terminal/blob/c5dcd45a8cd805e6f972d176a234ea76ddb58cf2/yt-ascii#L899-L903)).

NumPy describes `numpy.char` as legacy fixed-width string functionality and
recommends newer string APIs for new code. More importantly here, fixed-width
Unicode arrays are a poor intermediate representation for a byte-oriented
terminal protocol. See the [NumPy module structure documentation](https://numpy.org/doc/stable/reference/module_structure.html#legacy-namespaces).

A deterministic three-second FFV1 source was fed through the application's real
FFmpeg command and rendered at 60 fps. The table reports parent Python process
CPU, including pipe-call, conversion, construction and encoding CPU but not the
FFmpeg child. Output was captured by a sink, so it also excludes terminal-emulator
parsing and terminal backpressure.

| Mode | Terminal cells | Python CPU/frame | UTF-8 output/frame | Output rate at 60 fps |
|---|---:|---:|---:|---:|
| grayscale | 120 x 34 | 1.95 ms | 4.2 KiB | 0.26 MB/s |
| truecolor characters | 120 x 34 | 2.76 ms | 68.7 KiB | 4.2 MB/s |
| truecolor half-blocks | 120 x 34 | 3.55 ms | 141.1 KiB | 8.7 MB/s |
| truecolor characters | 240 x 68 | 5.91 ms | 273.6 KiB | 16.8 MB/s |
| truecolor half-blocks | 240 x 68 | 8.91 ms | 562.7 KiB | 34.6 MB/s |
| rain reveal | 240 x 68 | 6.43 ms | 39.7 KiB | 2.4 MB/s |

At 240 x 68, `cells_chars` accounts for about 3.1 ms/frame and its Python row
joins another 1.1 ms. `cells_half` accounts for about 5.8 ms/frame and row joins
another 1.4 ms. The profiled pipe read waits about 0.5 ms/frame. This makes ANSI
construction and terminal traffic the first steady-state targets; there is no
evidence that changing FFmpeg's scaler or adding hardware decoding should come
first.

The baseline ran on four-core AArch64 with Python 3.14.6 and NumPy 2.5.1. It
held the requested rate only because output went to a sink. Results must be
repeated on supported Python versions, fresh source installs and real
terminals.

## Ranked changes: most drastic to least

| Rank | Change | Estimated improvement | Where it helps |
|---:|---|---|---|
| 1 | One timestamped media engine | **+5-20% media CPU efficiency**; **45-50% less network traffic** when video and audio fall back to the same muxed URL; **80-95% less A/V drift** | Muxed fallback, seeks, reconnects, long sessions |
| 2 | Optional raster graphics backend | **+50-250% delivered-frame throughput** on large/slow supported terminals; about **79% less uncompressed wire data** for half-block input | `--pixels`, large windows; no gain on unsupported terminals |
| 3 | Native C/Rust ANSI rendering core | **+200-900% renderer throughput**; **+15-60% end-to-end** when rendering is CPU-bound | Wide truecolor and half-block output |
| 4 | Bounded latest-frame decoder worker | **+0-10% steady throughput**, **50-90% lower overloaded latency**, potentially **over 98% lower input latency during network stalls** | Slow terminals, stalls, input responsiveness |
| 5 | Terminal-aware source-format selection | **+10-35% total CPU efficiency**, **30-75% less decode CPU**, **50-90% less video traffic** on small terminals | Constrained CPUs/networks and default terminal sizes |
| 6 | Stateful changed-run renderer | **+10-60% mixed-video throughput**, **+100-900% static/slides**, approximately **0% noisy action** | Low-motion video, paused frames, presentations |
| 7 | Reusable Python/NumPy byte renderer — **implemented** | Measured about **+450% truecolor/half-block renderer throughput**; **+128-158% playback-loop throughput** at 240 x 68 | All ANSI modes; lower-risk alternative to rank 3 |
| 8 | Optional 256-color backend and SGR coalescing | **+15-50% end-to-end terminal throughput**; **37-40% less escape-prefix traffic** before run reuse | Remote sessions and terminals slow at parsing truecolor |
| 9 | Staged yt-dlp probe and expiry-aware reconnect | **40-70% less normal probe work**, **20-55% faster cold start** when probing dominates; **85-98% less expired-URL downtime** | Startup and unreliable/long-lived streams |
| 10 | Optional local `onedir` and slim PyInstaller builds | **50-90% lower local executable launch latency** for `onedir`; **20-60% lower one-file launch latency** for a slim system-FFmpeg build | Local executable startup; no playback FPS gain |
| 11 | Native grayscale FFmpeg path — **evaluated, not shipped** | Direct gray cut pipe traffic **66.7%** but changed 36.4% of fixture glyphs; exact conversion raised FFmpeg child CPU about **85%** | `--no-color` only |
| 12 | Sparse reveal-effect composition — **implemented** | Measured rain playback-loop throughput **+126%** at 240 x 68 | First reveal after start/seek only |
| 13 | Process, buffer and idle-path fixes — **partly implemented** | Bounded allocation and paused teardown implemented; paused refresh throttling remains | Paused control, severe lag, cleanup |
| 14 | Reproducible benchmarks and install caching — **partly implemented** | Tests, benchmark and CI source/installer smoke tests implemented; dependency locking remains | Preventing regressions and shortening iteration time |

### 1. Replace the two media clocks with one timestamped media engine

Today, FFmpeg decodes video while a separate `ffplay` process opens audio
([video process](yt-ascii#L477-L489), [audio process](yt-ascii#L492-L518)).
Playback position is a Python wall clock and the raw-video pipe contains no
timestamps. If yt-dlp chooses the same muxed URL for both tracks, that resource
is fetched twice.

Implement one media session with PyAV/libav, or one FFmpeg demux/decode process
feeding timestamped raw video and PCM audio. Use played audio samples as the
master clock, keep a two-frame video queue, and tag seek/reconnect generations
so stale output cannot cross a restart. This is the cleanest synchronization
model, but it also introduces an audio-device API, native packaging work and
substantial cross-platform testing. On normal split video/audio formats the
network saving is 0%; the synchronization improvements still apply.

### 2. Add an optional raster terminal backend

For terminals that support it, send scaled RGB frames through the Kitty graphics
protocol (and optionally Sixel/iTerm2 later) while preserving ANSI as the
portable fallback. A half-block cell currently emits about 38 bytes on average
but represents six raw RGB bytes, or roughly eight bytes after base64 before
protocol overhead: about 79% less data before compression.

The [Kitty graphics protocol](https://sw.kovidgoyal.net/kitty/graphics-protocol/)
accepts RGB24, zlib-compressed data and, for local clients, files or shared
memory. Detect support once, reuse an image placement, replace frames in place,
and fall back cleanly over SSH or on unsupported terminals. This is a product
choice as well as an optimization because raster output is no longer character
art.

### 3. Move exact ANSI encoding into a native core

Keep Python for argument handling, yt-dlp and process control, but pass each
`uint8` frame to a small C or Rust function that performs RGB-to-luminance,
palette selection and ANSI encoding in one pass into a reusable byte buffer.
Use lookup tables for decimal channel bytes and glyph encodings. Expose one
function for character cells and one for paired half-block cells.

This removes fixed-width Unicode grids, repeated temporary arrays, Python row
joins and the final text-to-UTF-8 conversion. It does not reduce the terminal
payload, so combine it with ranks 6 or 8 if the terminal, rather than Python,
is the ceiling. Prototype rank 7 first; only accept native packaging complexity
if the measured Python implementation misses its frame budget.

### 4. Drain FFmpeg in a bounded latest-frame worker

The implemented bounded drain removes the large catch-up allocation and fixes
partial-EOF accounting, but the UI still reads and renders in one synchronous
loop. Each exact frame read can wait while FFmpeg uses its reconnect budget, so
keys and resize handling may still stall for tens of seconds.

Run the decoder reader in a worker that reads exactly one frame at a time into a
`deque(maxlen=2)`. Publish EOF, error and stall events; let the main loop poll
keys and resize events and render only the newest due frame. Put a generation ID
on worker events so a seek cannot display an old frame. This is primarily a
latency and robustness win, not a steady-state FPS optimization.

### 5. Select source formats for the actual terminal

The resolver chooses a source before terminal dimensions are converted to an
output pixel size. FFmpeg may therefore decode 480p video only to reduce it to a
roughly 80 x 24 character image.

Extract the format list once, then choose a source approximately 1.5-3 times the
rendered pixel height. Prefer source FPS no higher than requested FPS and a
hardware-friendly codec such as AVC where available. Keep `--max-res` as a hard
quality ceiling and add a quality override for large `--pixels` displays. For
`--no-audio` or missing `ffplay`, avoid selecting audio solely for playback.
Quality needs snapshot tests: source detail can matter even after a large
downscale.

### 6. Emit only changed terminal runs when that is cheaper

Keep the previous quantized cell grid. Compare it to the new grid, group changed
cells into contiguous row spans, cursor-position to those spans and emit only
them. Estimate both the delta and full-frame byte cost every frame and use the
cheaper form. Force a full invalidation after resize, seek, mode change and
reveal start.

Truecolor video can differ by a few channel values almost everywhere, so apply
color quantization before comparing and always retain the cost-based full-frame
fallback. Without those safeguards this can add CPU while saving no bytes.

### 7. Replace `np.char` with a reusable byte renderer — implemented

[`AnsiRenderer`](yt_ascii_renderer.py) now precomputes decimal and UTF-8 glyph
lookup tables and fills reusable padded `uint8` records for each terminal cell.
NumPy compacts the non-padding bytes in C order once per frame, and the output
adapter performs one binary write and flush. The exact floating-point luminance
formula remains in place, preserving default character selection.

Randomized differential tests cover channel digit boundaries, built-in and
Unicode palettes, half-blocks and row suffixes. In the final 240 x 68 benchmark,
the isolated renderer moved from 4.88 to 0.87 ms for truecolor and from 8.78 to
1.61 ms for half-blocks. This exceeded the original 2.0/3.0 ms go/no-go target
without a native extension.

### 8. Offer adaptive 256-color output

Add `--color-depth auto|24|256`, quantize RGB to xterm-256 codes, and emit an SGR
sequence only when the code changes. Do the same independently for foreground
and background in half-block mode. A typical 256-color foreground prefix is
about 37-40% shorter than the current truecolor prefix even before adjacent
cells reuse state.

This trades color fidelity for throughput. `auto` should be conservative, and
the benchmark must cover dithering disabled/enabled, SSH, tmux and several
terminal emulators.

### 9. Stage yt-dlp clients and reconnect based on the failure

Every probe currently requests the four-client set `android_vr`, `android`,
`web`, and `ios` ([client list](yt-ascii#L145-L153), [probe options](yt-ascii#L300-L325)).
Trace a representative URL corpus first. If the extractor performs avoidable
fan-out, try the most reliable client, remember the winner for that URL, and add
other clients only when extraction or usable-format selection fails.

Also capture FFmpeg errors in the decoder worker. Retry the same URL briefly for
408/429/5xx, but re-resolve promptly for 401/403/404/410 instead of spending the
full reconnect budget on an expired signed URL. Preserve per-format HTTP headers
from yt-dlp. Worst-case fallback can be slightly slower, so retain the complete
client set and test ordinary, age-restricted, live, DRM/studio and unavailable
videos.

### 10. Build faster-starting local executable variants when needed

The build forces `--onefile --clean` and broadly collects yt-dlp,
imageio-ffmpeg and certifi ([build configuration](packaging/build.py#L38-L56)).
PyInstaller one-file applications unpack support files into a temporary folder
at launch, as described in its [runtime documentation](https://pyinstaller.org/en/stable/runtime-information.html#using-file).

The supported distribution path now installs source into an isolated Python
environment; CI does not build or publish executable artifacts. For users who
need a local executable, compare an archived `onedir` build with the convenient
single executable. A slim local build can require system FFmpeg while a full
build keeps the imageio-ffmpeg fallback. Before replacing `--collect-all
yt_dlp`, inspect the PyInstaller table of contents and run a local executable
against a URL corpus; yt-dlp imports extractors lazily, so aggressive trimming
has compatibility risk.

### 11. Specialize `--no-color` as grayscale end to end — evaluated, not shipped

This optimization failed the combined performance/compatibility gate. Direct
FFmpeg gray output reduced pipe traffic by 66.7%, but source matrix/range
conversion changed 36.4% of dense-palette glyphs in the fixture. An explicit
post-scale RGB `geq` reproduced all 722,160 old luminance bytes exactly, but
increased FFmpeg child CPU by about 85% locally and made total efficiency worse.

The shipped implementation therefore retains RGB24 and applies the exact legacy
formula in the much faster byte-record renderer. A native gray path should only
return if a faster exact FFmpeg filter or an explicit quality-tradeoff flag is
justified by end-to-end measurements.

### 12. Compose reveal effects sparsely — implemented

Scatter now applies its visibility mask directly to byte records. Rain builds
ANSI/glyph records only for active trail coordinates, while settled and blank
cells reuse the same record workspace. Endpoint and Unicode tests ensure both
reveals hand off exactly to the steady renderer. These gains still end when the
reveal does.

### 13. Fix small process, allocation and idle costs together — partly implemented

- The player now sends `SIGCONT` before terminating paused POSIX children,
  terminates video and audio together, waits on one shared deadline, then kills
  and reaps survivors.
- Bulk skip reads now use one reusable `readinto` buffer and a bounded drain
  loop. Complete frames consumed before EOF are counted correctly.
- Refreshing an unchanged paused status on input/resize or about four times per
  second remains a possible minor follow-up.

These are worthwhile correctness and tail-latency fixes. They should not be
presented as a major normal-playback speedup; normal output is already batched
into one write and flush per frame.

### 14. Add measurement and bootstrap guardrails — partly implemented

The renderer is now importable, the standard-library test suite runs in CI, and
the source entry point and source bootstrap receive cross-platform smoke tests.
[`benchmark_renderer.py`](benchmarks/benchmark_renderer.py) records
median/worst-group construction time, bytes/frame and same-process legacy
speedups at configurable dimensions. Tests cover exact/randomized ANSI output,
Unicode palettes, grayscale/half-block shapes, reveal endpoints, bounded short
reads, partial EOF, FFmpeg pixel-format selection, process ordering and
suspended-child teardown.

Still needed are real-terminal output-block measurements, peak RSS tracking,
clean install/update/uninstall timing, a deterministic media fixture checked
into the repository, and a categorized network URL corpus for
resolver/reconnect work.

The workflow now caches pip downloads. Pinning or locking installer dependencies
and making PyInstaller `--clean` opt-in for local incremental builds remain.
These improve the feedback loop but do not improve playback directly.

## Recommended next implementation order

The first go/no-go target was to reduce 240 x 68 character construction below
2.0 ms and half-block construction below 3.0 ms without increasing output. The
implemented renderer reaches about 0.88 and 1.63 ms respectively with identical
steady ANSI bytes, so a native extension is no longer the immediate next step.

For the next risk-adjusted pass:

1. Measure output blocking, dropped frames and input latency in several real
   terminals, SSH and tmux; extend the installed-source self-test to
   deterministic media playback on every supported OS.
2. Add the bounded latest-frame decoder worker if stall/input measurements
   confirm the synchronous pipe is still user-visible.
3. A/B terminal-aware source selection and staged yt-dlp clients against a
   categorized URL corpus.
4. Add 256-color and cost-based changed-run modes behind explicit/auto flags if
   terminal parsing or transport is now the measured ceiling.
5. Measure clean source installation, update and uninstall behavior on every
   supported OS, then lock installer dependencies where practical.
6. Consider a raster protocol or unified media engine only after those results;
   the measured Python renderer no longer justifies a native rewrite by itself.

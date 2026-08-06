# YouTube in the Terminal

`yt-ascii` is a portable terminal visual renderer for YouTube video. ASCII is
one rendering option, not the product boundary: frames can also be composed as
ANSI color cells or higher-resolution Unicode half-block pixels.

It runs on Linux, macOS, and Windows and includes optional audio, pause/seek
controls, static image styles, a small set of structural and motion effects,
and two entrance reveals.

> **v0.5.0 release hold:** this renderer/effect pivot is a working-tree review
> candidate. Do not tag, publish, or promote v0.5.0 until the manual visual
> review is complete. The automated pivot suites are green; stable remains the
> tag recorded in
> [`STABLE_VERSION`](STABLE_VERSION).

## Quick start

Python 3.10 or newer is required. The installer creates a private user-owned
environment and does not require administrator privileges.

Linux and macOS:

```sh
curl -fsSL https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | python3 -
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | py -3 -
```

Then open a new terminal and run:

```sh
yt-ascii https://youtu.be/jNQXAC9IVRw
```

Running `yt-ascii` without a URL opens a paste prompt. Development-channel
installs use `--edge`; they are for reviewing the pivot and are not a v0.5.0
release:

```sh
curl -fsSL https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | python3 - --edge
```

## Rendering model

The playback path has three deliberately separate stages:

```text
decoded RGB frame -> visual style -> motion/structural effect -> render backend
```

- A **render backend** decides how terminal cells are emitted.
- A **visual style** handles static color, contrast, dithering, and image
  treatment.
- An **effect** changes spatial structure, motion, or frame history. The only
  exceptions are two explicitly text-specific terminal reconstructions.

Graphical work happens on NumPy RGB/luminance arrays before terminal
composition. It therefore remains visible when no character glyphs are used.
The boundary also leaves room for future Kitty, Sixel, or iTerm protocol
backends without making any non-portable protocol a requirement.

### Render backends

Choose one with the canonical `--render` option:

| Backend | Output contract | Unicode required | Color |
|---|---|:---:|:---:|
| `chars` | Luminance mapped to characters; ASCII-only by default | No | Optional |
| `cells` | ANSI background colors followed by spaces; no visible glyphs | No | Required |
| `half-block` | Foreground/background colors joined by `▀`, two source rows per cell | Yes | Required |

`cells` is a real graphical backend, not an alias for character rendering.
Its colored frames contain background-color ANSI sequences and literal spaces.

`--pixels` remains a compatibility alias for `--render half-block`. It cannot
be combined with a conflicting `--render` value.

`--no-color` always has visible output. `chars` emits grayscale ASCII;
requested `cells` and `half-block` explicitly resolve to effective `chars`.
The status line and diagnostics record both the requested and effective
backend.

The byte-level contracts are intentionally recognizable:

```text
chars:      ESC[38;2;R;G;Bm<ASCII glyph>
cells:      ESC[48;2;R;G;Bm<space>
half-block: ESC[38;2;topRGBm ESC[48;2;bottomRGBm▀
```

Examples:

```sh
yt-ascii URL --render chars --no-color
yt-ascii URL --render cells --style edge-glow --effect none
yt-ascii URL --render half-block --style posterize --effect wave
yt-ascii URL --pixels --effect prism                 # compatibility alias
```

### Visual styles

Select a static image treatment with `--style NAME`; press `s` to cycle.

| Style | Image treatment |
|---|---|
| `classic` | Unmodified decoded RGB (default) |
| `bayer` | Four-level brightness dither with source hue retained |
| `posterize` | Five-level RGB quantization |
| `contour` | Clean cyan edge map |
| `edge-glow` | Neon contours over a dimmed source frame |
| `ordered-dither` | Four-level ordered quantization per color channel |
| `error-diffusion` | Deterministic serpentine error diffusion |
| `duotone` | Smooth navy-to-gold luminance grade |
| `two-tone` | Hard two-color luminance threshold |
| `riso` | Offset red/blue ink-plate treatment |

Styles are deterministic and renderer-independent. They do not own timestamps
or frame history.

### Motion and structural effects

Select an effect with `--effect NAME`; press `e` to cycle through effects
compatible with the effective renderer.

| Effect | Category | Distinct result |
|---|---|---|
| `none` | passthrough | No structural effect; zero-copy effect path |
| `pixelate` | spatial | Averaged color tiles |
| `glitch` | motion/structural | Animated row displacement, channel separation, and scanlines |
| `crt` | display simulation | Scanlines, vignette, glow, and shimmer |
| `chromatic-shift` | structural | Independently displaced color channels |
| `wave` | motion | Time-driven geometric row displacement |
| `trails` | temporal | Bounded frame-history afterimage |
| `prism` | structural | Multi-offset RGB channel splitting |
| `digital-rain` | text-specific | Moving luminance-gated heads and vertical trails |
| `terminal-hud` | text-specific | Borders, reticles, ticks, labels, and readouts |

The eight entries from `none` through `prism` produce RGB frames and work with
every renderer. `chars` quantizes them to its luminance palette; `cells` and
`half-block` preserve graphical color output. None is implemented by swapping
decorative Unicode symbols.

`digital-rain` and `terminal-hud` are retained because they reconstruct
materially different animated/interface cell layouts. They are not alternate
glyph palettes.

| Effect family | `chars` | `cells` | `half-block` |
|---|:---:|:---:|:---:|
| `none` plus graphical effects | Yes, quantized | Yes | Yes |
| `digital-rain`, `terminal-hud` | Yes | Rejected | Rejected |

Text-specific incompatibilities are explicit errors. Compatible-only cycling
prevents blank or visually unchanged selections. If `--no-color` changes a
requested graphical backend to effective `chars`, compatibility is evaluated
against that visible fallback.

Canonical examples:

```sh
yt-ascii URL --render cells --effect pixelate
yt-ascii URL --render cells --style ordered-dither --effect crt
yt-ascii URL --render cells --style posterize --effect pixelate
yt-ascii URL --render half-block --style duotone --effect chromatic-shift
yt-ascii URL --render chars --effect digital-rain
yt-ascii URL --render chars --effect terminal-hud --effect-text LIVE
```

Static treatments use `--style`; spatial, motion, temporal, and text-specific
changes use `--effect`.

Four historical effect names remain input aliases and do not appear during
cycling:

| Compatibility name | Canonical effect |
|---|---|
| `tile-mosaic` | `pixelate` |
| `wave-lines` | `wave` |
| `afterimage` | `trails` |
| `hologram` | `crt` |

The parser provides narrow migration shims for options that changed category:

| Legacy selection | Canonical interpretation |
|---|---|
| `--style glitch` | `--style classic --effect glitch` |
| `--effect posterize` | `--style posterize --effect none` |
| `--effect edge-glow` | `--style edge-glow --effect none` |
| `--effect ordered-dither` | `--style ordered-dither --effect none` |
| `--effect error-diffusion` | `--style error-diffusion --effect none` |
| `--effect duotone` | `--style two-tone --effect none` |
| `--effect poster-press` | `--style posterize --effect none` |

These parser-only shims never enter canonical registries or cycling. They apply
only when unambiguous; combining one with a conflicting explicit style/effect
is rejected rather than silently discarding a choice. In canonical commands,
use the right-hand spellings. `--style duotone` remains the smooth grade and
animated glitch is `--effect glitch`.

The redundant text-heavy effects `contour-glyph`, `number-field`,
`glyph-grid`, `word-field`, `inscription`, `type-echo`, and `type-collage` are
retired without aliases.

Effect options are `--effect-speed`, `--effect-seed`, and—for
`terminal-hud`—`--effect-text`. `--effect-glyphs ascii|unicode` affects only
the text-specific effects; ASCII is the portable default.

See [`EFFECTS.md`](EFFECTS.md) for the exact registry contract and
[`ROADMAP.md`](ROADMAP.md) for release-review status.

### Controls and reveals

| Key | Action |
|---|---|
| `space` | pause / resume |
| `p` | cycle the active character palette |
| `s` | cycle visual style |
| `e` | cycle compatible effect |
| `←` / `→` | seek -5s / +5s |
| `↓` / `↑` | seek -30s / +30s |
| `0`–`9` | jump to 0%–90% |
| `q` / `Ctrl-C` | quit |

`--scatter` and `--rain` are one-shot entrance reveals, not persistent
effects. They restart after a seek. In `cells`, both reveals remain
space/background-color output and introduce no decorative glyphs.

| Area | Options |
|---|---|
| Renderer | `--render`, `--pixels`, `--no-color`, `--palette`, `--chars` |
| Image processing | `--style`, `--effect`, `--effect-speed`, `--effect-seed` |
| Text-specific effects | `--effect-glyphs`, `--effect-text` |
| Reveals | `--scatter`, `--rain`, `--scatter-secs`, `--rain-secs`, `--rain-chars` |
| Playback | `--fps`, `--width`, `--height`, `--max-res`, `--no-audio`, `--8bit` |

`--palette` and `--chars` affect character rendering and grayscale fallbacks.
When the effective renderer is `chars`, press `p` to cycle
`simple → dense → blocks → binary → numbers → symbols → matrix → simple`.
Colored `cells`/`half-block` and text-specific effects ignore the key because
their output does not consume the luminance palette. A requested graphical
backend that falls back to `chars` under `--no-color` does support it. A custom
`--chars` selection is labelled `custom`; its first `p` selects `simple`, then
cycling continues through the built-ins and persists for the next interactively
selected video. The default character palette and rain reveal are portable
ASCII; `blocks` is an opt-in Unicode built-in.

## Visual review and screenshots

The command examples above replace the former glyph-heavy effect examples.
Fresh real-terminal screenshots are intentionally pending the manual visual
direction review; a captured PTY stream is not presented as a terminal
screenshot. The short review procedure is in
[`LIVE_PERFORMANCE.md`](LIVE_PERFORMANCE.md).

## Live diagnostics

Opt-in diagnostics can stop after a measured interval and write one aggregate
JSON report:

```sh
yt-ascii URL --render cells --style edge-glow --effect none \
  --diagnostics-json cells.json \
  --diagnostics-warmup 5 --diagnostics-duration 60
```

Reports include requested/effective renderer, effect, style, color path,
ordinary/stress profile, sink/tmux/attached classification, frame timing,
drops, resource trends, and cleanup evidence. The player refuses to overwrite
a report and excludes URLs, titles, command lines, raw decoder output,
keystrokes, effect text, and frame contents.

The exhaustive deterministic matrix is in-memory. Short sink, tmux, and
attached-class PTY runs provide representative path-classification evidence;
they are not matched output-path qualification. Real-terminal responsiveness,
visual judgement, and screenshot capture remain manual user checks. See
[`PERFORMANCE.md`](PERFORMANCE.md) and
[`LIVE_PERFORMANCE.md`](LIVE_PERFORMANCE.md).

## Requirements and troubleshooting

The managed environment contains NumPy, yt-dlp, imageio-ffmpeg, and certifi.
Video decoding checks `YTASCII_FFMPEG`, `ffmpeg` on `PATH`, then the bundled
imageio-ffmpeg fallback. Audio is optional and uses `YTASCII_FFPLAY` or
`ffplay`; `--8bit` has no effect with `--no-audio`.

On some Linux systems the static FFmpeg fallback cannot resolve DNS. Install
your distribution FFmpeg package if stream resolution succeeds but decoding
does not start.

## Install channels and updates

Managed stable and edge installations support:

```sh
yt-ascii --check-update
yt-ascii --update
```

Stable follows `STABLE_VERSION`; edge follows `main` when `EDGE_BUILD`
increases; pinned tags and local source installs do not move implicitly.
Playback may perform a silent best-effort update check but never installs an
update automatically. Use `--no-update-check` or
`YTASCII_NO_UPDATE_CHECK=1` to disable automatic checks.

| Goal | Installer argument |
|---|---|
| Stable install/reinstall | none |
| Pin a published tag | `--version v0.4.0` |
| Follow development | `--edge` |
| Do not change `PATH` | `--no-modify-path` |

The project installs tagged/source archives, not GitHub Release executables.
Maintainers must increment `EDGE_BUILD` with an installable edge change and
must change `STABLE_VERSION` only when promoting an existing reviewed tag.
This pivot intentionally does neither before review.

Default locations:

| Platform | Application data | Launcher |
|---|---|---|
| Linux/macOS | `${XDG_DATA_HOME:-$HOME/.local/share}/yt-ascii` | `$HOME/.local/bin/yt-ascii` |
| Windows | `%LOCALAPPDATA%\Programs\yt-ascii` | `%LOCALAPPDATA%\Programs\yt-ascii\bin\yt-ascii.cmd` |

Uninstall with the same installer command plus `--uninstall`.

## Run from source

```sh
git clone https://github.com/EnchiladaBoy/Youtube-in-the-terminal.git
cd Youtube-in-the-terminal
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python yt-ascii URL --render cells --style edge-glow --effect none
```

On Windows, create the environment with `py -3 -m venv .venv` and activate
`.\.venv\Scripts\Activate.ps1`.

## Build and test

PyInstaller is available for local builds; it cannot cross-compile and the
project does not publish executable artifacts:

```sh
python -m pip install -r requirements-build.txt
python packaging/build.py
```

Run the complete local checks with:

```sh
python -m unittest discover -s tests -v
python yt-ascii --self-test
python benchmarks/benchmark_renderer.py --profile ordinary --check-budgets
python benchmarks/benchmark_renderer.py --profile stress --check-budgets
```

The benchmark matrix and gates are documented in
[`PERFORMANCE.md`](PERFORMANCE.md).

## Author

EnchiladaBoy

# YouTube in the Terminal

Watch YouTube videos as terminal art on Linux, macOS, and Windows. The
`yt-ascii` command provides:

- ASCII rendering in 24-bit color or grayscale, plus color half-block pixels;
- six live video styles, structural terminal effects, and two animated reveals;
- optional audio and an 8-bit audio mode; and
- pause, seek, and percentage-jump controls during playback.

## Quick start

The installer requires Python 3.10 or newer and an internet connection. It
installs tagged project source and dependencies into a private, user-owned
environment; it does not need administrator privileges.

Linux and macOS:

```sh
curl -fsSL https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | python3 -
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | py -3 -
```

If the Windows Python launcher is unavailable, replace `py -3 -` with
`python -`.

Open a new terminal after installation so the updated `PATH` is available,
then play a video. The installer also prints the launcher's absolute path for
immediate use:

```sh
yt-ascii https://youtu.be/jNQXAC9IVRw
```

Run `yt-ascii` without a URL to open an interactive prompt where you can paste
links and play multiple videos. The default installer uses the stable source
tag recorded in [`STABLE_VERSION`](STABLE_VERSION), not a mutable branch or a
GitHub Release asset.

The v0.5.0 structural-effect candidate is currently available from the
development channel while the default installer remains on stable v0.4.0:

```sh
curl -fsSL https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | python3 - --edge
```

```powershell
irm https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | py -3 - --edge
```

## Usage

### Controls

| Key | Action |
|---|---|
| `space` | pause / resume |
| `s` | cycle video style |
| `e` | cycle structural effect |
| `←` / `→` | seek -5s / +5s |
| `↓` / `↑` | seek -30s / +30s |
| `0`–`9` | jump to 0%–90% |
| `q` / `Ctrl-C` | quit |

### Video styles

Choose a starting style with `--style NAME`, then press `s` during playback to
cycle without restarting the video:

| Style | Effect |
|---|---|
| `classic` | Original frame with no style transform (default) |
| `bayer` | Four-level ordered dithering with source hue retained |
| `duotone` | Navy-to-gold luminance mapping |
| `riso` | Offset red and blue ink plates with purple overlaps |
| `contour` | Bright cyan edge lines on black |
| `glitch` | Animated RGB separation, scanlines, and horizontal displacement |

Styles work with character and `--pixels` rendering, color and `--no-color`,
and both reveal effects. A style selected with `s` remains active for later
videos in the same interactive session. Glitch animation freezes while paused
and is deterministic after seeking.

```sh
yt-ascii https://youtu.be/jNQXAC9IVRw --style bayer
yt-ascii https://youtu.be/jNQXAC9IVRw --style riso --palette symbols
yt-ascii https://youtu.be/jNQXAC9IVRw --style contour --pixels --rain
yt-ascii https://youtu.be/jNQXAC9IVRw --style glitch --palette matrix
```

These styles are independent implementations of established image-processing
techniques. `ladybug.app` provided visual inspiration; no Ladybug code,
shaders, assets, or presets are included.

### Structural effects

Styles recolor or transform the RGB picture. Structural effects rebuild its
terminal cells using glyphs, lines, dots, regions, or frame history. Select one
with `--effect NAME`; `none` preserves the normal style-and-renderer path.
Press `e` during playback to cycle through this fixed order:

| Effect | Terminal treatment |
|---|---|
| `none` | No structural effect (default) |
| `geometry` | Maps tone bands to geometric cells |
| `contour-glyph` | Draws detected contours with directional glyphs |
| `hatch` | Builds light and shade from directional hatch marks |
| `dotfield` | Represents tone with a deterministic field of dots |
| `tile-mosaic` | Reconstructs the frame from averaged rectangular tiles |
| `wave-lines` | Draws an animated wave-line field bent by image tone |
| `voronoi` | Samples the picture into seeded Voronoi regions |
| `afterimage` | Retains a fading history of motion between frames |
| `number-field` | Labels every cell with its luminance decile from `0` to `9` |
| `glyph-grid` | Rebuilds the image as a seed-shifted light/heavy cell lattice |
| `vector-field` | Points directional marks toward increasing image brightness |
| `word-field` | Repeats a phrase with luminance-controlled text density |
| `inscription` | Writes a decorated phrase along detected image contours |
| `type-echo` | Layers deterministic time-offset echoes of a phrase |

The shared controls are:

| Option | Meaning |
|---|---|
| `--effect-glyphs ascii|unicode` | Use portable ASCII glyphs (default) or richer Unicode |
| `--effect-speed N` | Positive finite animation-speed multiplier (default `1.0`) |
| `--effect-seed N` | Integer seed for reproducible procedural layouts (default `0`) |
| `--effect-text TEXT` | Phrase used by text effects (default `YTASCII`) |

Static effects ignore the speed multiplier, and effects without a procedural
layout ignore the seed. Effects other than `word-field`, `inscription`, and
`type-echo` ignore `--effect-text`.

Effect selection composes with `--style`: the style transforms RGB first, then
the structural effect interprets that styled frame. Seeds and video timestamps
make procedural and animated output repeatable after seeking. An effect chosen
with `e` remains active for later videos in the same interactive session, as
does the configured effect text.

`geometry`, `contour-glyph`, `hatch`, `dotfield`, `number-field`, `glyph-grid`,
`vector-field`, `word-field`, `inscription`, and `type-echo` produce
character-cell structures. If `--pixels` is active, those ten effects
temporarily fall back to character rendering and the status line shows
`pixels→chars`; cycling to `none` or an RGB-only effect
restores half-block pixels. `--effect-glyphs` selects the schema for those
glyph effects. `tile-mosaic`, `wave-lines`, `voronoi`, and `afterimage`
transform RGB and therefore retain pixel mode and the normal `--palette` /
`--chars` behavior.

`word-field` repeats the configured phrase with a seeded row stagger and uses
image luminance as its text density: black stays blank and white shows the
complete field. ASCII rows repeat `TEXT + ". "`; Unicode rows use
`TEXT + "· "`. `inscription` writes `[TEXT] ` or `‹TEXT› ` across detected
contours in row-major order. `type-echo` repeats `TEXT + ": "` or `TEXT + "∶ "`
and analytically derives up to three faded row-band echoes from
`floor(video_time × speed × 6)`. It is stateless, so seeking to the same time
reconstructs the same output and pausing freezes it.

`--effect-text` is preserved exactly, without Unicode normalization, and may
contain 1 to 253 code points, including at least one non-space character.
ASCII glyph mode accepts portable printable ASCII. Unicode mode accepts
printable, left-to-right single-cell characters and ordinary spaces but rejects
control characters, combining or decomposed sequences, and East Asian wide or
full-width characters with a clear error. Some accepted symbols have
locale-dependent ambiguous width; use the default ASCII mode if columns drift
in a CJK-width terminal configuration.

```sh
yt-ascii https://youtu.be/jNQXAC9IVRw --effect geometry
yt-ascii https://youtu.be/jNQXAC9IVRw --style duotone --effect hatch
yt-ascii https://youtu.be/jNQXAC9IVRw --effect voronoi --effect-seed 42
yt-ascii https://youtu.be/jNQXAC9IVRw --effect wave-lines --effect-speed 1.5
yt-ascii https://youtu.be/jNQXAC9IVRw --effect number-field
yt-ascii https://youtu.be/jNQXAC9IVRw --effect glyph-grid --effect-seed 5
yt-ascii https://youtu.be/jNQXAC9IVRw --effect vector-field --effect-glyphs unicode
yt-ascii https://youtu.be/jNQXAC9IVRw --effect word-field --effect-text TERMINAL --effect-seed 5
yt-ascii https://youtu.be/jNQXAC9IVRw --effect inscription --effect-text "HELLO WORLD" --effect-glyphs unicode
yt-ascii https://youtu.be/jNQXAC9IVRw --effect type-echo --effect-text AFTERIMAGE --effect-speed 1.5
```

These effects are independent, terminal-native implementations of established
generative-art techniques. They do not copy another application's code,
shaders, assets, presets, or control recipes. See [`EFFECTS.md`](EFFECTS.md)
for compatibility and the living implementation roadmap.

### Reveals

`--scatter` and `--rain` are entrance animations, not persistent structural
effects. They uncover the composed live picture once at playback start and
restart after a seek. The flags are mutually exclusive, and their timing and
rain glyphs are controlled by `--scatter-secs`, `--rain-secs`, and
`--rain-chars`.

### Palettes and other options

Built-in character palettes are `simple`, `dense`, `blocks`, `binary`,
`numbers`, `symbols`, and `matrix`. A non-empty `--chars` string overrides the
selected palette.

| Area | Options |
|---|---|
| Display | `--no-color`, `--pixels`, `--palette`, `--chars`, `--style` |
| Structural effects | `--effect`, `--effect-glyphs`, `--effect-speed`, `--effect-seed`, `--effect-text` |
| Reveals | `--scatter`, `--rain`, `--scatter-secs`, `--rain-secs`, `--rain-chars` |
| Playback size/rate | `--fps`, `--width`, `--height`, `--max-res` |
| Audio | `--no-audio`, `--8bit` |

`--pixels` is ignored with `--no-color`. Because pixel mode renders colored
half-blocks instead of characters, `--palette` and `--chars` do not affect it.
Custom `--rain-chars` should use single-cell-width glyphs so columns stay
aligned. Run `yt-ascii --help` for full option descriptions and presets.

## Requirements and troubleshooting

The private environment contains NumPy, yt-dlp, imageio-ffmpeg, and certifi.
For video decoding, the player checks `YTASCII_FFMPEG`, then `ffmpeg` on
`PATH`, then its packaged imageio-ffmpeg fallback.

On some Linux systems the fallback's static FFmpeg build cannot resolve DNS.
If video does not start, install your distribution's FFmpeg package, for
example `sudo apt install ffmpeg` on Debian or Ubuntu, or the equivalent for
your distribution.

Audio is optional and needs `ffplay`, selected through `YTASCII_FFPLAY` or
found on `PATH`; playback is silent when it is unavailable. `--8bit` has no
effect with `--no-audio`.

## Install channels and maintenance

### In-app updates

Installer-managed stable and edge installations can check for and install an
update without returning to the copy-paste installer command:

```sh
yt-ascii --check-update
yt-ascii --update
```

`--check-update` waits for the channel check and reports whether the installed
version is current. `--update` installs an available update immediately and
preserves the installation's recorded channel:

| Installed channel | In-app update behavior |
|---|---|
| Stable | Follows the tagged version named by `STABLE_VERSION` |
| Edge | Follows development `main` when its edge build number increases |
| Pinned tag | Refuses to move the pin and explains how to choose a new version |
| Local source install | Refuses and points back to its original checkout |

Normal URL playback and the no-URL paste prompt also start a best-effort update
check in the background. If it finds a newer build before terminal playback
begins, it prints a notice suggesting `yt-ascii --update`; it never installs an
update automatically or delays playback to wait for the result. Automatic
checks stay silent when the installation is current or the network, server, or
response is unavailable. Explicit `--check-update` and `--update` commands
instead report failures and return a nonzero status.

Use `--no-update-check` to suppress the automatic check for one launch. Set
`YTASCII_NO_UPDATE_CHECK=1` in your shell profile or user environment to keep
automatic checks disabled across launches. These opt-outs do not disable the
two explicit update commands.

Updates use the same GitHub source archives as the installer; the project does
not publish or download GitHub Release assets. Installations created before
the updater—including v0.4.0 stable and earlier v0.5.0 edge builds—need the
external copy-paste installer once to bootstrap onto a build that includes it.
Today that is the v0.5.0 edge candidate shown in Quick start; stable remains
v0.4.0 until v0.5 is promoted.

### Installer channels

Append an installer argument to either quick-start command to choose a channel
or change installation behavior:

| Goal | Argument | Behavior |
|---|---|---|
| Install/reinstall stable | none | Uses the tag in `STABLE_VERSION` |
| Pin a source tag | `--version v0.4.0` | Installs that exact source tag |
| Follow development | `--edge` | Installs mutable development `main` |
| Leave `PATH` unchanged | `--no-modify-path` | Skips `PATH` changes; still prints the launcher path |

Rerun the same command to reinstall or change channels when needed. Stable and
edge installations can normally use `yt-ascii --update` instead.
Installer-supported tags begin at `v0.3.0`. Run `yt-ascii --version` to see
both the application version and installed source reference.

Maintainer note: `EDGE_BUILD` is the monotonic update marker for `main`.
Increment it in the same commit whenever the installable edge application or
installer changes; never reuse or decrease a published value. Change
`STABLE_VERSION` only when promoting an existing version tag to stable. The
two-line `current` pointer and v1 launcher templates are also update-protocol
contracts: keep their bytes compatible, or make a new updater accept and
migrate every previously published form before changing them.

Default locations:

| Platform | Application data | Launcher |
|---|---|---|
| Linux/macOS | `${XDG_DATA_HOME:-$HOME/.local/share}/yt-ascii` | `$HOME/.local/bin/yt-ascii` |
| Windows | `%LOCALAPPDATA%\Programs\yt-ascii` | `%LOCALAPPDATA%\Programs\yt-ascii\bin\yt-ascii.cmd` |

Uninstall on Linux or macOS:

```sh
curl -fsSL https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | python3 - --uninstall
```

Uninstall from Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | py -3 - --uninstall
```

Uninstall removes only installer-managed versions, state, launcher, and its
managed `PATH` entry. It leaves unrelated files and user configuration alone.

<details>
<summary>Inspect the installer before running it</summary>

Piping downloaded Python into an interpreter executes remote code. To inspect
the installer first, download and run a saved copy.

Linux and macOS:

```sh
curl -fsSLo install.py https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py
less install.py
python3 install.py
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py -OutFile install.py
Get-Content .\install.py
py -3 .\install.py
```

Replacing `main` in the download URL with a tag or full commit ID pins the
installer code only. To pin the application source too, use a version tag in
the URL and pass the same tag with `--version`.

</details>

## Run from source

```sh
git clone https://github.com/EnchiladaBoy/Youtube-in-the-terminal.git
cd Youtube-in-the-terminal
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python yt-ascii https://youtu.be/jNQXAC9IVRw
```

On Windows, create the environment with `py -3 -m venv .venv`, activate it with
`.\.venv\Scripts\Activate.ps1`, and use `python` for the remaining commands.
Source installs require Python 3.10 or newer.

## Build and test

PyInstaller packaging is available for local use, but this project does not
publish or install executables. PyInstaller cannot cross-compile, so build on
the target operating system:

```sh
python -m pip install -r requirements-build.txt
python packaging/build.py          # dist/yt-ascii or dist/yt-ascii.exe
```

Run the regression suite, offline self-test, and deterministic benchmark with:

```sh
python -m unittest discover -s tests -v
python yt-ascii --self-test
python benchmarks/benchmark_renderer.py
```

GitHub Actions tests source and installer behavior on Linux with Python 3.10
and 3.12, macOS arm64 with Python 3.12, and Windows with Python 3.12. It does
not build, upload, or publish executable artifacts.

See [performance notes and benchmarks](PERFORMANCE.md) for measured bottlenecks
and implementation details.

## Author

EnchiladaBoy

# Youtube in the Terminal

Stream YouTube videos as ASCII art in your terminal — 24-bit color, audio,
persistent video styles, and two reveal effects — on **Linux, macOS and
Windows**. The command is `yt-ascii`.

## Quick start

The installer fetches the project source, creates an isolated Python
environment for it, and exposes the `yt-ascii` command for your user account.
It requires Python 3.10 or newer and an internet connection.

Linux and macOS:

```sh
curl -fsSL https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | python3 -
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | py -3 -
```

If the Windows Python launcher is unavailable, replace `py -3 -` with
`python -` in these commands.

Open a new terminal after installation so the updated `PATH` is available,
then run `yt-ascii`. The installer also prints the launcher's absolute path.
By default it installs the stable `v0.4.0` source tag rather than a mutable
branch.

Launched with no arguments, it shows a prompt — paste a YouTube link and press
Enter:

```
   +-----------------------------------------------+
   |   yt-ascii  -  YouTube as ASCII in your shell  |
   +-----------------------------------------------+

   > paste link (q to quit):
```

Or pass a link directly:

```
yt-ascii https://youtu.be/jNQXAC9IVRw
```

The private environment contains NumPy, yt-dlp, imageio-ffmpeg and certifi. The
player prefers a system `ffmpeg` when one is on `PATH`, then falls back to the
copy supplied by imageio-ffmpeg. On some Linux systems the fallback's static
build cannot resolve DNS; install your distribution's FFmpeg package if video
does not start (`sudo apt install ffmpeg` or `sudo dnf install ffmpeg`).

Audio is optional and still requires `ffplay` on `PATH`; playback is silent when
it is unavailable. Point the player at specific binaries with the
`YTASCII_FFMPEG` / `YTASCII_FFPLAY` environment variables.

## Install channels and maintenance

The default command installs the stable version recorded in `STABLE_VERSION`.
Choose a tagged source version or the latest development branch by adding an
installer argument. Installer-managed tagged versions start at `v0.3.0`:

```sh
# Linux / macOS: exact tagged version
curl -fsSL https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | python3 - --version v0.4.0

# Linux / macOS: unreleased main branch
curl -fsSL https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | python3 - --edge
```

```powershell
# Windows PowerShell: exact tagged version
irm https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | py -3 - --version v0.4.0

# Windows PowerShell: unreleased main branch
irm https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | py -3 - --edge
```

Rerun the corresponding stable, versioned or edge command to update that
installation. `--edge` deliberately follows mutable, unreviewed development
code. To install without changing the user `PATH`, append
`--no-modify-path`. Run `yt-ascii --version` to see the application version and
installed source reference.

The default locations are:

- Linux/macOS data: `${XDG_DATA_HOME:-$HOME/.local/share}/yt-ascii`
- Linux/macOS launcher: `$HOME/.local/bin/yt-ascii`
- Windows data: `%LOCALAPPDATA%\Programs\yt-ascii`
- Windows launcher: `%LOCALAPPDATA%\Programs\yt-ascii\bin\yt-ascii.cmd`

Uninstall with:

```sh
curl -fsSL https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | python3 - --uninstall
```

```powershell
irm https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py | py -3 - --uninstall
```

Uninstall removes only installer-managed versions, state, launcher and `PATH`
marker. It leaves unrelated files and user configuration alone.

### Inspect before running

Piping downloaded Python into an interpreter executes remote code. If you want
to inspect it first, download the installer and run the saved copy:

```sh
curl -fsSLo install.py https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py
less install.py
python3 install.py
```

```powershell
irm https://raw.githubusercontent.com/EnchiladaBoy/Youtube-in-the-terminal/main/install.py -OutFile install.py
Get-Content .\install.py
py -3 .\install.py
```

To pin the installer code you review, replace `main` in the download URL with a
reviewed tag or full commit ID. When using a version tag in that URL, also pass
the same tag with `--version` so the application source comes from that tag.
The installer never needs administrator privileges.

## Controls

| Key            | Action            |
|----------------|-------------------|
| `space`        | pause / resume    |
| `←` / `→`      | seek -5s / +5s    |
| `↓` / `↑`      | seek -30s / +30s  |
| `0`–`9`        | jump to 0%–90%    |
| `s`            | cycle video style |
| `q` / `Ctrl-C` | quit              |

## Options

`--no-color`, `--no-audio`, `--8bit` (chiptune audio), `--pixels` (half-block,
2× vertical resolution), `--style`, `--scatter` / `--rain` (reveal effects),
`--rain-chars` (rain glyph set), `--fps`, `--width` / `--height`, `--max-res`,
`--palette`, `--chars`. See `yt-ascii --help` for the full list.

### Video styles

Version 0.4.0 adds persistent frame styles.

Choose a starting style with `--style NAME`, then press `s` during playback to
cycle through the styles without restarting the video:

| Style | Effect |
|---|---|
| `classic` | Original unmodified RGB video (the default) |
| `bayer` | Four-level ordered dithering with the source hue retained |
| `duotone` | Navy-to-gold luminance mapping |
| `riso` | Offset red and blue ink plates with purple overlaps |
| `contour` | Bright cyan edge lines on black |
| `glitch` | Animated RGB separation, scanlines, and horizontal displacement |

Styles work with character and `--pixels` rendering, color and `--no-color`,
and both reveal effects. A style selected interactively remains active for the
next video entered at the prompt. Glitch animation freezes while paused and is
deterministic after seeking.

```sh
yt-ascii https://youtu.be/jNQXAC9IVRw --style bayer
yt-ascii https://youtu.be/jNQXAC9IVRw --style riso --palette symbols
yt-ascii https://youtu.be/jNQXAC9IVRw --style contour --pixels --rain
```

Alongside the existing palettes, v0.4.0 adds `binary`, `numbers`, `symbols`,
and `matrix`. Pass a palette name with `--palette`; an explicit `--chars`
string always takes precedence:

```sh
yt-ascii https://youtu.be/jNQXAC9IVRw --style glitch --palette matrix
yt-ascii https://youtu.be/jNQXAC9IVRw --style duotone --chars ' .oO@'
```

The style implementations use established image-processing techniques and
project-owned defaults. Ladybug was visual inspiration only; no Ladybug code,
shaders, assets, or presets are included.

## Run from source

```sh
git clone https://github.com/EnchiladaBoy/Youtube-in-the-terminal.git
cd Youtube-in-the-terminal
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python yt-ascii <url>
```

On Windows, create the environment with `py -3 -m venv .venv`, activate it with
`.\.venv\Scripts\Activate.ps1`, and use `python` for the remaining commands.
Source installs require Python 3.10 or newer.

## Build a local executable

PyInstaller packaging remains available for local use, but executables are not
published or installed by this project. PyInstaller cannot cross-compile, so
build on the target OS:

```sh
python -m pip install -r requirements-build.txt
python packaging/build.py          # -> dist/yt-ascii  (yt-ascii.exe on Windows)
```

The GitHub Actions workflow tests source and installer behavior on Linux with
Python 3.10 and 3.12, macOS arm64 with Python 3.12, and Windows with Python
3.12. It does not build, upload or publish executable artifacts.

For measured bottlenecks and proposed optimizations, see the
[performance improvement roadmap](PERFORMANCE.md).

Run the regression suite and deterministic renderer benchmark with:

```sh
python -m unittest discover -s tests -v
python benchmarks/benchmark_renderer.py
```

## Author

EnchiladaBoy

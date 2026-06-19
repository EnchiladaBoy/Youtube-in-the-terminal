# Youtube in the Terminal

Stream YouTube videos as ASCII art in your terminal — 24-bit color, audio, and
two reveal effects — on **Linux, macOS and Windows**. The command is `yt-ascii`.

## Quick start

Download the executable for your OS from the
[latest release](../../releases/latest), then run it:

- **Linux / macOS:** `chmod +x yt-ascii-* && ./yt-ascii-*`
- **Windows:** double-click `yt-ascii-windows-x86_64.exe` (or run it from a terminal)

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
./yt-ascii https://youtu.be/jNQXAC9IVRw
```

**Dependencies are bundled** — yt-dlp ships inside the released binaries, and so
does ffmpeg. The binary still prefers a system `ffmpeg` when one is on your
`PATH`, because the bundled Linux build is statically linked and can fail DNS on
some hosts:

- **macOS / Windows** — works out of the box, nothing to install.
- **Linux** — if video won't start, install ffmpeg (`sudo dnf install ffmpeg` or
  `sudo apt install ffmpeg`); the bundled copy is only a fallback.

Audio is optional: it uses `ffplay` (shipped with ffmpeg) when found on your
`PATH`, and plays silently otherwise. Point the player at specific binaries with
the `YTASCII_FFMPEG` / `YTASCII_FFPLAY` environment variables.

## Controls

| Key            | Action            |
|----------------|-------------------|
| `space`        | pause / resume    |
| `←` / `→`      | seek -5s / +5s    |
| `↓` / `↑`      | seek -30s / +30s  |
| `0`–`9`        | jump to 0%–90%    |
| `q` / `Ctrl-C` | quit              |

## Options

`--no-color`, `--no-audio`, `--8bit` (chiptune audio), `--pixels` (half-block,
2× vertical resolution), `--typewriter` / `--rain` (reveal effects), `--fps`,
`--width` / `--height`, `--max-res`, `--palette`, `--chars`. See
`yt-ascii --help` for the full list.

## Run from source

```
git clone https://github.com/EnchiladaBoy/Youtube-in-the-terminal.git
cd Youtube-in-the-terminal
pip install -r requirements.txt
./yt-ascii <url>            # Windows: python yt-ascii <url>
```

Requires Python 3.9 or newer.

## Build your own executable

PyInstaller can't cross-compile, so build on each target OS:

```
pip install -r requirements-build.txt
python packaging/build.py          # -> dist/yt-ascii  (yt-ascii.exe on Windows)
```

The GitHub Actions workflow in `.github/workflows/build.yml` builds Linux,
macOS (Apple Silicon) and Windows binaries on every push to `main`, and
attaches them to a GitHub Release when you push a `v*` tag. (Intel Macs aren't
built separately — GitHub is retiring the Intel runner; run from source there.)

## Author

EnchiladaBoy

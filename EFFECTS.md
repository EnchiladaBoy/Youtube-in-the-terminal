# Terminal effects catalog

This file is the source of truth for structural terminal effects: what ships,
what is being tested on the edge channel, what the shared engine should support
next, and what belongs outside the portable player. RGB video styles are listed
in [`README.md`](README.md); renderer and pipeline budgets live in
[`PERFORMANCE.md`](PERFORMANCE.md).

## Terms and status

- **Style** transforms the decoded RGB frame before terminal structure is
  chosen. Styles cycle with `s`.
- **Effect** rebuilds terminal structure from the styled frame. Effects cycle
  with `e`; `none` preserves the ordinary renderer.
- **Reveal** temporarily uncovers the composed output at playback start or
  after seeking. `scatter` and `rain` are reveals, not persistent effects.
- **Stable** is available from the default tagged installer.
- **Edge candidate** is implemented on development `main` but not yet selected
  by `STABLE_VERSION`.
- **Planned** has an agreed engine path but no shipped public contract.
- **Approximation** intentionally translates a raster, physical, or interface
  look into terminal cells rather than promising visual equivalence.
- **Deferred subsystem** requires media, tracking, graphics, or editor
  architecture beyond the effect engine.

## Current catalog

Stable v0.4.0 provides the `classic`, `bayer`, `duotone`, `riso`, `contour`, and
`glitch` RGB styles plus the `scatter` and `rain` reveals. The v0.5.0 edge
candidate provides 34 selectable names: `none` followed by these 33 effects in
this exact cycle order:

| Order | CLI name | Status | Terminal interpretation |
|---:|---|---|---|
| 0 | `none` | Edge candidate; stable behavior | Explicitly preserves ordinary v0.4 style, palette, and pixel/character rendering |
| 1 | `geometry` | Edge candidate | Assign geometric glyphs to luminance bands |
| 2 | `contour-glyph` | Edge candidate | Draw local image contours with directional glyphs |
| 3 | `hatch` | Edge candidate | Represent light and shade with directional hatch marks |
| 4 | `dotfield` | Edge candidate | Convert tone into a deterministic dot-density field |
| 5 | `tile-mosaic` | Edge candidate | Reconstruct the frame from averaged rectangular tiles |
| 6 | `wave-lines` | Edge candidate | Animate a line field bent by tone and video time |
| 7 | `voronoi` | Edge candidate | Sample the frame into deterministic seeded regions |
| 8 | `afterimage` | Edge candidate | Blend motion into a fading frame-history trail |
| 9 | `number-field` | Edge candidate | Label each cell with its exact luminance decile |
| 10 | `glyph-grid` | Edge candidate | Reconstruct the image as a seeded light/heavy cell lattice |
| 11 | `vector-field` | Edge candidate | Point directional data marks toward increasing luminance |
| 12 | `word-field` | Edge candidate | Repeat a configured phrase with luminance-controlled density |
| 13 | `inscription` | Edge candidate | Write a decorated phrase across detected image contours |
| 14 | `type-echo` | Edge candidate | Layer deterministic time-offset copies of a configured phrase |
| 15 | `error-diffusion` | Edge candidate | Quantize tone into a deterministic binary glyph field |
| 16 | `halftone` | Edge candidate | Rebuild tone with a clustered-dot screen |
| 17 | `poster-press` | Edge candidate | Reduce ink levels and offset red/blue press plates |
| 18 | `cross-stitch` | Edge candidate | Convert tone into alternating diagonal stitches |
| 19 | `weave` | Edge candidate | Interlace light and dark warp-and-weft marks |
| 20 | `kilim` | Edge candidate | Gate mirrored chevron and diamond textile motifs by tone |
| 21 | `quadtree` | Edge candidate | Subdivide image detail into adaptive mean-color blocks |
| 22 | `patchwork` | Edge candidate | Reconstruct the frame from seeded irregular patches |
| 23 | `digital-rain` | Edge candidate | Drop analytic glyph heads and fading column trails |
| 24 | `ribbon-scan` | Edge candidate | Sweep warped highlight ribbons over a dimmed frame |
| 25 | `pixel-sort` | Edge candidate | Sort luminance inside animated horizontal blocks |
| 26 | `stardust` | Edge candidate | Drift a luminance-controlled field of twinkling particles |
| 27 | `type-collage` | Edge candidate | Animate staggered and reversed configured-text bands |
| 28 | `engraving` | Edge candidate | Combine contour strokes with directional hatching |
| 29 | `brickwork` | Edge candidate | Rebuild tone as staggered bricks and mortar |
| 30 | `prism` | Edge candidate | Split and recombine displaced color channels |
| 31 | `hologram` | Edge candidate | Map tone to scanlined cyan/magenta light and sparkles |
| 32 | `glass` | Edge candidate | Refract the frame through frosted offset blocks |
| 33 | `terminal-hud` | Edge candidate | Overlay borders, reticles, grid ticks, and analytic time |

Public controls are `--effect`, `--effect-glyphs ascii|unicode`,
`--effect-speed`, `--effect-seed`, and `--effect-text`. ASCII is the portable
glyph default; Unicode is an opt-in richer schema. The speed must be positive
and finite, the integer seed makes procedural layouts reproducible, and effect
text defaults to `YTASCII`.

The first cell/type slice has explicit terminal-native behavior.
`number-field` quantizes cell luminance into the ten deciles `0` through `9`.
`glyph-grid` uses an approximately square 4-row by 8-column lattice whose seed
shifts its phase; source tone selects blank, light, or heavy interiors and line
glyphs. `vector-field` applies a cell-space Sobel operator, suppresses weak
gradients, and quantizes the direction of increasing luminance into eight
marks. Its ASCII schema shares slash glyphs for opposite diagonals, while
Unicode mode exposes eight distinct arrows.

The completing cell/type slice adds configurable text. `word-field`
repeats the configured phrase with a row stagger and seeded phase/hash; cell
luminance controls density, so black is blank and white exposes the complete
field. Its repeated token is `TEXT + ". "` in ASCII or `TEXT + "· "` in
Unicode. `inscription` visits Sobel-active contour cells in row-major order and
writes `[TEXT] ` in ASCII or `‹TEXT› ` in Unicode. `type-echo` derives the
integer phase `floor(video_time × speed × 6)` and builds up to three repeating
row-band ages from `TEXT + ": "` in ASCII or `TEXT + "∶ "` in Unicode. Their
5/3/1 density and RGB levels create a ghosted type trail without retained frame
history.

`--effect-text` preserves the supplied code-point sequence exactly and never
normalizes it. It accepts 1 to 253 code points and must include a non-space
character. ASCII mode requires portable printable ASCII. Unicode mode accepts
printable, left-to-right one-cell code points and ordinary spaces, while
rejecting control, combining or decomposed sequences, and East Asian wide or
full-width characters with a clear command-line error. Locale-dependent
ambiguous-width characters remain subject to the Unicode-mode caveat below.

The print/textile family gives each public name a fixed terminal-native model.
`error-diffusion` accumulates binary tone quantization in seeded serpentine
order. `halftone` applies a seed-shifted 4×4 clustered-dot rank screen.
`poster-press` uses four channel levels, edge darkening, and offset red/blue
plates. `cross-stitch`, `weave`, and `kilim` use deterministic checker stitches,
over-under line roles, and mirrored 8×16 chevron/diamond motifs respectively.

The adaptive family stays frame-derived. `quadtree` uses integral-image
variance, a maximum depth of five, and minimum 4×4 leaves; `patchwork` uses a
cached seeded BSP layout of no more than 32 rectangles with four-cell minimum
sides. Both fill regions from current-frame means and expose one-cell seams.

The temporal family is analytic rather than retained state. `digital-rain`
uses 12 Hz column heads and trails, `ribbon-scan` uses 8 Hz warped highlight
bands, `pixel-sort` uses 6 Hz stable sorting within 16-cell horizontal blocks,
and `stardust` uses 8 Hz hashed particle drift and twinkle. `type-collage`
derives staggered/reversed configured-text bands at 3 Hz. `hologram` adds 12 Hz
scanline sparkles, while `terminal-hud` reconstructs its five-digit readout at
10 Hz.

The remaining interface/material approximations are intentionally raster and
terminal native. `engraving` combines Sobel contours with tone hatching;
`brickwork` uses seeded staggered 4×8 courses; `prism` displaces opposing color
channels; `glass` applies cached 4×6 block refraction, frost, and highlights;
and `terminal-hud` adds fixed border, reticle, tick, and digit roles. None claim
physical simulation, tracking, or a compositing interface.

## Compatibility contract

- The pipeline is decoded RGB → style → structural effect → ANSI renderer →
  optional reveal. Style and effect selections are independent and persist
  between videos in one interactive session. Configured effect text also
  persists for that session.
- `none` must preserve the v0.4 renderer path and output. Enabling effects must
  not change the behavior of `--style`, `--palette`, or `--chars` when the
  effect does not supply its own cell structure.
- ASCII mode uses portable one-cell glyphs. Unicode mode excludes full-width,
  combining, and control characters, but may include symbols with
  locale-dependent ambiguous width. Configured effect text follows the same
  cell-safety rules and is preserved verbatim. CJK-width terminal
  configurations should use the ASCII default if columns drift.
- Color and `--no-color` are supported. Structural glyph and region choices
  remain meaningful without RGB color.
- `geometry`, `contour-glyph`, `hatch`, `dotfield`, `number-field`,
  `glyph-grid`, `vector-field`, `word-field`, `inscription`, `type-echo`,
  `error-diffusion`, `halftone`, `cross-stitch`, `weave`, `kilim`,
  `digital-rain`, `stardust`, `type-collage`, `engraving`, `brickwork`, and
  `terminal-hud` provide character-cell planes. They temporarily force
  character rendering when `--pixels` is set, with `pixels→chars` shown in the
  status line.
- `none`, `tile-mosaic`, `wave-lines`, `voronoi`, `afterimage`, `poster-press`,
  `quadtree`, `patchwork`, `ribbon-scan`, `pixel-sort`, `prism`, `hologram`, and
  `glass` retain half-block pixel mode and ordinary palette behavior because
  they preserve or return RGB frames.
- Effect state and size-dependent layouts reset on seek/jump, resize, style or
  effect selection, and new media. A continuous same-source reconnect preserves
  state and presentation sequence. Pausing freezes video-time animation and
  retained history.
- `--scatter` and `--rain` reveal the final effect composition. Their random
  masks remain stable when the effect or glyph schema changes at the same cell
  dimensions, and reveal completion equals ordinary composed output.
- The same timestamp and seed must produce the same procedural output on every
  supported operating system. `type-echo`, `digital-rain`, `ribbon-scan`,
  `pixel-sort`, `stardust`, `type-collage`, `hologram`, and `terminal-hud` are
  analytic time effects, so seeking reconstructs their output without retained
  history. `afterimage` remains the sole frame-history effect.

## Shared-engine roadmap

The complete edge cohort reuses the structured-frame, glyph-schema,
deterministic-layout, and renderer paths rather than adding one-off output
formats.

| Stage | Status | Capability families |
|---|---|---|
| Cell and type | Complete; edge candidates | Number and word fields, grid glyphs, contour inscriptions, directional data marks, and deterministic ghosted type |
| Print and textile | Complete; edge candidates | Error diffusion, clustered halftone, poster press, cross-stitch, weave, and kilim cell patterns |
| Adaptive regions | Complete; edge candidates | Quadtree subdivision and seeded patchwork region maps |
| Temporal structure | Complete; edge candidates | Digital rain, ribbon scans, block pixel sorting, stardust, and analytic motion |
| Interface and material looks | Complete; edge candidates; approximations | Type collage, engraving, brickwork, prism, hologram, glass, and terminal HUD overlays |

These names are now public CLI contracts. Future additions must retain an
ASCII-safe default and define an explicit terminal-native visual model before
entering the registry.

## Deferred subsystems

The following are intentionally outside the portable effect engine:

- audio reactivity, until playback exposes synchronized PCM analysis;
- hand, face, subject, or HUD target tracking and background removal, which
  need camera/decoder integration and packaged CV models;
- a compositing timeline, masks, keyframes, and project/preset storage;
- true 3D scenes, GPU shaders, raster graphics protocols, vector generation,
  and high-resolution image/video export.

Static terminal approximations may be proposed under new names, but must not
claim the tracking, physical-material, 3D, or export behavior of those systems.

## Shipping rule

An effect is not **shipped** until its public documentation, deterministic unit
and composition tests, installed self-test coverage, and isolated plus composed
benchmark cases are updated first. A stable tag additionally requires the full
Linux, macOS, and Windows source/installer matrix. Performance measurements
must be reported as measurements on named hardware; targets must not be
presented as achieved results.

All implementations are project-owned translations of established generative
art techniques for ANSI terminals. Do not copy third-party code, shaders,
assets, preset values, control recipes, or product copy, and do not claim
compatibility with another application.

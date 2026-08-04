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
candidate adds these eight effects in this exact cycle order after `none`:

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

Public controls are `--effect`, `--effect-glyphs ascii|unicode`,
`--effect-speed`, and `--effect-seed`. ASCII is the portable glyph default;
Unicode is an opt-in richer schema. The speed must be positive and finite, and
the integer seed makes procedural layouts reproducible.

## Compatibility contract

- The pipeline is decoded RGB → style → structural effect → ANSI renderer →
  optional reveal. Style and effect selections are independent and persist
  between videos in one interactive session.
- `none` must preserve the v0.4 renderer path and output. Enabling effects must
  not change the behavior of `--style`, `--palette`, or `--chars` when the
  effect does not supply its own cell structure.
- ASCII mode uses portable one-cell glyphs. Unicode mode excludes full-width,
  combining, and control characters, but may include geometric symbols with
  locale-dependent ambiguous width. CJK-width terminal configurations should
  use the ASCII default if columns drift.
- Color and `--no-color` are supported. Structural glyph and region choices
  remain meaningful without RGB color.
- `geometry`, `contour-glyph`, `hatch`, and `dotfield` provide character-cell
  planes. They temporarily force character rendering when `--pixels` is set,
  with `pixels→chars` shown in the status line. `none`, `tile-mosaic`,
  `wave-lines`, `voronoi`, and `afterimage` retain half-block pixel mode and
  the ordinary palette behavior because they return RGB frames.
- Effect state and size-dependent layouts reset on seek/jump, resize, style or
  effect selection, and new media. A continuous same-source reconnect preserves
  state and presentation sequence. Pausing freezes video-time animation and
  retained history.
- `--scatter` and `--rain` reveal the final effect composition. Their random
  masks remain stable when the effect or glyph schema changes at the same cell
  dimensions, and reveal completion equals ordinary composed output.
- The same timestamp and seed must produce the same procedural output on every
  supported operating system.

## Shared-engine roadmap

The next effects should reuse the same structured-frame, glyph-schema,
deterministic-layout, and renderer paths rather than adding one-off output
formats.

| Stage | Status | Capability families |
|---|---|---|
| Cell and type | Planned | Word/number fields, grid glyphs, inscriptions, directional data marks, and ghosted type |
| Print and textile | Planned | Error diffusion, halftone patterns, pixel press/poster treatments, crosshatch, stitch, weave, and kilim-like cell patterns |
| Adaptive regions | Planned | Block mosaics, quadtree subdivision, threshold/edge families, and additional seeded region maps |
| Temporal structure | Planned | Full-frame digital rain, drifting lines, ribbon scans, pixel sorting, stardust, and longer motion traces |
| Interface and material looks | Approximation | Tag/collage typography, engraving, simulated bricks, prism/holographic/glass treatments, and terminal HUD overlays |

Names in the roadmap are capability descriptions, not reserved CLI names.
Promote only a small coherent set at a time, with an ASCII-safe default and an
explicit terminal-native visual specification.

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

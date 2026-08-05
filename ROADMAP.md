# Product roadmap

## Direction

`yt-ascii` is now a **terminal visual renderer with ASCII as one rendering
option**. The abandoned direction was a growing catalog of Unicode/glyph
substitutions.

The v0.5.0 release remains on hold while this pivot is reviewed. The former
effect matrix and its qualification results do not count toward this release.

## v0.5 pivot scope

| Workstream | Required outcome | Working-tree status |
|---|---|---|
| Backend boundary | Canonical portable `chars` and `cells`, plus Unicode-dependent `half-block` | Implemented |
| Graphical cells | ANSI background colors and literal spaces; no visible glyph dependency | Implemented |
| No-color behavior | Requested graphical backend resolves to visible effective `chars` | Implemented |
| Static style registry | 10 deterministic color/contrast/dither/image treatments | Implemented |
| Effect registry | 10 structural/motion/text entries; only two text-specific | Implemented |
| Text-effect reduction | Retire seven redundant glyph-heavy effects without aliases | Implemented |
| RGB-first processing | Graphical methods remain visible in `cells` and `half-block` | Implemented |
| Compatibility | Capability-derived errors and compatible-only effect cycling | Implemented |
| CLI migration | `--render`, `--pixels`, style/effect boundary, narrow legacy shims | Implemented |
| Visual contracts | Glyph-free cells, ASCII paths, deterministic/distinct output | Implemented; 243-test suite and self-test pass |
| Performance matrix | 10 styles x applicable effects x six backend/color paths x two profiles | Implemented; ordinary and stress gates pass |
| Live evidence | Short representative sink/tmux/PTY classification smokes | Fresh runs pass; not matched path qualification |
| Documentation/packaging | README, help, installer, diagnostics, effects, performance, live process | Implemented in working tree |
| Visual-direction review | Manual real-terminal comparison and fresh screenshots | Pending user review |

The canonical registries are:

- Styles: `classic`, `bayer`, `posterize`, `contour`, `edge-glow`,
  `ordered-dither`, `error-diffusion`, `duotone`, `two-tone`, `riso`.
- Effects: `none`, `pixelate`, `glitch`, `crt`, `chromatic-shift`, `wave`,
  `trails`, `prism`, `digital-rain`, `terminal-hud`.

The retired text-heavy set is `contour-glyph`, `number-field`, `glyph-grid`,
`word-field`, `inscription`, `type-echo`, and `type-collage`. It will not return
under new names.

## Release-blocking review

Before v0.5 can move forward:

1. Run the complete unit and integration suite plus `yt-ascii --self-test`.
2. Run fresh ordinary and stress deterministic matrices. Diagnose failures
   without weakening CPU or behavioral gates.
3. Retain the short representative sink/tmux/attached-class PTY diagnostic
   evidence; do not expand it into a broad live Cartesian harness.
4. Manually compare every style and compatible effect in a real attached
   terminal, including grayscale fallbacks, cycling, controls, resize, and
   cleanup.
5. Capture new real-terminal screenshots only after the visual direction is
   approved. A PTY byte capture is not a screenshot.
6. Review the complete working-tree diff and validation evidence before any
   release, push, or tag decision.

## After v0.5

Portable ANSI remains the baseline. Later work may add opt-in bitmap protocol
adapters in this order, subject to capability detection and graceful fallback:

1. Kitty graphics protocol;
2. Sixel;
3. iTerm inline images.

Those protocols must consume the same post-style/post-effect RGB frame. They
must not fork the visual registry or become installation requirements.

Other post-v0.5 candidates:

- user-adjustable parameters through typed style/effect capability metadata;
- renderer-specific bandwidth adaptation based on measured write latency;
- checked-in deterministic visual fixtures after manual screenshot approval;
- optional native ANSI composition only if profiling identifies it as the
  remaining bottleneck.

The roadmap does not include reviving decorative glyph effects to inflate the
registry.

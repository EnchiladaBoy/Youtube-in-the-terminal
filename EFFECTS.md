# Renderer, style, and effect contract

This is the source of truth for the post-pivot visual registry. The v0.5.0
release remains blocked until this contract, its validation, and the new visual
direction are reviewed.

## Pipeline boundaries

```text
RGB decoder -> StyleProcessor -> EffectProcessor -> render backend -> reveal
```

1. `StyleProcessor` owns static color, contrast, quantization, dithering, and
   image treatment. A style has no timestamp and retains no frame history.
2. `EffectProcessor` owns spatial/motion transformations, bounded temporal
   state, and the two explicitly text-specific reconstructions.
3. A render backend owns terminal composition only. It does not choose or
   implement a visual treatment.

Graphical styles and effects operate on `uint8` RGB/luminance arrays before
terminal composition. An effect normally returns an RGB `EffectFrame`.
Text-specific effects may instead return a structured `CellPlane`, which is
accepted only by a backend with text-cell capability.

## Render backend registry

| Name | Protocol | Source rows/cell | Visible payload | Unicode required | Color required |
|---|---|---:|---|:---:|:---:|
| `chars` | ANSI | 1 | ASCII luminance glyph by default | No | No |
| `cells` | ANSI | 1 | Background-colored literal space | No | Yes |
| `half-block` | ANSI | 2 | `▀` joining foreground/background colors | Yes | Yes |

`cells` is first-class graphical composition. Its colored records have the
form `ESC[48;2;R;G;Bm ` and contain no decorative glyph. Reveals preserve the
same contract.

Requested `cells` or `half-block` with `--no-color` resolves explicitly to
effective `chars`, preventing invisible uncolored spaces. Compatibility is
checked against the effective backend. Diagnostics retain both values.

Character-palette selection belongs to terminal composition, not to styles or
effects. While the effective renderer is consuming its luminance palette, `p`
cycles the built-in palettes without resetting decode, reveal, style, effect,
or trail history. The key is explicitly inactive for colored graphical
backends and for text-specific effects, whose structured cell plane owns its
glyphs. No-color graphical fallbacks are effective `chars`, so palette cycling
remains available there.

Backend capability records describe protocol, source-row density, Unicode,
color, and structured-cell support. A future Kitty, Sixel, or iTerm backend can
consume the same post-effect RGB frame without modifying style or effect code.

## Canonical style registry

Style cycle order is the order below.

| # | Name | Category | Contract |
|---:|---|---|---|
| 0 | `classic` | identity | Return decoded RGB unchanged |
| 1 | `bayer` | dither | Four-level brightness screen with source hue retained |
| 2 | `posterize` | quantization | Quantize RGB channels to five levels |
| 3 | `contour` | edge treatment | Clean cyan edge map |
| 4 | `edge-glow` | edge treatment | Neon contours over a dim source field |
| 5 | `ordered-dither` | dither | Four-level ordered screen per RGB channel |
| 6 | `error-diffusion` | dither | Deterministic serpentine error propagation |
| 7 | `duotone` | color treatment | Smooth navy-to-gold luminance grade |
| 8 | `two-tone` | color treatment | Hard two-color luminance threshold |
| 9 | `riso` | color treatment | Offset red/blue ink plates |

Every non-identity style preserves `uint8` RGB geometry, does not mutate its
input, is deterministic for the same frame, and materially changes the visual
contract fixture. All styles can precede every applicable effect/backend pair.

## Canonical effect registry

Effect cycle order is the order below. Compatibility aliases and migration
shims never appear in this registry, cycling, status, or diagnostics.

| # | Name | Kind | State | Renderers |
|---:|---|---|---|---|
| 0 | `none` | identity | none | chars, cells, half-block |
| 1 | `pixelate` | graphical/spatial | analytic | chars, cells, half-block |
| 2 | `glitch` | graphical/motion | timestamp | chars, cells, half-block |
| 3 | `crt` | graphical/display | timestamp | chars, cells, half-block |
| 4 | `chromatic-shift` | graphical/structural | analytic | chars, cells, half-block |
| 5 | `wave` | graphical/motion | timestamp | chars, cells, half-block |
| 6 | `trails` | graphical/temporal | bounded history | chars, cells, half-block |
| 7 | `prism` | graphical/structural | analytic | chars, cells, half-block |
| 8 | `digital-rain` | text-specific motion | timestamp | chars only |
| 9 | `terminal-hud` | text-specific interface | timestamp | chars only |

Every graphical effect preserves `uint8` RGB geometry and does not mutate its
input. Its output must be materially different from `none` on the contract
fixture and remain visibly distinct in `cells`; changing a glyph schema is not
a graphical implementation.

Effect output is deterministic for the same input, seed, video time, and
retained history. `trails` is the only effect with frame history. Its one-frame
history is bounded by the current geometry and resets on source, seek, resize,
style change, or effect selection. Timestamp-driven effects reconstruct at the
same video time rather than advancing from wall-clock calls.

## Compatibility matrix

| Family | `chars` | `cells` | `half-block` |
|---|:---:|:---:|:---:|
| All 10 styles | Yes | Yes | Yes |
| `none`, `pixelate`, `glitch`, `crt`, `chromatic-shift`, `wave`, `trails`, `prism` | Yes, quantized | Yes | Yes |
| `digital-rain`, `terminal-hud` | Yes | Explicit error | Explicit error |

Effect cycling is filtered by backend capabilities, so a colored `cells` or
`half-block` run cannot cycle into a text plane it cannot compose. With
`--no-color`, the requested graphical backend has already become effective
`chars`, so the visible character-compatible set applies.

## Text-specific category

Only two text-specific effects remain:

- `digital-rain` creates moving luminance-gated heads and fading vertical
  trails.
- `terminal-hud` composes borders, reticles, tick marks, labels, and readout
  regions around the source image.

Both default to portable ASCII. `--effect-glyphs unicode` is opt-in and
subject to terminal-width behavior. `--effect-text` labels the HUD; it does not
create a general tiled-word or collage family.

## Compatibility names and migration shims

These true effect aliases select another canonical effect immediately:

| Historical input | Canonical effect |
|---|---|
| `tile-mosaic` | `pixelate` |
| `wave-lines` | `wave` |
| `afterimage` | `trails` |
| `hologram` | `crt` |

The CLI parser also accepts narrowly scoped migration shims for selections
whose architectural category changed:

| Legacy selection | Canonical interpretation |
|---|---|
| `--style glitch` | `--style classic --effect glitch` |
| `--effect posterize` | `--style posterize --effect none` |
| `--effect edge-glow` | `--style edge-glow --effect none` |
| `--effect ordered-dither` | `--style ordered-dither --effect none` |
| `--effect error-diffusion` | `--style error-diffusion --effect none` |
| `--effect duotone` | `--style two-tone --effect none` |
| `--effect poster-press` | `--style posterize --effect none` |

These are parser-boundary migrations, not registry entries or cycle aliases.
They apply only when unambiguous. A legacy style migration combined with a
different explicit effect, or a legacy effect migration combined with a
different explicit style, is rejected with a clear error rather than silently
discarding either choice.

## Removed text-heavy registry

The redundant text effects below are retired with no alias or migration shim:

- `contour-glyph`
- `number-field`
- `glyph-grid`
- `word-field`
- `inscription`
- `type-echo`
- `type-collage`

Their removal is intentional product scope, not a temporary compatibility
gap. New rendering methods should operate on image/color/luminance data rather
than substitute another Unicode alphabet.

## Acceptance gates

A style/effect/backend combination qualifies only when all applicable gates
hold:

- deterministic unit and visual-contract fixtures pass;
- non-identity output materially differs from its baseline and retained peers;
- `cells` uses background-color sequences plus spaces, never decorative glyphs;
- ASCII-only `chars` and `cells` contracts decode without Unicode;
- every declared backend/color combination composes successfully;
- incompatible combinations raise an explicit, stable error;
- compatible-only cycling cannot select a blank or unsupported result;
- ordinary 120x34 at 30 fps and stress 240x68 at 60 fps meet
  [`PERFORMANCE.md`](PERFORMANCE.md);
- reset, pause, seek, resize, reveal, diagnostics, and cleanup contracts pass;
- the short representative path smokes and manual real-terminal review in
  [`LIVE_PERFORMANCE.md`](LIVE_PERFORMANCE.md) are completed;
- help, README, installer packaging, diagnostics, roadmap, and examples match
  this registry.

Pre-pivot effect-matrix evidence does not satisfy these gates. No release,
push, or tag is implied by a passing working-tree check.

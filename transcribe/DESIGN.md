# WHISPER — UI design system

The entire frontend is one file: `src/whisper_notes/web/static/index.html`
(D-09: vanilla HTML/CSS/JS, no build step). This records the durable visual
decisions so later surfaces extend rather than reinvent them.

## World

A quiet local instrument, Operate mode. The drop zone is the hero because the
drop is the product. One column, max-width 860px, comfortable at 1280×800.
No dashboard-card scaffold, no decorative motion.

## Tokens (CSS custom properties on `:root`)

- **Ground:** cool neutrals. Light: `--bg #f4f6f7`, `--surface #ffffff`,
  `--panel #e9edef`. Dark (via `prefers-color-scheme`): `#14181b / #1d2327 / #242c31`.
- **Accent:** one deep teal carries every action and progress signal —
  `#0d7a6e` light, `#37c0af` dark. Nothing else is colored except `--danger`
  (`#b23527` / `#ef7c6d`) for destructive/failure state.
- **Type:** system stack (SF Pro on target). Four sizes only:
  11.5px uppercase labels · 13px secondary · 15px body/base · 17px headings.
  Tabular numerals on all timing/count figures.
- **Shape:** cards radius 10px, hero surfaces 14px, controls 7–8px.
  Hairline `1px var(--border)` borders; soft two-layer shadow (`--shadow`).

## Components

- **Buttons:** solid accent primary, `ghost` bordered secondary, 30px
  `icon-btn` squares. Icons are drawn inline SVGs, 1.5px stroke, round caps —
  never emoji or glyph characters.
- **Meter:** 5px rounded track (`--panel`) + accent fill. Determinate fills by
  `transform: scaleX()`; indeterminate slides a 30% segment via `translateX`.
  Queued items show a still, empty track — motion means work is happening.
- **Job card:** name · timing · phase line · meter; done state reveals
  preview (fading clamp) + download + meta row with a 220ms rise.
- **History:** dense table — When / File / Length / Lang / Words / actions.
  Empty state teaches where transcripts live on disk.
- **Toasts:** neutral surface, leading state dot (accent/danger). No colored
  side borders anywhere.

## Motion

150–250ms ease-out, state feedback only: hover surfaces, meter progress, the
done-card reveal. No entrance choreography, no scroll effects.

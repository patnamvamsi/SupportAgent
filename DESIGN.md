---
name: SupportAgent
description: Self-hosted support analyst admin UI where every decision is an exhibit, tagged, sealed and traceable.
colors:
  ground: "#eef1f4"
  panel: "#ffffff"
  sunk: "#f6f8fa"
  ink: "#16202d"
  ink-2: "#364253"
  muted: "#566273"
  line: "#d5dce4"
  line-2: "#c3ccd7"
  rail: "#16202d"
  rail-2: "#223044"
  rail-ink: "#cfd7e1"
  rail-muted: "#8e9aab"
  tag: "#f3c316"
  tag-ink: "#16202d"
  seal: "#c4232b"
  seal-ink: "#ffffff"
  seal-tint: "#fdecec"
  agent: "#5b3fd6"
  agent-tint: "#eeeafd"
  ok: "#1b7340"
  ok-tint: "#e4f3ea"
  warn: "#9a5a00"
  warn-tint: "#fcf0d9"
  select: "#2b4a73"
  select-tint: "#e2e9f2"
  k-service: "#3b6db3"
  k-job: "#a06a24"
  k-data: "#23806f"
  k-topic: "#a4527c"
  k-external: "#737e8c"
typography:
  headline:
    fontFamily: "Public Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "1.6rem"
    fontWeight: 680
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  title:
    fontFamily: "Public Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "1.08rem"
    fontWeight: 650
    letterSpacing: "-0.005em"
  subtitle:
    fontFamily: "Public Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "0.98rem"
    fontWeight: 650
  body:
    fontFamily: "Public Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "tnum"
  label:
    fontFamily: "Public Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "0.8rem"
    fontWeight: 600
    lineHeight: 1.5
  mono-tag:
    fontFamily: "ui-monospace, SF Mono, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 700
    lineHeight: 1.5
    letterSpacing: "0.02em"
rounded:
  hairline: "3px"
  sm: "4px"
  control: "6px"
  md: "8px"
  pill: "999px"
spacing:
  xs: "0.35rem"
  sm: "0.5rem"
  md: "0.75rem"
  lg: "1rem"
  xl: "1.25rem"
  page-y: "28px"
  page-x: "36px"
components:
  button:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0.45rem 0.95rem"
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.panel}"
    rounded: "{rounded.control}"
    padding: "0.45rem 0.95rem"
  button-primary-hover:
    backgroundColor: "{colors.ink-2}"
  button-seal:
    backgroundColor: "{colors.seal}"
    textColor: "{colors.seal-ink}"
    rounded: "{rounded.control}"
    padding: "0.45rem 0.95rem"
  input:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0.4rem 0.6rem"
  exhibit-tag:
    backgroundColor: "{colors.tag}"
    textColor: "{colors.tag-ink}"
    typography: "{typography.mono-tag}"
    padding: "1px 7px 1px 12px"
  chip:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "0.05rem 0.55rem"
  severity-p1:
    backgroundColor: "{colors.seal}"
    textColor: "{colors.seal-ink}"
    rounded: "{rounded.sm}"
    padding: "0 0.35rem"
  panel:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.md}"
    padding: "1rem 1.15rem"
  rail:
    backgroundColor: "{colors.rail}"
    textColor: "{colors.rail-ink}"
    width: "236px"
  rail-item-current:
    backgroundColor: "{colors.rail-2}"
    textColor: "#ffffff"
    rounded: "{rounded.control}"
    padding: "8px 10px"
---

# Design System: SupportAgent

## Overview

**Creative North Star: "The Evidence Room"**

The admin UI is a lab bench where every decision the agent makes sits as an exhibit: tagged with an ID, sealed when a human must authorize it, and its chain of custody one click away. A blue-grey bench ground holds white evidence panels; a deep ink-navy rail runs down the left. Colour is not decoration here, it is apparatus: three inks are reserved for three meanings (exhibit yellow, seal red, agent violet) and everything else stays in the neutral blue-grey family, so a newcomer at 3 a.m. can read what matters from colour alone.

Density is operational, not airy: 15px body, tabular numerals everywhere, tables and custody rows packed for scanning. State is carried twice, in hue and in line form, so it survives colour blindness, dark mode and greyscale: confirmed is a solid line, proposed is dashed (or wireframe in 3D), did-not-run is struck through. The dark theme is a graphite lightbox that keeps the same tag, seal and violet reservations, with each ink lifted for contrast.

The system rejects the category default of KPI tiles over a grey card grid with one blue accent. It also runs under a strict CSP and fully offline: one self-hosted variable face, a vendored 3D library, an inline SVG sprite, no inline styles or scripts.

**Key Characteristics:**
- Reserved inks: yellow means evidence, red means seal, violet means the agent's own path.
- State in line form: solid confirmed, dashed proposed, struck did-not-run.
- Full borders and tints for state; never a coloured side stripe.
- Notched monospace exhibit tags; custody steps numbered in violet rings.
- One variable sans (Public Sans 100–900) plus the platform monospace.
- Light and dark follow the OS; both are first-class.

## Colors

A cool blue-grey neutral family carrying three reserved, single-meaning inks and a quiet navy for every interaction.

### Primary
- **Bench Ink Navy** (ink, rail): primary text, primary buttons, confirmed lines and edges, and the left rail's ground. Primary action is ink, not a brand hue.
- **Selection Slate Blue** (select, select-tint): all interaction chrome: link hover, focus ring, selected row (inset 2px ring on select-tint), selected 3D node ring, active card outline, checkbox and progress accent, text selection. Dark: #c8d4e4 on #263040.

### Secondary (reserved inks)
- **Exhibit Tag Yellow** (tag on tag-ink): the notched exhibit tag on evidence IDs and exhibit counts ("3/4 held"). Same value in both themes.
- **Seal Red** (seal, seal-tint, seal-ink): approvals (the approvals count, seal buttons, the dashed seal box), P1 severity, failures, contradicted claims, error steps, alarms, and the open-case halo and alert cone in 3D. Dark: #f0646c on #3a1d21.
- **Agent Violet** (agent, agent-tint): only the agent's own reasoning: the case path hop in 3D, the gate where triage stopped or routed, custody step rings, LLM spans in the replay, and the Case path toggle when pressed. Dark: #a594ff on #262043.

### Tertiary (status and kind)
- **Held Green** (ok, ok-tint): exhibits that held, confirmed and ready states. Dark #63d394.
- **Proposal Amber** (warn, warn-tint): proposed relations (dashed underline), P2 severity, warnings. Dark #eab05a.
- **Kind hues** (k-service blue, k-job ochre, k-data teal, k-topic mauve, k-external grey): node families in the 3D explorer and its legend only. Each kind also has its own shape, so the hue is never the only cue.

### Neutral
- **Bench Blue-Grey** (ground): page ground. Dark #141516.
- **Evidence White** (panel): panels, cards, tables, inputs. Dark #1b1c1e.
- **Sunk Tray** (sunk): table heads, pre blocks, quoted evidence, hover rows. Dark #17181a.
- **Ink 2 / Muted** (ink-2, muted): secondary text and metadata. Dark #c8cacd / #9da1a7.
- **Hairline / Stroke** (line, line-2): dividers and borders; line-2 for control borders and dashed proposed outlines.
- **Rail tones** (rail-2, rail-ink, rail-muted): rail hover/current fill, rail text, rail group labels.

### Named Rules
**The Three Inks Rule.** Yellow is only on exhibit tags, red only on seals (approvals, P1, failures), violet only on the agent's own path and decisions. If an element is none of those, it may not use those inks.

**The Slate Chrome Rule.** Every hover, focus, selection and active indicator uses select. Interaction never borrows a reserved ink.

**The Twice-Said Rule.** Every state carries a line-form or text cue as well as a hue: solid, dashed, struck, or a word.

## Typography

**Display / Body Font:** Public Sans (variable, 100–900, self-hosted woff2, OFL) with system-ui, -apple-system, Segoe UI fallback
**Label/Mono Font:** platform monospace (ui-monospace, SF Mono, Menlo, Consolas)

**Character:** A plain, government-grade grotesque used at intermediate variable weights (650, 680, 720) for quiet authority; monospace is reserved for IDs, counts, severities, timestamps and code, the things an analyst copies.

### Hierarchy
- **Headline** (680, 1.6rem, 1.2, -0.015em, balanced wrap): page titles; case titles at 1.7rem.
- **Title** (650, 1.08rem): section heads; the dossier's selected-item head at 1.3rem.
- **Subtitle** (650, 0.98rem): sub-sections.
- **Body** (400, 15px, 1.5, tabular numerals): all running text; paragraphs capped at 72ch.
- **Label** (600, 0.8–0.84rem): chips, badges, filter labels, table heads (0.82rem, muted).
- **Mono tag** (700, 11px, 0.02em): exhibit tags, counts, severity marks, step numbers.

### Named Rules
**The Copyable Mono Rule.** Monospace marks identifiers and measurements only; it never sets prose or headings.

**The Sentence Case Rule.** Labels, group names and table heads are sentence case at normal tracking; no uppercase tracked labels.

## Layout

A two-column shell: a 236px sticky ink rail and a fluid main column (max 1280px, padding 28px 36px 64px). Review surfaces widen to 1560px; the System explorer takes the full bleed with a stage and a fixed 420px dossier column at 100vh. The case page is a main column plus a 340px sticky side column (gates and seal).

Spacing runs on a rem rhythm (0.35, 0.5, 0.75, 1, 1.25rem), with table cells at 0.6rem 0.8rem and panels at 1rem 1.15rem.

Responsive: at 1100px the case side column and the dossier stack below; at 900px the rail becomes a horizontal top bar with a scroll-masked nav, main padding drops to 16px, the 3D stage stacks its toolbar and legend in flow; at 600px non-current nav items collapse to icons.

## Elevation & Depth

Mostly flat with one shared ambient shadow for surfaces that sit on the bench (panels, tables, custody list, replay, floating toolbar, legend). Cards inside panels stay flat with a hairline. Depth in the 3D explorer comes from lit geometry, not CSS.

### Shadow Vocabulary
- **Bench lift** (`box-shadow: 0 1px 2px rgb(22 32 45 / 6%), 0 6px 18px -8px rgb(22 32 45 / 18%)`; dark `0 1px 2px rgb(0 0 0 / 30%), 0 8px 24px -10px rgb(0 0 0 / 60%)`): panels, tables, floating stage controls.
- **Agent halo** (`box-shadow: 0 0 0 5px var(--agent-tint)`): only the gate where the agent stopped or routed.

### Named Rules
**The One Lift Rule.** There is one shadow token; nothing invents a second elevation level.

## Shapes

Gently rounded rectangles (8px) for every container, 6px for controls, 4px for small marks (severity, kbd), full pills for chips and badges. Two signature silhouettes: the exhibit tag, a luggage-tag notch cut with clip-path (a 7px point on the left), and the circle, used for gate marks and custody step numbers. Borders are always full 1px hairlines; state changes the border's style (dashed) or colour, never its side.

## Components

### Buttons
- **Shape:** gently rounded (6px), 600 weight at 0.92rem, padding 0.45rem 0.95rem, 1px line-2 border.
- **Default:** panel fill, ink text; hover darkens the border to ink-2; active nudges down 1px.
- **Primary:** ink fill, panel text; hover ink-2.
- **Seal:** seal red fill for approve and authorize actions only; hover brightens 8%.
- **Segmented:** a 7px-rounded group; pressed segment is ink-filled.

### Exhibit tag (signature)
Notched yellow mono tag on evidence IDs, preceding the claim or exhibit it cites. A hollow variant (no notch, line-2 inset ring, muted text) marks a reference that is not evidence.

### Chips, badges, severity
- **Chips/badges:** pill, 0.8rem 600, line-2 border on panel. ok = green border and tint; bad = seal; running = violet; proposed = dashed amber border, no fill; confirmed = green border; rejected = muted and struck.
- **Severity:** mono box; P1 filled seal red; P2 amber border on amber tint; P3/P4 neutral.

### Cards / Containers
- **Corner Style:** 8px.
- **Background:** panel; confirmed or ready cards take a 55% ok-tint mix and a green-mixed border; not-ready takes a seal-tint mix; rejected and untestable are dashed.
- **Shadow Strategy:** panels lift (bench lift); cards inside stay flat.
- **Border:** full 1px line, always all four sides.
- **Internal Padding:** 0.75rem 0.95rem (card), 1rem 1.15rem (panel).

### Inputs / Fields
- **Style:** panel fill, 1px line-2, 6px radius, 0.4rem 0.6rem; selects carry an inline SVG chevron.
- **Focus:** 2px select outline, border goes transparent.
- **Error:** seal border on seal-tint block.

### Navigation (rail)
Ink rail, 236px, brand mark at top, grouped items with 18px stroke icons from the sprite, a who/mode block pinned to the bottom. Hover and current fill rail-2; current adds an inset 1px ring and bolder text. Counts are mono pills; the approvals count is seal red. Collapses to a top bar at 900px.

### Custody chain (signature)
A single bordered list; each step row has a violet-ringed mono number, a head, and exhibits beneath separated by dashed hairlines. Exhibit results: held in green, not held in ink-2, not run at 70% opacity with the name struck.

### Decision gates (signature)
A vertical rail of 28px circular marks joined by a 2px line. Passed gates are quiet; the gate where the agent stopped or routed fills violet with the agent halo; gates not reached are dashed and struck.

### Seal box
A 2px dashed seal-red frame holding the outcome or approval form: the one place a human authorizes.

### 3D explorer (signature)
Three.js scene on the bench ground. Node families have their own shapes and kind hues: service sphere, job octahedron, data cylinder, topic torus, external cube. Key components draw larger. Proposed nodes are wireframe and proposed edges dashed; confirmed edges are solid ink-2. Open cases ring the node in seal red and hang a red alert cone; the agent's hop from alert to component is violet and never changes the system's edges. Selection is a select-coloured ring. HTML labels float over the canvas, ranked by priority so minor labels hide when they overlap; labels carry a P-severity mark, an "n open" seal badge and the yellow exhibit count. Colours are read from the CSS custom properties, so the scene follows the theme.

### Replay timeline
Steps list with mono time and kind columns and a duration bar; LLM spans are violet, errors seal, skipped steps struck. Bar position and width are set as CSS custom properties from JS (CSSOM), never inline styles.

## Do's and Don'ts

### Do:
- **Do** keep yellow, red and violet to their single meanings (The Three Inks Rule); use select for every hover, focus and selection.
- **Do** say state twice: solid confirmed, dashed (or wireframe) proposed, struck did-not-run, plus a word.
- **Do** cite evidence with the notched exhibit tag in front of the claim it supports; leave speculation untagged in a dashed frame.
- **Do** read theme colours from CSS custom properties in JS, and set dynamic geometry through CSS custom properties via the CSSOM.
- **Do** vendor everything: fonts in static/fonts, libraries in static/vendor, icons in the inline SVG sprite.
- **Do** design both themes; the dark graphite lightbox keeps the same reservations.

### Don't:
- **Don't** use a coloured side stripe (border-left accent) on cards, rows or alerts; use a full border and a tint.
- **Don't** write inline style attributes or inline scripts; the CSP (script-src 'self'; style-src 'self') rejects them.
- **Don't** load anything from a CDN or external font service; the product must run offline.
- **Don't** show state by fading alone; fades are for dimming the unselected, not for meaning proposed.
- **Don't** build KPI tiles over a grey card grid with one blue accent.
- **Don't** use uppercase tracked labels or eyebrow text above headings.

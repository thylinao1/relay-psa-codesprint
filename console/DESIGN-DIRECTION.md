# Console design direction: the RELAY ops board

Written before any console code. Every decision below traces to a surface an
operator has to read in about two seconds, under time pressure, without training.

## Direction: "remote operations centre at night", control-room editorial

The console is a **dark ops board**, styled after a terminal remote-operations room
(PSA's own phrase: "remote exception-handling"), not a SaaS dashboard. One screen,
no navigation, no cards-in-a-uniform-grid. The layout is an editorial composition:
a dominant countdown board on the left (the thing the operator actually watches),
a narrower decision rail on the right (approval cards arrive here), and a full-width
evidence band below (trace timeline + governance tiles). Panels have different
visual weights on purpose, hierarchy through scale contrast, not through sameness.

Explicitly out of bounds:
- purple-gradient hero / template look, uniform card grids, decorative accent color
- light-gray-on-white SaaS defaults, uniform radius/spacing/shadows everywhere
- any styling that makes SYNTHETIC data look like a marketing mock

## Palette: semantic status colors as CSS custom properties

Color is *state*, never decoration. Defined once in `static/css/tokens.css`:

| token | value | meaning |
|---|---|---|
| `--ground` | `#0a0e13` | room black (page ground, blue-graphite, not pure black) |
| `--panel` | `#10161d` | panel surface |
| `--panel-raised` | `#161e27` | raised surface (cards, active rows) |
| `--line` | `#232d38` | hairline borders |
| `--ink` | `#dbe4ec` | primary text |
| `--ink-dim` | `#8595a5` | secondary text / microlabels |
| `--ink-faint` | `#55636f` | tertiary / empty states |
| `--ok` | `#3fce7c` | FEASIBLE / RECOVERED / chain-verified |
| `--warn` | `#f2b135` | AT_RISK / DEGRADED_TO_ADVISORY |
| `--bad` | `#f2545b` | INFEASIBLE / action_failed / DENY_BY_DEFAULT |
| `--hold` | `#b58cf5` | ESCALATE / HELD / insufficient-evidence (violet = "hand it to a human") |
| `--act` | `#3fa7f2` | interactive affordances (buttons, switches, links) only |
| `--rationale` | `#7b6ce0` tint band | model_rationale events, visually separated, labelled RATIONALE_NOT_AUDIT_RECORD |

Every status color has a dim `*-bg` companion (10-14% alpha) for badges and row
washes so AT_RISK rows glow without shouting.

## Typography: two families, real hierarchy

- **Numerals + identifiers** (margins, clocks, hashes, IDs): `ui-monospace, "SF Mono",
  Menlo, Consolas, monospace` with `font-variant-numeric: tabular-nums`. The margin
  figure is the hero: 40px+, weight 650. Clocks and cut-offs 20-24px.
- **Prose + labels**: system sans (`-apple-system, "Segoe UI", Inter, sans-serif`).
  Microlabels are 10-11px uppercase, `letter-spacing: .12em`, `--ink-dim`, the
  "instrument label" register that makes a control room read as engineered.
- No webfont downloads: the demo records offline; system stacks cannot flake mid-take.

Scale contrast is deliberate: hero margin numeral roughly 4x the microlabel size.
Spacing rhythm: a 4px base grid, but section gaps (20/28px) breathe more than
intra-panel gaps (8/12px), intentional rhythm, not uniform padding.

## Surfaces (what each one has to communicate)

1. **Connection countdown board.** One row per connection: verdict rail (4px color
   edge), box-group + hero container id, cut-off clock (T-minus vs world as-of),
   P90 margin bar (zero-line marked; negative margin extends left in `--bad`), margin
   numeral. AT_RISK rows get the warm wash + pulsing rail. This is where a margin
   of 41 minutes becomes 101.
2. **Approval card.** The frozen `approval_card.json` schema rendered as a decision
   instrument: tier + risk chips, confidence dial (overall + per-field bars + basis
   line), **editable plan steps** (editable:true rows are inputs), options-considered
   with binding constraints printed on rejected options, justification textarea that
   hard-gates the Approve button when `justification_required`, deny-after countdown.
3. **Trace timeline.** The ledger replayed, newest last, one line per event: seq,
   ts, actor chip (llm / tool / rule / human, four distinct chip styles), action,
   badges from trace-native labels (DEGRADED_TO_ADVISORY / RECOVERED / DENY_BY_DEFAULT
   / ESCALATED / HELD / failure). `model_rationale` events render as an indented,
   violet-tinted "rationale" band with the RATIONALE_NOT_AUDIT_RECORD label, visually
   separated from audit events (MGF footnote 27 made visible). Chain-verified state
   shown at the head of the timeline.
4. **Governance tiles.** Denominators or it does not ship: override rate "N=…",
   approval response time, seeded-wrong-recommendation catch rate (the measured probe
   run with its own denominator; this ledger's own probe count is reported beside it and
   never merged into it), tokens **measured** vs dollars **imputed (labelled, dated
   basis)**, per-tier hit counters (rules / local / frontier).
5. **Controls strip.** Exactly ONE fault control (carrier-schedule tool kill switch,
   a physical-looking toggle with an armed state) + the replay-mode switch (LIVE vs
   REPLAY ledger source). Nothing else is operable from the page.
6. **Wall clock.** A real SGT wall clock, top right, mono, seconds ticking, so a
   stalled page is visible at a glance. World as-of is shown separately and
   labelled SYNTHETIC.

## States

Empty, loading, and error are designed, not defaulted: skeleton shimmer rows while
loading; "no cards awaiting review" with a one-line explanation in the approvals
rail; an offline banner if the API stops answering; a broken-chain state in the
timeline (`--bad` head) because tamper-evidence is a demo beat.

## Motion

Compositor-friendly only (`transform`/`opacity`): the AT_RISK rail pulses at 2s,
margin bars ease-out on change (300ms), new trace lines fade in 150ms, card arrival
slides 8px up. `prefers-reduced-motion` disables all of it.

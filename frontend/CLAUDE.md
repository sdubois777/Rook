# Rook — Frontend — Directory Guide

> Auto-loads when working under `frontend/`. Moved out of the root `CLAUDE.md`
> (July 2026).

---

## Frontend Responsive / Mobile Conventions

Mobile use case = pre-draft PREP and REVIEW (browse/research), NOT live drafting
(live drafting needs the desktop browser extension). Presentation layer only —
responsive work never changes logic, stores, data fetching, WS, or contracts.

- **Mobile-first.** Base classes target small phones; layer up with `sm:`/`md:`/
  `lg:` prefixes. Tailwind v4 (CSS-first, default breakpoints; v4 has native
  `@container` — use it for components in variable-width slots, viewport
  breakpoints elsewhere).
- **Desktop = the `lg` (≥1024px) tier and MUST NOT regress** — it is the approved
  design and must render pixel-equivalent to before at ≥1024px. Add smaller
  styles beneath it; never strip the desktop classes (move them behind `lg:`).
- **Device tiers:** base <640 (phones) · `sm` 640 (large/landscape phone) ·
  `md` 768 (tablet portrait) · **`lg` 1024 = approved desktop** · `xl` 1280.
- **≥44px touch targets** for interactive elements on mobile (`min-h-11` etc.),
  reverted at `lg` so desktop density is unchanged.
- **Dense data views** (DraftBoard, Teams) stay div-grids for now (see backlog);
  pick the responsive pattern PER VIEW — pinned-player-column horizontal scroll,
  breakpoint column-hiding, or row→card — not one blanket transform. On the
  DraftBoard, `adp_diff` and the snake flags (TARGET/SLEEPER/VALUE/REACH) are the
  core product signal and must ALWAYS stay visible on mobile; hide raw/duplicate
  rank or PPR cells instead. New tables use semantic `<table>` w/ `th scope`.

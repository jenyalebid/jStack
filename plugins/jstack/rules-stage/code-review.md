---
paths:
  - "**/*.swift"
---

# iOS Code Review Checklist

Auto-loaded when reviewing iOS code. Combines with `ios-design-ethos.md` (the principles being checked) and project-specific AGENTS.md files (the architecture).

## Per-file checklist

### Structure
- File placed in the correct type folder (Screens/, Sheets/, Components/, Modifiers/, Styles/, Services/, Forms/, Support/)?
- File naming follows convention (`*Screen`, `*Sheet`, `*View`, `*Modifier`, `*Style`, `*EditView`)?
- File size < ~150 lines without justification?
- New models/enums in the correct subfolder?

### Design compliance
- State flows through environment, not prop drilling?
- Using JSwiftUI components where they exist (`PrimaryActionButtonStyle`, `DismissButton`, `FlowLayout`, `ProgressCircle`, `PresentationController`)?
- Animations use spring constants (`response: 0.6, dampingFraction: 0.8`), not linear?
- Every tap has feedback (haptic, scale, visual)?
- Text scales with Dynamic Type — semantic (`.body`/`.headline`) or relative (`.system(.title2, design:, weight:)`) fonts, not fixed `.system(size:)`? Fixed sizing flagged unless it's decorative display type with a stated reason.
- iOS 26 adaptation at modifier level, not scattered version checks?
- Sheets follow `ios-sheets.md` rules?
- Toolbar buttons via `NavigationStack` + `ToolbarItem(placement:)`, never hand-rolled `HStack`?
- "Dismiss X, present Y" sequential via `.onDismiss` callback chain, never stacked?

### Architecture
- No imperative UI hacks (manual refresh, force-unwraps, notification-driven updates)?
- Extensions in separate files, not bloating existing ones?
- Modifier ordering: Layout → Appearance → Effects → Interaction → Environment?
- Correct data stack for this project (project AGENTS.md is canonical)?
- No `AnyView` — use generic `Content`/`Card` or `@ViewBuilder` switch dispatch?

### Package awareness
- Did the change rebuild something a shared package already provides?
- Did it create a local workaround for a stale shared component?
- Is there a new cross-app pattern worth extracting into a shared package?

### Numeric verification (when touching formulas / curves / multipliers / progression)
- Run the formula at representative levels (1, 10, 30, 50, 99)
- Print a table showing input → output
- Flag anything flat, broken, or unintuitive

### Verification
- Layout checked is NOT verified. Verify = clicked the actual button, submitted the actual form. State so explicitly if only layout was checked.
- Never claim "verified" / "looks good" while the screenshot contradicts it.

## Per-file verdict

PASS — or — ISSUE: `{specific violation}` → `{fix}`. Then fix it inline or create a task. No "noted in journal" without action.

## Accessibility IDs

Interactive elements should carry stable accessibility identifiers so UI tests can target them deterministically. A convention worth borrowing if your project doesn't already have one:
- `screen_*` — screen root identifiers
- `{screen}_tap_*` — tap targets
- `{screen}_nav_*` — navigation triggers
- `{screen}_item_*` — list/collection items
- `tab_*` — tab bar items

Missing IDs on new interactive code is a regression in projects that have adopted the convention.

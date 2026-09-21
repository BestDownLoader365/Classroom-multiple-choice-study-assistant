# Frontend Visual & Interaction Design Specification

This document defines a standalone visual language for new interfaces. It specifies aesthetic character, color relationships, typography, layout, component proportions, interaction states, motion, and rules for extending the system. It does not prescribe a product structure, content model, navigation map, or technology stack.

## How to read this document

Three kinds of content live here, and they have different authority:

| Kind | What it means | Where |
|---|---|---|
| **Normative design guidance** | The rules a new page or component must follow: color relationships, typography roles, spacing, motion character, accessibility. Changing them is a design decision. | The DNA, color, typography, layout, spacing, hierarchy, shape, interaction, motion, responsive, form-composition, feedback, accessibility and aesthetic-constraint sections |
| **Currently implemented components** | Recipes that ship in `app/templates/` + `app/static/css/style.css` today, with the class names and markup the application actually uses. | Component recipes marked “Implemented”, plus [Implementation index](#implementation-index) |
| **Extension specification (not implemented)** | Component recipes specified for future work. They follow this design language but **no template, CSS class, or JavaScript for them exists in this repository yet** — do not reference them in markup. | Component recipes marked “Extension spec” |

Nothing in this document is a promise about the current repository. When a recipe and the code disagree, the code and its tests win.

## Implementation index

What actually ships today:

* Templates (`app/templates/`): `base.html`, `auth.html`, `courses.html`, `home.html`, `quiz_setup.html`, `quiz.html`, `mistakes.html`, `review` (rendered through `quiz.html`), `dashboard.html`, `stats.html`, `exam_setup.html`, `exam.html`, `exam_report.html`, `glossary.html`, `error.html`.
* Main CSS classes (`app/static/css/style.css`): page shell `page-shell` / `page-shell-workspace` / `content-stack`; headers `site-header` / `brand` / `header-actions` / `header-link`; course navigation `course-switcher` / `course-current` / `course-menu` / `course-menu-trigger` / `course-menu-list` / `course-menu-link` / `course-status`; surfaces `question-card` / `summary-card` / `auth-card` / `result-card` / `table-card` / `error-card`; data `data-table` / `table-wrap` / `metric-strip` / `report-metrics` / `trend-chart` / `mastery-bar` / `mastery-cell`; task continuation `task-band` / `task-progress` / `exam-resume`; controls `button` (`button-primary` / `button-secondary` / `button-ghost` / `button-danger` / `button-large`) / `field-group` / `size-picker-*` / `chapter-picker` / `chapter-option` / `option-row`; feedback `feedback` / `feedback-correct` / `feedback-wrong` / `flash` / `status` (`status-mastered` / `status-progressing` / `status-good` / `status-weak` / `status-pending` / `status-not-started`) / `empty-state`; glossary `glossary-grid` / `glossary-card` / `glossary-popover` / `glossary-reveal`.
* `app.js` behavior: enable/disable the answer submit button and selection hint; confirm dialogs on restart/reset/submit forms; the bilingual toggle backed by `localStorage["mcq-bilingual"]`; focus management for the feedback region; the shared `initializePicker` listbox behavior for the quiz-size, mistake-filter and glossary-category pickers (hidden input + server-rendered option buttons); the global/per-source chapter checkbox synchronization (the “all chapters” box mirrors whether any specific chapter is ticked, and each per-source group toggle goes indeterminate when only part of that source is selected); the exam selection hint; the exam countdown mirror.
* `glossary.js` behavior: longest-first literal term highlighting inside `data-glossary-highlight` containers, the keyboard-accessible definition popover, glossary search, category filtering, visible counts, empty state, and Chinese reveal.
* `data-*` hooks in use: `data-answer-form`, `data-submit-answer`, `data-selection-hint`, `data-feedback`, `data-bilingual-toggle`, `data-confirm` / `data-confirm-restart` / `data-confirm-reset`, `data-picker` / `data-picker-value` / `data-picker-option` / `data-picker-submit-on-change` / `data-select-picker` / `data-select-menu` / `data-select-current` / `data-value`, `data-all-chapters` / `data-chapter-group` / `data-chapter-group-all` / `data-specific-chapter`, `data-exam-answer-form` / `data-exam-hint` / `data-exam-submit-form` / `data-exam-timer` / `data-remaining-seconds`, `data-course-id` / `data-current-course`, `data-glossary-highlight` / `data-glossary-page` / `data-glossary-grid` / `data-glossary-card` / `data-glossary-term` / `data-glossary-search` / `data-glossary-reveal` / `data-glossary-skip` / `data-glossary-visible-count` / `data-glossary-empty` / `data-term-id`, `data-option-row` / `data-correct-option` / `data-selected-option` / `data-corrected-question`, `data-table` / `data-label`, `data-error-course-id`.
* Responsive breakpoints: `max-width: 820px` (header becomes two rows, the course picker takes a full-width row, the course grid collapses to one column, inner grids stack) and `max-width: 640px` (low-priority labels such as `当前课程` are dropped and long titles truncate). `prefers-reduced-motion: reduce` removes decorative transitions.

# Design DNA

The visual language is restrained, editorial, spacious, sharp, and predominantly flat.

Hierarchy comes from typography, whitespace, text contrast, and fine rules before it comes from color or elevation. Warm neutral surfaces create the feeling of paper rather than a cold digital canvas. Serif typography gives headings and reading content an authored, publication-like quality; sans-serif typography keeps controls clear; compact monospaced labels add a precise technical counterpoint.

The interface should feel quiet but deliberate:

- Use warm off-whites instead of pure white.
- Use near-black ink instead of absolute black.
- Keep the palette neutral most of the time.
- Reserve a muted brick-red accent for primary actions, selection, progress, and important emphasis.
- Prefer thin dividers and dark top rules to card shadows.
- Keep corners square or almost square.
- Use generous space between sections and moderate space inside controls.
- Make interaction feedback immediate and low-amplitude.
- Avoid decorative motion, ornamental icons, glossy surfaces, and deeply nested containers.

The distinctive signature is the combination of a warm paper canvas, strong serif hierarchy, tiny monospaced metadata, thin ink-colored rules, restrained brick-red emphasis, and almost no ambient shadow.

## Design in one sentence

Design every interface like a carefully typeset contemporary publication: warm, structured, readable, border-led, and selectively energized by a single earthy accent.

# Visual Similarity Priorities

Visual similarity means reproducing relationships and habits, not copying a layout or set of screens. When full fidelity is not possible, preserve these qualities in order:

1. Color relationships: warm neutrals dominate; accent remains scarce.
2. Typography hierarchy: editorial serif, functional sans, precise mono metadata.
3. Spacing density: spacious sections, moderate control density, readable content.
4. Shape language: square surfaces and 0–2px control rounding.
5. Surface treatment: borders and tonal shifts before shadows.
6. Component proportions: 46px default controls and generous click targets.
7. Input proportions: quiet, underline-led fields with clear labels.
8. Dropdown treatment: paper-colored, sharp, compact, and keyboard-complete.
9. Interaction states: subtle color and background changes with a visible focus ring.
10. Motion character: fast, functional, and physically small.

Maintaining the overall visual character is more important than reproducing any single isolated numeric value.

# Color System

## Palette character

The palette is low-saturation and warm. Neutral colors should occupy at least 85% of a typical interface. The accent and semantic colors are functional signals, not decoration. Large saturated panels, colorful gradients, and multiple competing brand colors are outside this system.

## Neutral foundation

| Token | Value | Role |
| --- | --- | --- |
| `surface.canvas` | `#F1EEE6` | Primary canvas and application background |
| `surface.subtle` | `#E5DFD2` | Recessed areas, quiet placeholders, restrained loading fills |
| `surface.primary` | `#FAF8F2` | Cards, grouped content, tables, reading surfaces |
| `surface.raised` | `#FFFDF8` | Floating panels, highlighted feedback, neutral hover surface |
| `surface.hover` | `#FFFDF8` | Neutral hover state on a primary surface |
| `surface.selected` | `#F2E2DA` | Selected or softly emphasized state |

Use small luminance steps. A surface should feel separated without looking like a bright white tile placed on a gray background.

## Text

| Token | Value | Role |
| --- | --- | --- |
| `text.primary` | `#20231F` | Headings, primary values, body copy |
| `text.secondary` | `#51564F` | Supporting copy, descriptions, secondary values |
| `text.muted` | `#747970` | Nonessential metadata and low-priority context |
| `text.disabled` | `#94988F` | Disabled labels and unavailable controls |
| `text.inverse` | `#FFFDF8` | Text on dark accent or semantic fills |

`text.primary` on `surface.canvas` has approximately 13.7:1 contrast. `text.secondary` has approximately 6.5:1. `text.muted` has approximately 3.8:1 and must therefore be limited to nonessential text, large text, or contexts where a stronger accessible name is also present. Use `text.secondary` for small labels that carry essential meaning.

## Borders

| Token | Value | Role |
| --- | --- | --- |
| `border.subtle` | `#D1CCBF` | Dividers, row rules, quiet grouping |
| `border.default` | `#AAA598` | Controls, floating panels, emphasized boundaries |
| `border.strong` | `#20231F` | Major top rules and high-level structural separation |
| `border.focus` | `rgba(161, 68, 47, 0.26)` | Three-pixel focus ring |

Prefer one-dimensional rules—top, bottom, or left borders—when a full rectangular boundary is unnecessary.

## Accent

| Token | Value | Role |
| --- | --- | --- |
| `accent.primary` | `#A1442F` | Primary actions, selection, progress, key emphasis |
| `accent.hover` | `#783122` | Hovered primary actions and emphasized selected text |
| `accent.active` | `#783122` | Pressed primary actions; pair with a non-layout-shifting active cue |
| `accent.subtle` | `#F2E2DA` | Selected backgrounds and low-intensity emphasis |
| `accent.focus` | `rgba(161, 68, 47, 0.26)` | Focus ring |

Accent text on the canvas has approximately 5.35:1 contrast. Inverse text on the accent fill has approximately 6.1:1. Use the accent for action, current selection, progress, or meaningful emphasis. Do not use it merely to make a section more colorful.

## Semantic colors

| Family | Strong | Subtle | Border | Use |
| --- | --- | --- | --- | --- |
| Positive | `#356B55` | `#E4EEE7` | `#B7CBBD` | Success, completion, valid outcomes |
| Warning | `#8A642C` | `#F2EAD8` | `#D7C49D` | Caution, pending attention, partial outcomes |
| Destructive | `#9D3C35` | `#F3E2DE` | `#D8B3AD` | Errors, irreversible actions, invalid outcomes |
| Information | `#A1442F` | `#F2E2DA` | `#A1442F` | Important neutral notices that need emphasis |

Information deliberately reuses the accent family to keep the palette narrow. Semantic color must always be paired with a label, icon, marker, or explanatory text. Never encode status through color alone.

## Color distribution

- Canvas and neutral surfaces: 70–85%.
- Primary and secondary text: 10–20%.
- Borders and dividers: 5–10%.
- Accent and semantic fills: usually below 5%.
- Use at most one strong semantic family in a small local region unless comparison is essential.

# Typography System

## Font roles

### Display and reading

Use an old-style serif with strong Latin and CJK fallbacks:

`Iowan Old Style, Palatino Linotype, Book Antiqua, Noto Serif SC, Songti SC, Georgia, serif`

Use it for major headings, section headings, long-form reading content, prominent values, and selected high-emphasis control values.

### Interface

Use a neutral humanist or neo-grotesque sans-serif:

`Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, Microsoft YaHei, sans-serif`

Use it for controls, navigation, helper text, compact body copy, and interactive labels.

### Data and metadata

Use a compact monospaced family:

`SFMono-Regular, Consolas, Liberation Mono, monospace`

Use it only for short labels, metadata, indexes, table headings, compact counts, and machine-like context. Do not use it for paragraphs.

## Type scale

| Style | Size | Line height | Weight | Tracking |
| --- | --- | --- | --- | --- |
| Display XL | `clamp(44px, 7vw, 92px)` | `0.98` | `500` | `-0.055em` |
| Display L | `clamp(42px, 6vw, 80px)` | `1.00` | `500` | `-0.05em` |
| Display M | `clamp(26px, 3vw, 36px)` | `1.12` | `500` | `-0.03em` |
| Heading S | `22px` | `1.25` | `500–600` | `-0.02em` |
| Reading L | `clamp(25px, 3.4vw, 39px)` | `1.32` | `500` | `-0.03em` |
| Body L | `16px` | `1.75` | `400` | `0` |
| Body | `16px` | `1.60` | `400` | `0` |
| Body S | `13–14px` | `1.55` | `400–500` | `0` |
| Label | `12px` | `1.35` | `650` | `0.04em` |
| Metadata | `10px` | `1.40` | `600` | `0.08em` |
| Button | `16px` | `1.25` | `650–700` | `0` |
| Numeric display | `34–61px` | `1.00` | `400` | `-0.055em` |

If variable weights such as 650 or 680 are unavailable, use 600 for secondary controls and 700 for primary controls. Do not rely on synthetic weights.

## Typography rules

- Keep major headings medium-weight rather than bold.
- Balance major headings across lines when the rendering environment supports it.
- Use tight negative tracking only at display sizes.
- Use tabular numerals for aligned counts, measurements, and table values.
- Use uppercase metadata only for short Latin labels. Preserve tracking and weight for scripts without case.
- Use serif type to signal reading or editorial hierarchy, not to style every UI string.
- Keep long body text between 55 and 75 characters per line where possible.
- Never use muted 10–12px text for critical instructions.
- Minimal or restrained interfaces must not achieve refinement by shrinking text. Keep normal product copy around `15–16px`, important labels and supporting descriptions around `13–14px`, and reserve `10–12px` for genuinely low-priority metadata.
- If a layout only works after shrinking normal interface copy below a comfortable reading size, fix the layout rather than the typography.

## Display typography guardrails

Display typography signals hierarchy; it is not a default treatment for every page title or section.

- Reserve Display XL and Display L for genuinely introductory pages, editorial or marketing openings, and rare major moments.
- Prefer Display M or Heading S on frequently used product surfaces such as workspaces, dashboards, editors, learning tools, and administration interfaces.
- Allow only one clearly dominant serif heading in a viewport. Section headings must not compete with the page heading.
- Do not repeat large serif headings merely to give every region an editorial character.
- Do not automatically pair a primary-language title with an italic English translation. Use bilingual titles only when the product has a real bilingual requirement.

# Layout Philosophy

## Application shell

- Center the primary content region.
- Use a maximum content width of approximately `1180px`.
- Use responsive horizontal gutters: `18px` on small screens, `22–36px` on intermediate screens, and up to `72px` on wide screens.
- Use `48–96px` of space above the first major section and approximately `56–88px` below the final section.
- Keep the top toolbar between `64px` and `66px` high.
- Separate the toolbar from content with a single subtle bottom border; do not use a shadow.
- The toolbar may take a second row on narrow screens when it carries a course picker (see "Course selector and course list"): one row of `64–66px` is the single-row target, not a constraint that justifies pushing an action onto a wrapped line.
- The global content maximum is a default, not a hard limit for every workspace. Learning workspaces, editors, operations tools, dashboards, and data-heavy surfaces may use a wider composition when local text measure remains controlled and hierarchy stays clear.
- A wide canvas does not require a narrow content island. Use deliberate desktop gutters and useful viewport coverage without stretching short copy into long lines.

## Content width

- Use `760–980px` for reading-focused or sequential content.
- Use the full `1180px` width only for multi-column layouts or wide data.
- Keep modal content near `520px` by default and no wider than `720px` without a clear need.
- Do not stretch short forms or short text across the full content width.

## Section structure

- Separate major sections with `30–56px` vertical space.
- Use a title block with an optional small label, a large heading, and secondary copy.
- Place primary actions opposite the title only when space permits; stack them below the title on narrow screens.
- Use a 2px dark top rule to introduce an important content region.
- Use 1px internal dividers rather than nested card backgrounds.

## Task-first product composition

Editorial visual language does not imply landing-page composition. Repeated-use, task-oriented interfaces should usually be task-first rather than hero-first.

- Surface the user's current state, unfinished work, next action, and directly relevant status before brand expression.
- Keep identity and context visible, but reduce oversized introductory titles when users already understand where they are.
- Do not use a hero, slogan, or decorative supporting copy by default on a workspace, dashboard, learning tool, editor, question bank, or administration surface.
- Spend large areas of the first viewport on the primary task only when the task itself needs that room.
- Do not add content merely to balance a composition.

## Columns and rails

- Prefer asymmetrical editorial columns over equal card grids when one region is clearly primary.
- For a main/secondary split, use ratios near `1.5:0.75` or `2.2:0.8`.
- If a persistent side rail is required, keep it approximately `240–280px`, separate it with a subtle border, and avoid a raised container.
- Collapse secondary columns below approximately `820px` unless the content remains comfortably readable.
- Purposeful asymmetry must reflect information priority, not decorate the composition. Do not default to three equal columns, four equal cards, or a symmetric dashboard grid unless the content has genuinely equal weight.

## Divider budget

Rules are structural, not decorative. Prefer spacing before rules and local rules before full-content-width rules. When whitespace already communicates grouping, do not add another divider. Avoid repeating full-width horizontal rules throughout a single viewport merely to create an editorial appearance.

## Forms and lists

- Place form labels above controls by default.
- Use `8px` between label and control and `24px` between fields.
- Use list rows at least `44px` high; use `62–66px` when a row contains substantial text.
- Keep dense table cells around `19px 22px` on large screens and `14px` on small screens.
- Avoid enclosing each list row in a separate rounded card.

## Panel usage

Use panels sparingly. A page should usually have a canvas, one primary surface hierarchy, and optional floating surfaces. Avoid card-on-card-on-card composition. Distinguish adjacent regions with spacing or rules before adding another background.

# Spacing and Density

## Spacing scale

| Token | Value | Typical role |
| --- | --- | --- |
| `space.1` | `4px` | Micro gap, paired label lines |
| `space.2` | `8px` | Label/control gap, compact actions |
| `space.3` | `12px` | Grid gap, compact padding |
| `space.4` | `16px` | Standard internal separation |
| `space.5` | `20px` | Standard horizontal control padding |
| `space.6` | `24px` | Field spacing, cell padding, card padding |
| `space.8` | `32px` | Panel padding, small section gap |
| `space.10` | `40px` | Medium region separation |
| `space.12` | `48px` | Large section separation |
| `space.14` | `56px` | Spacious section separation |
| `space.16` | `64px` | Major column or region gap |
| `space.18` | `72px` | Prominent control height, wide gutter |
| `space.22` | `88px` | Page-end spacing |
| `space.24` | `96px` | Maximum introductory spacing |

Use this scale by default. Values between scale steps are permitted only for optical alignment, typographic baseline correction, or matching an adjacent established component.

## Density character

The outer layout is spacious; controls and data regions are moderately dense. Do not confuse spaciousness with oversized components. Large gaps belong between concepts, while controls should remain compact and precise.

Spacious does not mean empty. Productive whitespace supports grouping, hierarchy, and readability; unused whitespace leaves the primary task visually stranded. A spacious interface should still make confident use of the viewport.

# Hierarchy System

Create hierarchy in this order:

1. Typography size and family.
2. Whitespace and grouping.
3. Text contrast.
4. Font weight.
5. Surface luminance.
6. Border strength and direction.
7. Accent or semantic color.
8. Shadow.

Do not use shadow as the primary hierarchy mechanism. Accent color should remain scarce and primarily communicate action, selection, progress, or important state. A layout that remains understandable in grayscale is structurally sound; color should sharpen meaning rather than create it from nothing.

## Hierarchy budget

Each viewport has a visual hierarchy budget. An oversized page heading, oversized metrics, a prominent primary action, multiple large section headings, and a decorative slogan all compete for the same limited attention. Usually only one element or task group should act as the dominant anchor. Reduce or remove the others until the next action remains obvious in grayscale.

# Shape and Elevation

## Radius

| Token | Value | Use |
| --- | --- | --- |
| `radius.none` | `0` | Cards, tables, menus, popovers, panels |
| `radius.control` | `2px` | Buttons and compact controls |
| `radius.round` | `999px` | Avatars, status dots, switch tracks only |

Square corners are the default. Rounded shapes are semantic exceptions, not decoration.

## Border

- Standard divider: `1px solid border.subtle`.
- Control boundary: `1px solid border.default`.
- Major rule: `2px solid border.strong`.
- Semantic emphasis: `2–3px` left or top border using the relevant semantic color.
- Focus: `3px solid accent.focus` with `2–3px` offset.

## Shadow

| Token | Value | Use |
| --- | --- | --- |
| `shadow.none` | `none` | All in-flow surfaces |
| `shadow.floating` | `0 16px 42px rgba(32, 35, 31, 0.16)` | Popovers, menus that must overlap complex content, modals, floating toasts |

Do not apply floating shadow to ordinary cards, toolbars, inputs, or tables.

# Interaction State Grammar

## Default

Keep controls neutral unless they are the primary action or represent an active selection.

## Hover

Use one or two of these cues:

- Shift text or border from neutral to accent.
- Change the surface to `surface.hover` or `accent.subtle`.
- Move a selectable card upward by no more than `1px`.
- Increase a row's leading inset by no more than `6px`.

Do not combine color, shadow, scale, and movement on the same hover state.

## Active

Use the hover color at a slightly stronger perceived intensity. Avoid layout shifts. A `translateY(1px)` press cue is acceptable for isolated buttons but should not be used in dense rows.

## Focus

Use a clearly visible `3px` translucent accent ring with `2–3px` offset. Focus must not depend on a color change alone. Apply the ring only for keyboard-visible focus when the platform supports that distinction.

Do not remove useful interaction merely to make an interface appear more minimal. Preserve functional hover, active, focus, directional, and state-transition feedback when it clarifies affordance and does not compete with reading.

## Disabled

Use `opacity: 0.42`, a not-allowed cursor where appropriate, and remove the element from action logic. Preserve its dimensions. Disabled elements must not retain hover or active effects.

## Loading

Preserve component width and layout. Disable repeated activation, change the label to an active verb where useful, and optionally add a `16px` current-color spinner. Announce asynchronous state programmatically. Do not replace an entire region with a spinner when a localized loading state is sufficient.

# Motion System

## Motion character

Motion should feel immediate and functional rather than decorative. Most transitions are short and low-amplitude. Components should avoid large translations, springy effects, exaggerated scaling, overshoot, parallax, and background animation.

Motion explains state change, spatial origin, or progress. It should not compete with reading.

Restraint applies to motion amplitude, not to the existence of interaction feedback. Decorative motion and functional motion are different; small purposeful cues such as directional movement, active feedback, and progress transitions may remain.

## Duration

| Token | Duration | Use |
| --- | --- | --- |
| `motion.instant` | `0ms` | Synchronous content/state replacement |
| `motion.micro` | `100ms` | Menu-item and compact hover feedback |
| `motion.fast` | `140ms` | Button, border, text, and background transitions |
| `motion.standard` | `180ms` | Dropdown, popover, accordion, tab indicator |
| `motion.slow` | `220ms` | Progress changes and modal transitions |

## Easing

| Token | Value | Use |
| --- | --- | --- |
| `ease.standard` | `ease` | Color, border, simple opacity |
| `ease.linear` | `linear` | Short floating-surface opacity |
| `ease.emphasized` | `cubic-bezier(.22, .72, .28, 1)` | Small translation and scale |
| `ease.progress` | `cubic-bezier(.2, .8, .4, 1)` | Progress indicators |

## Component motion

- Hover: color/background/border over `100–140ms`; movement no more than `1px`.
- Dropdown: opacity plus `7px` vertical translation and scale from `.985` to `1` over `130–190ms`.
- Popover: opacity plus `5px` vertical translation over `130–160ms`.
- Modal: opacity plus `5px` translation and scale from `.985` to `1` over `180–220ms`.
- Tooltip: opacity plus `2–3px` translation over `100–140ms` after its show delay.
- Tab indicator: width/position over `180ms`; avoid spring behavior.
- Accordion: height or grid-track expansion plus opacity over `180–220ms`; do not rotate content dramatically.
- Toast: opacity plus `8px` translation over `180ms`; do not bounce.
- Loading: spinner rotation may loop linearly around `700–900ms`; skeleton pulse may loop around `1000–1400ms` with very low contrast.

When reduced motion is requested, disable smooth scrolling, collapse transitions to approximately `1ms`, remove transforms, and preserve all state changes without animation.

# Responsive Behavior

Use two primary adaptation thresholds:

- Around `820px`: collapse major editorial columns, reduce large gaps, and move opposing actions below headings.
- Around `640px`: stack form controls, make primary actions full-width, simplify dense navigation, and convert record-like tables into labeled blocks when horizontal comparison is not essential.

Responsive design must change composition, not merely shrink typography.

- Wide desktop layouts should use available canvas for composition instead of leaving a small centered island, while keeping short text within controlled local measures.
- Tablet and mobile layouts should change columns, action arrangement, and row flow while preserving readable text, touch targets, and task priority. Responsive design is not a miniature desktop layout.

- Keep small-screen horizontal gutters near `18px`.
- Reduce large panel padding to approximately `20px`.
- Maintain minimum control and menu-item heights.
- Allow long tab lists or toolbars to scroll horizontally rather than compressing labels into unreadable widths.
- Use sticky bottom placement selectively for a single high-frequency form action, with at least `10px` viewport clearance.
- Reflow two-column summaries into stacked label/value groups when content would collide.
- Keep modals within `18px` of each viewport edge and cap their height to preserve a scrollable interior.
- Hide low-priority toolbar metadata before hiding actions or accessible labels.
- In a wrapping toolbar, decide explicitly which items share the first row: a
  full-width picker that may shrink must still wrap on its own (`flex: 1 0 100%`
  with an explicit `order`), otherwise it squeezes onto the first row, forces the
  account actions onto a second line, and the most important control appears to
  move for no reason. Actions (`退出登录`) stay on the first row; metadata labels
  (`当前课程`) are the first thing to drop on the narrowest screens.
- Align repeated action buttons across a card row: pin the action to the bottom
  of a stretched card (`margin-top: auto`) rather than letting copy length decide
  the button's vertical position.

# Component Recipes

Each recipe below is marked either **Implemented** (present in `app/templates/` + `app/static/css/style.css` today, with the class names the application uses) or **Extension spec** (specified for future work; nothing in this repository implements it yet, so do not reference it from markup). See [Implementation index](#implementation-index) for the full inventory.

## Button — Implemented

Implemented as `.button` plus the variants `.button-primary`, `.button-secondary`, `.button-ghost`, `.button-danger` and the size modifier `.button-large`, together with the text-only affordance `.header-link` and the small square/circular controls `.agenda-arrow`, `.restart-button`, `.submit-answer` and `.glossary-popover-close`.

- **Anatomy:** Container, text label, optional leading or trailing icon, optional secondary line, optional loading indicator.
- **Dimensions:** Default min-height `46px`; compact `40px`; prominent `72px`. Default padding `12px 20px`; compact `8px 12px`; prominent `17px 22px`. Radius `2px`; content gap `14px`.
- **Visual style:** Primary uses accent fill and inverse text. Secondary uses transparent fill and default border. Ghost uses transparent fill and a bottom rule. Destructive uses transparent fill and destructive border/text.
- **Typography:** Interface type, `16px`, weight `650–700`. Secondary line `11px`, weight `500`, muted.
- **Spacing:** Center ordinary labels. For two-line prominent buttons, left-align the copy and distribute supporting content deliberately.
- **Default state:** Use the variant treatment without shadow.
- **Hover:** Primary darkens; secondary changes border/text to accent; ghost changes text/rule to primary ink; destructive fills with destructive color.
- **Active:** Retain hover color and optionally move down `1px`; do not resize.
- **Focus:** Three-pixel accent focus ring with three-pixel offset.
- **Disabled:** Opacity `.42`, no hover/active behavior, preserved dimensions.
- **Loading:** Preserve width, disable activation, use active-verb text and an optional `16px` spinner.
- **Motion:** Color, border, and background `140ms ease`; optional press transform `100ms`.
- **Usage rules:** Use one primary button per local action group. Use ghost for low-priority navigation or cancellation. Confirm irreversible destructive actions.

## Metric Summary — Implemented

Shipped as .metric-strip, .report-metrics, .result-stats and .report-score.

- **Purpose:** Compactly communicate counts, progress state, or lightweight product status without implying a full analytics dashboard.
- **Anatomy:** Short label, value, and optional concise qualifier. Use tabular numerals where alignment benefits comparison.
- **Composition:** Keep supporting metrics compact and close to the content they explain. Use inline groups, simple label/value rows, or sparse local dividers before creating independent cards.
- **Hierarchy:** Metrics must not compete with the primary task. Do not enlarge zero values merely to preserve symmetry.
- **Usage rules:** Do not automatically use equal three-column KPI layouts. Give metrics separate surfaces only when they require genuinely independent interaction, explanation, or comparison.

## Task Continuation / Resume Block — Implemented

Shipped as .task-band with .task-progress / .task-position / .task-actions, and .exam-resume.

- **Purpose:** Resume an unfinished learning session, draft, form, onboarding sequence, checkout, workflow, or other long-running task.
- **Anatomy:** Context, current state or progress, optional progress indicator, one explicit primary continuation action, and an optional secondary restart or abandon action.
- **Hierarchy:** Make the continuation action the local primary action. Keep restart or abandon visually secondary and confirm it when progress would be lost.
- **Semantics:** Separate status from action. A phrase such as “Step 5 of 10” describes state; “Continue” invokes an action.
- **Usage rules:** Do not combine status copy and the primary action into one ambiguous oversized CTA. Progress indicators must reflect completed work rather than merely echoing the current item number.

## Icon Button — Implemented

Shipped as .agenda-arrow, .restart-button, .submit-answer and .glossary-popover-close.

- **Anatomy:** Button container, one icon, accessible name, optional tooltip.
- **Dimensions:** Default `40×40px`; comfortable `46×46px`; icon `16px`; close glyph may be `20–22px`. Radius `2px`.
- **Visual style:** Transparent by default; use a subtle border only when the boundary is otherwise unclear.
- **Typography:** No visible text; the accessible name is mandatory.
- **Spacing:** Center the icon exactly. Do not reduce the hit target to the glyph size.
- **Default state:** Muted or secondary icon color.
- **Hover:** Accent or primary icon color with `accent.subtle` only when stronger feedback is needed.
- **Active:** Use accent-hover and optional `translateY(1px)`.
- **Focus:** Standard focus ring.
- **Disabled:** Opacity `.42`, noninteractive.
- **Loading:** Replace the glyph with a `16px` spinner while preserving the accessible name.
- **Motion:** Color/background `140ms`; tooltip follows its own recipe.
- **Usage rules:** Use for universally understood actions such as close or directional navigation. Prefer a labeled button when meaning could be ambiguous.

## Input — Implemented

Shipped as .field-group with its label/help/error structure; inputs are styled from the shared control tokens.

- **Anatomy:** Label, input control, optional helper text, optional error text, optional prefix/suffix.
- **Dimensions:** Min-height `46px`; compact search height `38–40px`; horizontal padding `0–8px`; vertical padding `8px`; prefix/suffix icon `16px`.
- **Visual style:** Transparent background, no enclosing shadow, and a `1px` default bottom border. Use a full rectangular border only when the input must be visually isolated.
- **Typography:** Interface type, `16px`; label `10–12px` metadata style; helper/error `12–13px` interface type.
- **Spacing:** Label-to-control `8px`; control-to-helper `6–8px`; field-to-field `24px`.
- **Default state:** Primary text, subtle placeholder, default border.
- **Hover:** Darken the bottom border to `border.default`; do not add a bright fill.
- **Active:** Same as focus once editing begins.
- **Focus:** Accent bottom border plus standard focus ring.
- **Disabled:** Disabled text, opacity `.42–.55`, no hover, preserve label readability.
- **Loading:** Add a trailing `16px` spinner only for async lookup or validation; do not block unrelated fields.
- **Motion:** Border and color `140ms ease`.
- **Usage rules:** Every input needs a visible or programmatic label. Connect helper/error text programmatically. Use invalid-state semantics for errors.

## Textarea — Extension spec

Not implemented in this repository yet: no template, class or script uses it. Treat the recipe below as a target for future work.

- **Anatomy:** Label, multiline field, optional character count, helper, and error.
- **Dimensions:** Min-height `120px`; padding `12px`; resize vertically by default; radius `0`.
- **Visual style:** Primary surface or transparent fill with a full `1px` default border. A single underline is insufficient for a large editable region.
- **Typography:** Interface type, `16px/1.6`; label and helper follow Input.
- **Spacing:** Same vertical rhythm as Input; place count opposite helper text.
- **Default state:** Default border, primary text.
- **Hover:** Border strengthens slightly.
- **Active:** No separate visual treatment beyond focus.
- **Focus:** Accent border and standard focus ring.
- **Disabled:** Disabled text, subdued surface, no resize interaction.
- **Loading:** Avoid loading inside a textarea; show save or validation state adjacent to it.
- **Motion:** Border/color `140ms`.
- **Usage rules:** Use for genuinely multiline content. Keep line length readable and avoid auto-growing without a sensible maximum.

## Select — Implemented

Shipped as the custom listbox picker (.size-picker-trigger / .size-picker-menu / .size-picker-option) and .chapter-picker; there is no native select element.

- **Anatomy:** Label, trigger, selected value or placeholder, optional leading icon, trailing chevron, value representation.
- **Dimensions:** Trigger min-height `38px` in compact rows or `46px` in standalone forms; right-side chevron reserve `32–36px`; icon `16px`.
- **Visual style:** Transparent warm background, no shadow, underline or surrounding structural rules, radius `0–2px`.
- **Typography:** Selected value may use Display type at `16–18px`; labels use metadata style.
- **Spacing:** Label gap `7–8px`; value/icon gap `10–12px`; chevron separated by at least `20px` from text.
- **Default state:** Primary text, muted placeholder, 8px chevron with approximately `1.5px` stroke.
- **Hover:** Strengthen text/border subtly; do not fill the trigger with a saturated color.
- **Active:** Rotate chevron from approximately `45deg` to `225deg` and expose the floating surface.
- **Focus:** Standard focus ring.
- **Disabled:** Disabled text and chevron, opacity `.42`, no opening behavior.
- **Loading:** Keep trigger size; replace chevron with a `16px` spinner and prevent opening until options are ready.
- **Motion:** Chevron `180ms ease.emphasized`; floating surface follows Dropdown.
- **Usage rules:** Use for choosing one value. The control must expose its name, expanded state, selected value, and related list semantics to assistive technology.

## Dropdown and Menu — Implemented

Shipped as a native details/summary disclosure plus the .size-picker-menu and .course-menu / .course-menu-list floating list.

### Anatomy

Trigger, floating surface, optional group label, menu items, separators, optional submenu trigger, optional scroll affordance.

### Trigger

- Height `38–46px`.
- Padding `0 4px` for an underline trigger or `8px 12px` for a bounded trigger.
- Border `0` inside a ruled field group; otherwise `1px solid border.default`.
- Radius `0–2px`; background `surface.canvas` or transparent.
- Typography `16–18px` Display for value selection, `14–16px` Interface for action menus.
- Chevron `8px` with `1.5px` stroke; general leading icons `16px`.
- Hover changes text/border only.
- Focus uses the standard three-pixel ring.

### Floating surface

- Background `surface.canvas` for selects or `surface.raised` for action menus.
- Border `1px solid border.default`; radius `0`; optional `2px` accent top rule for a high-emphasis panel.
- Use no shadow over simple backgrounds. Use `shadow.floating` only when overlap would otherwise be ambiguous.
- Internal padding `4px`.
- Offset `8px` from the trigger.
- Width matches the trigger by default; action menus may use `min-width: 180px` and size to content up to viewport limits.
- Maximum height `min(360px, viewport height - 120px)` with internal scrolling.
- Viewport gutter `12px`.

### Menu item

- Minimum row height `44px`; padding `10px 12px`.
- Interface or Display text `14–16px/1.35` according to trigger role.
- Leading icon `16px`; icon/text gap `10px`.
- Default is primary text on a transparent surface.
- Hover and focused states use accent-dark text on `accent.subtle`.
- Selected state uses accent fill with inverse text.
- Selected hover/focus uses accent-hover fill.
- Destructive item uses destructive text; hover uses destructive subtle background, not a solid fill.
- Disabled item uses disabled text, cannot receive selection, and is skipped by keyboard movement.
- Separator is `1px border.subtle` with `4px` vertical margin.

### Interaction

- Open on click or keyboard activation, never hover alone.
- Close on selection, Escape, outside pointer interaction, or Tab leaving the menu.
- Return focus to the trigger after selection or Escape.
- Support Arrow Up/Down, Home, End, Enter, and Space.
- Typeahead is required for long option lists.
- Keep focused items visible while scrolling.
- Submenus open with click, Enter, Space, or Arrow Right; Arrow Left closes to the parent. Add a short pointer grace area rather than requiring pixel-perfect movement.
- Place below the trigger by default. Flip above when space below is insufficient and space above is greater. Shift horizontally to maintain the 12px viewport gutter.
- Escape clipping when necessary; visual behavior must remain identical.
- On open, use opacity `130ms linear` plus translateY `7px` and scale `.985→1` over `180–190ms ease.emphasized`.
- Reverse the translation direction when opening upward. Closing should not leave invisible focusable items.

### State summary

- **Default:** Trigger is closed and neutral; floating content is hidden and non-focusable.
- **Hover:** Trigger changes text or border subtly; an item uses accent-dark text on `accent.subtle`.
- **Active:** The trigger exposes its expanded state and rotates the chevron; a pressed item does not shift layout.
- **Focus:** Trigger and focused items have visible focus treatment; focus is restored after selection or Escape.
- **Disabled:** Disabled triggers cannot open. Disabled items are skipped by keyboard movement and cannot be selected.
- **Loading:** Preserve trigger and panel geometry. Replace the trigger chevron with a spinner while initial options load, or use skeleton rows for incremental panel loading.
- **Motion:** Use the specified opacity, `7px` translation, and `.985→1` scale; reduce all motion when requested.

### Usage rules

Use Select for choosing a value and Menu for invoking actions. Do not mix navigation, destructive actions, checkable options, and complex form fields without clear grouping. Do not place long explanatory content in a menu item.

## Checkbox — Implemented

Shipped as native checkboxes inside .chapter-option, .chapter-option-all, .source-select-all and .option-row.

- **Anatomy:** Control, label, optional supporting text, optional indeterminate state.
- **Dimensions:** Box `16–18px`; label hit target at least `40px` high; label gap `9–12px`.
- **Visual style:** Square, `1px` default border, warm transparent background; checked fill uses accent; check mark uses inverse.
- **Typography:** Interface `14–16px`; helper `12–13px` secondary.
- **Spacing:** Align box with the first text line, not the total block center.
- **Default state:** Empty box and primary label.
- **Hover:** Accent border or subtle selected background across a larger row.
- **Active:** Accent-dark border/fill.
- **Focus:** Standard ring around the control or its complete selectable container.
- **Disabled:** Opacity `.42`, no toggle.
- **Loading:** Not applicable; loading belongs to the containing form or operation.
- **Motion:** Border/background/check opacity `100–140ms`.
- **Usage rules:** Use for independent multi-selection. Support an indeterminate state for group selection.

## Radio — Implemented

Shipped as native radios inside .option-row for single-choice questions.

- **Anatomy:** Circular control, label, optional supporting text.
- **Dimensions:** Control `18px`; label target at least `40px` high; gap `10–12px`.
- **Visual style:** Circular semantic exception; default border and accent inner dot when selected.
- **Typography:** Interface `14–16px` or Display `16px` for reading-heavy options.
- **Spacing:** Use `8–12px` between stacked options or turn substantial options into `62–66px` ruled rows.
- **Default state:** Empty ring.
- **Hover:** Accent border and optional subtle row background.
- **Active:** Accent-dark inner dot.
- **Focus:** Standard focus ring.
- **Disabled:** Opacity `.42`, no selection.
- **Loading:** Not applicable at control level.
- **Motion:** Color/background `100–140ms`.
- **Usage rules:** Use for one choice among visible alternatives. Use Select when the option set is long or space is constrained.

## Switch — Extension spec

Not implemented in this repository yet: no template, class or script uses it. Treat the recipe below as a target for future work.

- **Anatomy:** Track, thumb, label, optional state description.
- **Dimensions:** Track `42×22px`; thumb `16px`; internal inset `3px`; label target at least `44px` high.
- **Visual style:** Rounded only because the control's physical metaphor requires it. Off track uses `border.default`; on track uses accent; thumb uses raised surface.
- **Typography:** Interface `14–16px`; do not place essential state text inside the track.
- **Spacing:** Track-to-label gap `12px`.
- **Default state:** Off state with neutral track.
- **Hover:** Strengthen track border or accent color.
- **Active:** Thumb may compress by no more than `1px` visually; no bounce.
- **Focus:** Standard focus ring around the track.
- **Disabled:** Opacity `.42`, no toggle.
- **Loading:** Lock the track and show status beside it; do not spin the thumb.
- **Motion:** Thumb translation and track color `140–180ms ease.emphasized`.
- **Usage rules:** Use for an immediately applied binary setting. Use Checkbox when submission is deferred.

## Tabs — Extension spec

Not implemented in this repository yet: no template, class or script uses it. Treat the recipe below as a target for future work.

- **Anatomy:** Tab list, tab triggers, active indicator, tab panels.
- **Dimensions:** Trigger min-height `40px`; horizontal padding `0–4px`; gap `12–16px`; indicator `2px`.
- **Visual style:** Underline tabs, transparent background, radius `0`; never a row of decorative pills.
- **Typography:** Interface `13–14px`, weight `650`.
- **Spacing:** Keep `24–32px` between the tab list and panel content when no divider is present.
- **Default state:** Muted text and transparent border.
- **Hover:** Primary or accent text.
- **Active:** Accent-dark text with a `2px` accent bottom indicator.
- **Focus:** Standard focus ring, offset reduced to `2px` where space is tight.
- **Disabled:** Disabled text, skipped during activation.
- **Loading:** Keep the tab selected; skeletonize or locally load the panel.
- **Motion:** Indicator position/width `180ms ease.emphasized`; text color `140ms`.
- **Usage rules:** Support Left/Right/Home/End with roving focus. On narrow screens, scroll the tab list horizontally.

## Card — Implemented

Shipped as .question-card, .summary-card, .auth-card, .result-card, .table-card, .error-card and .glossary-card.

- **Anatomy:** Optional header, body, optional footer/actions, optional top rule.
- **Dimensions:** Compact padding `16px`; default `22–24px`; reading panel `clamp(30px, 6vw, 70px)`. Grid gap `10–12px`.
- **Visual style:** `surface.primary`, radius `0`, no shadow. Important cards use a `2px border.strong` top rule; quiet cards use a subtle border or no boundary.
- **Typography:** Heading uses Display `20–24px`; body follows content role.
- **Spacing:** Header-to-body `16–24px`; body-to-footer `20–24px`.
- **Default state:** Flat and stable.
- **Hover:** No hover for static cards. Clickable cards may use accent border and `translateY(-1px)`.
- **Active:** Clickable cards use `accent.subtle`; avoid scaling.
- **Focus:** Standard ring around the complete interactive card.
- **Disabled:** Reduce action opacity, not necessarily the readability of static content.
- **Loading:** Preserve card dimensions with restrained skeleton blocks.
- **Motion:** Interactive border/background/transform `140ms`.
- **Usage rules:** Do not make every section a card. Do not nest multiple raised cards.

## Modal — Extension spec

Not implemented in this repository yet: no template, class or script uses it. Treat the recipe below as a target for future work.

- **Anatomy:** Overlay, dialog surface, title, body, close control, action row.
- **Dimensions:** Width `min(520px, viewport width - 36px)`; optional wide variant up to `720px`; max-height `viewport height - 48px`; padding `24–32px`.
- **Visual style:** Raised surface, `1px border.default`, `2px` accent or semantic top rule, radius `0–2px`, floating shadow. Overlay `rgba(32,35,31,.42)` with no blur or very subtle blur.
- **Typography:** Display title `22–28px`; body Interface `14–16px/1.6`.
- **Spacing:** Title-to-body `16px`; body-to-actions `24–32px`; action gap `8–12px`.
- **Default state:** Centered and visually dominant without appearing glossy.
- **Hover:** Only interactive descendants respond.
- **Active:** No dialog-level active state.
- **Focus:** Trap focus inside; focus the title or first meaningful control; return focus on close.
- **Disabled:** Disable individual actions; do not disable the entire surface visually.
- **Loading:** Keep the dialog open, preserve layout, disable conflicting actions, and show local progress.
- **Motion:** Overlay fade `180ms`; dialog opacity + `5px` translation + `.985→1` scale over `180–220ms`.
- **Usage rules:** Support Escape and a visible close control. Decide outside-click dismissal according to consequence. Require explicit confirmation for destructive actions.

## Popover — Implemented

Shipped as the glossary definition popover only (.glossary-popover and its parts); there is no generic popover component.

- **Anatomy:** Anchor, floating surface, optional heading, content, optional close control.
- **Dimensions:** Width `min(340px, viewport width - 24px)`; max-height `viewport height - 24px`; padding `20px 22px`; offset `9px`; viewport gutter `12px`.
- **Visual style:** Raised surface, default border, `2px` accent top rule, radius `0`, floating shadow.
- **Typography:** Heading Display `20px/1.3`; body Interface `12–14px/1.65`; metadata Mono `10px`.
- **Spacing:** Heading-to-body `8–12px`; distinct content groups `12–16px`.
- **Default state:** Hidden and non-focusable.
- **Hover:** Anchor may use accent text and subtle background; the surface itself does not change.
- **Active:** Anchor shows expanded/selected treatment.
- **Focus:** Support Escape and focus return. Move focus inside only when the popover contains interactive controls.
- **Disabled:** Disabled anchors do not open.
- **Loading:** Preserve panel size; use local skeleton or concise progress text.
- **Motion:** Opacity `130ms` plus `5px` translation over `160ms ease.emphasized`.
- **Usage rules:** Reposition on scroll/resize, clamp horizontally, and flip vertically. Use for contextual content or compact interaction, not critical multi-step flows.

## Tooltip — Extension spec

Not implemented in this repository yet: no template, class or script uses it. Treat the recipe below as a target for future work.

- **Anatomy:** Trigger relationship and short text bubble; optional small directional pointer.
- **Dimensions:** Padding `6px 8px`; max-width `240px`; offset `6–8px`; radius `2px`.
- **Visual style:** Primary ink background, inverse text, no large shadow.
- **Typography:** Interface `12px/1.4`, weight `500`.
- **Spacing:** One short phrase or at most two compact lines.
- **Default state:** Hidden.
- **Hover:** Show after `200–300ms`; keep open while the trigger remains hovered.
- **Active:** Not applicable.
- **Focus:** Show on keyboard focus and dismiss on Escape or blur.
- **Disabled:** A disabled control may still expose a tooltip through a focusable wrapper when explanation is essential.
- **Loading:** Not applicable.
- **Motion:** Opacity + `2–3px` translation over `100–140ms`.
- **Usage rules:** Tooltips provide supplementary labels, never essential instructions or interactive content.

## Toast — Extension spec

Not implemented in this repository yet: no template, class or script uses it. Treat the recipe below as a target for future work.

- **Anatomy:** Semantic rule or marker, message, optional action, optional close button.
- **Dimensions:** Width `min(360px, viewport width - 36px)`; padding `12px 16px`; stack gap `8px`.
- **Visual style:** Raised or semantic-subtle surface, `3px` semantic left border, radius `0`, optional floating shadow only when detached from layout.
- **Typography:** Interface `13–14px/1.5`.
- **Spacing:** Message-to-action `12px`; close target at least `40px`.
- **Default state:** Neutral notices use accent family; positive, warning, and destructive use their semantic families.
- **Hover:** Pause timed dismissal; only actions change color.
- **Active:** No container-level active state.
- **Focus:** Actions and close control use standard focus rings.
- **Disabled:** Not applicable to the container.
- **Loading:** Use a persistent informational toast only for genuinely background work; prefer local loading feedback.
- **Motion:** Opacity + `8px` translation over `180ms`; no bounce.
- **Usage rules:** Auto-dismiss low-risk success/information after `5–7s`; keep errors or actionable notices until dismissed. Announce with the appropriate live-region politeness.

## Table — Implemented

Shipped as .table-wrap + .data-table with .table-link rows.

- **Anatomy:** Optional caption/toolbar, header, body rows, cells, optional row actions, empty/loading region.
- **Dimensions:** Desktop cells `19px 22px`; compact cells `14px`; row target at least `44px`.
- **Visual style:** Primary surface with `2px border.strong` top rule; collapsed borders; subtle row dividers; radius `0`; no shadow or zebra stripes by default.
- **Typography:** Header Mono `10px`, weight `600`, tracking `.1em`, uppercase where appropriate. Body Interface `14px`; principal text may use Display `16px/1.55`. Numeric cells use tabular numerals.
- **Spacing:** Keep actions compact and align comparable values consistently. Right-align numeric columns when comparison benefits.
- **Default state:** Primary text for key column, secondary text elsewhere.
- **Hover:** Row changes to raised surface over `140ms`.
- **Active:** Selected rows use accent-subtle background and an accent edge or explicit selection control.
- **Focus:** Interactive cells and row actions have independent focus rings; do not rely on row hover.
- **Disabled:** Unavailable row actions are disabled without fading all readable data.
- **Loading:** Preserve columns with skeleton rows; avoid replacing the complete table with a centered spinner.
- **Motion:** Row color `140ms`; sorting/reordering should avoid large movement.
- **Usage rules:** Use horizontal scrolling when cross-column comparison is essential. Otherwise, below `640px`, convert each row into labeled value blocks and retain accessible header relationships.

## Navigation — Implemented

Shipped as .site-header, .brand, .course-switcher / .course-menu-*, and the .agenda-row / .resource-row link rows.

- **Anatomy:** Brand or title, primary links, optional secondary actions, optional side rail, optional compact mobile control.
- **Dimensions:** Top toolbar `64–66px`; text action min-height `38px`; side rail `240–280px`; icon `16px`.
- **Visual style:** Canvas background, subtle bottom or side border, no shadow. Active items use accent text and either accent-subtle background or a thin accent rule.
- **Typography:** Brand/section title Display `14–16px`, weight `600`; links Interface `12–14px`, weight `600–650`.
- **Spacing:** Toolbar horizontal padding `22–72px`; action gap `8px`; side-rail rows `40–44px` high.
- **Default state:** Secondary text on transparent background.
- **Hover:** Accent text and border or subtle background.
- **Active:** Accent-dark text with accent-subtle surface; expose the active location semantically.
- **Focus:** Standard focus ring.
- **Disabled:** Disabled text, no navigation.
- **Loading:** Keep navigation stable; show progress in the destination region rather than replacing navigation.
- **Motion:** Color/background `140ms`; collapsible rail `180–220ms` without spring.
- **Usage rules:** Keep hierarchy shallow and labels explicit. On small screens, hide low-priority metadata before primary actions; use a drawer only when the link set cannot fit.

### Interactive Row / Navigation Row

- **Purpose:** Use for secondary navigation, resource entries, review entries, related tools, and navigation-rail items.
- **Anatomy:** One complete interactive target with a title, optional description, and optional trailing directional cue. Do not make the title, description, and arrow separate tab targets.
- **Visual style:** Flat, square, and ruled. Prefer shared local dividers over independent four-sided cards. Rows fill their local rail and share consistent padding, type, minimum height, and divider grammar.
- **Typography:** Title `16–18px` Interface at `600–650`; description `13–14px` Interface with `1.5–1.6` line height. Descriptions are product information, not decorative metadata.
- **Hover:** Use at most two cues: a subtle surface/text/border change plus optional `2–6px` directional movement. No scale, glow, shadow, bounce, or large lift.
- **Active:** Use stable accent-subtle or accent-text feedback without changing row dimensions.
- **Focus:** The complete row receives one visible focus ring and remains keyboard-operable through native link or button semantics.
- **Reduced motion:** Remove translation while preserving background, text, border, active, and focus feedback.
- **Usage rules:** Same-level rows share width, padding, min-height, typography, divider, hover, focus, and arrow behavior. A zero-count destination remains interactive unless business logic explicitly disables it.

### Secondary rail

Secondary does not mean tiny. Keep a rail wide enough for readable titles and descriptions; adjust the column ratio or breakpoint before shrinking typography. Repeated entries should use a shared interactive-row rhythm rather than disconnected small cards. A comfortable desktop rail is typically `320–380px` when the main task has priority.

## Badge — Implemented

Shipped as the .status chips (.status-mastered / .status-progressing / .status-good / .status-weak / .status-pending / .status-not-started), .mode-pill and .knowledge-status.

- **Anatomy:** Short text, optional `6px` status dot, optional compact icon.
- **Dimensions:** Padding `4px 7px`; min-height approximately `22px`; icon/dot gap `6–7px`; radius `0` by default.
- **Visual style:** Transparent or subtle surface with `1px border.subtle`; semantic badges use semantic text and subtle fill. Use round radius only for a lone dot.
- **Typography:** Mono `10px`, weight `650`, tracking `.04–.06em`.
- **Spacing:** Keep detached badges at least `8px` from primary text.
- **Default state:** Secondary or semantic text.
- **Hover:** None unless the badge is explicitly interactive.
- **Active:** Interactive badges use accent-subtle selection treatment.
- **Focus:** Standard ring when interactive.
- **Disabled:** Opacity `.42` only when interactive.
- **Loading:** Not applicable; use Skeleton for unknown badge content.
- **Motion:** Color/background `100–140ms` when interactive.
- **Usage rules:** Keep labels brief. Do not use large rounded pills as decoration.

## Avatar — Extension spec

Not implemented in this repository yet: no template, class or script uses it. Treat the recipe below as a target for future work.

- **Anatomy:** Image or initials, accessible text alternative, optional status dot.
- **Dimensions:** Compact `32px`; default `40px`; large `56px`; status dot `6–8px`.
- **Visual style:** Circular semantic exception, subtle border, surface-subtle fallback, no shadow. Keep imagery natural and low in saturation when controllable.
- **Typography:** Initials Interface `12–16px`, weight `650`, primary ink.
- **Spacing:** Avatar-to-label gap `10–12px`.
- **Default state:** Image or initials with a quiet boundary.
- **Hover:** None unless interactive; interactive avatars may strengthen border.
- **Active:** Accent border or subtle background on the containing control.
- **Focus:** Standard ring around the full interactive target.
- **Disabled:** Fade only if it represents an unavailable action.
- **Loading:** Use a circular surface-subtle Skeleton of the same size.
- **Motion:** Border/color `140ms`; no image zoom.
- **Usage rules:** Avatars identify people or persistent identities; do not use them as generic decoration.

## Skeleton — Extension spec

Not implemented in this repository yet: no template, class or script uses it. Treat the recipe below as a target for future work.

- **Anatomy:** Shape blocks matching the final layout.
- **Dimensions:** Match the expected text lines, controls, images, and cards; text lines use `8–12px` height with realistic varying widths.
- **Visual style:** Surface-subtle fill on canvas or primary surface; radius `0–2px`; no shadow.
- **Typography:** None.
- **Spacing:** Mirror final content spacing exactly to prevent layout shift.
- **Default state:** Low-contrast static blocks.
- **Hover:** None.
- **Active:** None.
- **Focus:** Never focusable.
- **Disabled:** Not applicable.
- **Loading:** Optional opacity pulse between approximately `.55` and `.85`; avoid bright shimmer streaks.
- **Motion:** `1000–1400ms` low-contrast pulse; static under reduced motion.
- **Usage rules:** Use when structure is known and loading lasts long enough to perceive. Use concise progress text when structure is unknown.

## Empty State — Implemented

Shipped as .empty-state with .empty-actions.

- **Anatomy:** Heading, description, optional primary action, optional secondary action.
- **Dimensions:** Vertical padding `clamp(54px, 9vw, 100px)` and horizontal padding `24px`; keep text in a compact centered measure.
- **Visual style:** Transparent or primary surface, optional strong top rule, no illustration by default, no shadow.
- **Typography:** Display heading `32px`; secondary description `14–16px/1.6`.
- **Spacing:** Heading-to-description `10px`; description-to-action `20–24px`.
- **Default state:** Calm, informative, and centered.
- **Hover:** Only actions respond.
- **Active:** Not applicable to the container.
- **Focus:** Actions use standard focus.
- **Disabled:** Avoid presenting a disabled primary action without explanation.
- **Loading:** Do not show Empty State until loading has conclusively finished.
- **Motion:** None at container level.
- **Usage rules:** State what is absent, explain the consequence briefly, and offer one sensible recovery path. Avoid large decorative illustrations.
- For zero counts or empty collections, choose either a compact zero metric or a semantic empty state according to the decision the user needs to make.
- Explain what is absent and what will cause content to appear next. Do not fabricate activity, recommendations, or statistics to fill space.

# Form Composition

- Use a vertical label/control/helper structure by default.
- Group related fields with spacing before adding a surrounding card.
- Put adjacent controls in a shared border-block row only when they form one compact configuration unit.
- Place the primary submit action after the fields and separate it by at least `8px` beyond normal field spacing.
- On small screens, stack multi-column forms and make the primary action full-width.
- Required fields need semantic indication; a visual asterisk may supplement but not replace programmatic required state.
- Inline validation belongs directly below its field. Summary errors belong above the form only when multiple fields need attention.
- Destructive actions must be visually secondary until the user deliberately approaches them.

# System Feedback

## Loading

Prefer localized loading. Use button loading for submissions, Skeleton for predictable content, and progress bars for measurable sequences. Avoid full-screen spinners unless no stable shell can be shown.

## Error

Use destructive text and subtle background with a `3px` left rule for banners. Use destructive border plus adjacent helper text for fields. Full-region failure states use a strong top rule, clear heading, concise explanation, and a recovery action.

## Success

Use positive text and subtle background. Confirm outcomes with explicit language. Avoid celebratory animation unless the interaction specifically calls for it.

## Warning

Use warning color for partial completion, caution, or pending attention. A warning should not visually compete with a destructive error.

## Information

Use the accent family for important neutral notices. Keep informational feedback visually quieter than the primary action.

# Accessibility Rules

- Maintain at least 4.5:1 contrast for ordinary essential text and 3:1 for large text.
- Restrict `text.muted` to nonessential metadata; use `text.secondary` for critical small copy.
- Give every interactive element a visible keyboard focus state.
- Keep pointer targets at least `40×40px`; prefer `44px` minimum row height.
- Give every form control a programmatic label.
- Connect helper and error text to controls and expose invalid state semantically.
- Support complete keyboard interaction for menus, selects, tabs, modals, popovers, and disclosures.
- Restore focus when dismissing temporary surfaces.
- Trap focus only in truly modal surfaces.
- Use live-region semantics appropriate to the urgency of feedback.
- Pair all semantic colors with text, shape, symbol, or accessible labeling.
- Preserve content and state when motion is reduced.
- Do not use a native title attribute as the only delivery mechanism for essential information.

# Design Tokens

The following CSS variables are an illustrative, technology-neutral encoding. Equivalent tokens may be represented in any platform as long as names, values, and relationships remain consistent.

**Token → implemented CSS variable.** The shipped stylesheet predates this document and uses its own names, so this table is the authoritative translation between the two. Use the right-hand column when writing CSS for this application:

| Design token | Implemented CSS variable | Value |
|---|---|---|
| `surface.canvas` | `--paper` | `#f1eee6` |
| `surface.subtle` | `--paper-deep` | `#e5dfd2` |
| `surface.primary` | `--sheet` | `#faf8f2` |
| `surface.raised` / `surface.hover` | `--sheet-bright` | `#fffdf8` |
| `surface.selected` / `accent.subtle` | `--accent-soft` | `#f2e2da` |
| `text.primary` | `--ink` | `#20231f` |
| `text.secondary` | `--ink-soft` | `#51564f` |
| `text.muted` | `--muted` | `#747970` |
| `text.disabled` | *(no dedicated variable; `--muted` is used)* | — |
| `text.inverse` | *(no dedicated variable; `--sheet-bright` is used)* | — |
| `border.subtle` | `--line` | `#d1ccbf` |
| `border.default` | `--line-strong` | `#aaa598` |
| `border.strong` | `--ink` | `#20231f` |
| `border.focus` / `accent.focus` | `--focus` | `rgba(161, 68, 47, 0.26)` |
| `accent.primary` | `--accent` | `#a1442f` |
| `accent.hover` / `accent.active` | `--accent-dark` | `#783122` |
| `positive` / `positive-subtle` | `--success` / `--success-soft` | `#356b55` / `#e4eee7` |
| `destructive` / `destructive-subtle` | `--danger` / `--danger-soft` | `#9d3c35` / `#f3e2de` |
| `warning` / `warning-subtle` | `--warning` / `--warning-soft` | `#8a642c` / `#f2ead8` |
| `font.display` | `--font-display` | Iowan Old Style / Palatino / Noto Serif SC / Songti SC / Georgia, serif |
| `font.interface` | `--font-ui` | Inter / system-ui / Segoe UI / Microsoft YaHei, sans-serif |
| `font.metadata` | `--font-data` | SFMono-Regular / Consolas / Liberation Mono, monospace |

There is no implemented variable for the spacing, radius, shadow, motion or size scales below: the stylesheet writes those values literally (page shell `1180px`, control height `46px`, radius `0–2px`, durations `100–220ms`). Treat the scale below as the normative target when adding new rules, and keep the literal values consistent with it.

```css
:root {
  --surface-canvas: #f1eee6;
  --surface-subtle: #e5dfd2;
  --surface-primary: #faf8f2;
  --surface-raised: #fffdf8;
  --surface-hover: #fffdf8;
  --surface-selected: #f2e2da;

  --text-primary: #20231f;
  --text-secondary: #51564f;
  --text-muted: #747970;
  --text-disabled: #94988f;
  --text-inverse: #fffdf8;

  --border-subtle: #d1ccbf;
  --border-default: #aaa598;
  --border-strong: #20231f;
  --border-focus: rgba(161, 68, 47, 0.26);

  --accent-primary: #a1442f;
  --accent-hover: #783122;
  --accent-active: #783122;
  --accent-subtle: #f2e2da;

  --positive: #356b55;
  --positive-subtle: #e4eee7;
  --warning: #8a642c;
  --warning-subtle: #f2ead8;
  --destructive: #9d3c35;
  --destructive-subtle: #f3e2de;

  --font-display: "Iowan Old Style", "Palatino Linotype", "Book Antiqua",
    "Noto Serif SC", "Songti SC", Georgia, serif;
  --font-interface: Inter, system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", "Microsoft YaHei", sans-serif;
  --font-metadata: "SFMono-Regular", Consolas, "Liberation Mono", monospace;

  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --space-10: 40px;
  --space-12: 48px;
  --space-14: 56px;
  --space-16: 64px;
  --space-18: 72px;
  --space-22: 88px;
  --space-24: 96px;

  --radius-none: 0;
  --radius-control: 2px;
  --radius-round: 999px;

  --shadow-floating: 0 16px 42px rgba(32, 35, 31, 0.16);

  --motion-micro: 100ms;
  --motion-fast: 140ms;
  --motion-standard: 180ms;
  --motion-slow: 220ms;
  --ease-standard: ease;
  --ease-emphasized: cubic-bezier(.22, .72, .28, 1);
  --ease-progress: cubic-bezier(.2, .8, .4, 1);

  --control-height-compact: 40px;
  --control-height-default: 46px;
  --content-max: 1180px;
  --reading-max: 980px;
  --dialog-max: 520px;
}
```

# Aesthetic Constraints

The following choices materially weaken this visual language:

- Pure white canvases or cool blue-gray neutral systems.
- Multiple saturated accent colors competing in one view.
- Gradients, glassmorphism, glossy highlights, or blurred translucent cards.
- Large ambient shadows on in-flow content.
- Medium or large corner radii on ordinary controls and panels.
- Excessive pill-shaped buttons, tabs, filters, and badges.
- Bold sans-serif typography replacing the editorial serif hierarchy.
- Decorative monospaced paragraphs or uppercase applied to long text.
- Oversized headings on every view rather than selective editorial emphasis.
- Dense card grids where spacing and rules would communicate structure more clearly.
- Deeply nested card-on-card layouts.
- Decorative icons without semantic purpose.
- Hover effects that combine lift, scale, glow, color, and shadow.
- Springy animation, large slides, bounce, or parallax.
- Arbitrary spacing and one-off colors outside the token system.
- Removing focus outlines without an equally visible replacement.

Avoid mechanically combining a tiny metadata kicker, very large serif hero, italic English translation, abstract inspirational slogan, three equal oversized metrics, full-width accent CTA, repeated long horizontal rules, excessive whitespace, and perfectly symmetric content blocks. Any one device may be valid in context; together they must not become a default formula for looking designed. Optimize for making product hierarchy obvious, not for displaying every available design token or filling the composition.

# Designing New Components

When a component is not explicitly specified, derive it from the established primitives:

1. Choose all colors from the semantic token set.
2. Use the spacing scale for dimensions and internal rhythm.
3. Use `radius.none` by default and `radius.control` only for controls.
4. Assign typography by content role: Display, Interface, or Metadata.
5. Match the border treatment of the closest structural relative.
6. Reuse the state grammar for hover, active, focus, disabled, and loading.
7. Follow the motion durations, easing, and amplitude limits.
8. Prefer composition of existing primitives over a new visual motif.
9. Add shadow only if the component leaves normal document flow and overlaps content.
10. Test the component in neutral, selected, disabled, error, loading, and narrow-screen contexts.
11. Verify that the component would still belong if its content and purpose changed completely.
12. Do not introduce a new visual language merely because the component is new.

## Derivation examples

- A date picker inherits Input for its trigger, Popover for its floating surface, Menu Item for selectable cells, and Dropdown for focus/collision behavior.
- A command palette inherits Modal or Popover for elevation, Input for search, and Dropdown Menu Item for results.
- A file uploader inherits Card for its boundary, Button for explicit actions, and Empty State for its unfilled condition.
- An accordion inherits Card rules, a Ghost Button trigger, subtle dividers, and standard reveal motion.
- A pagination control inherits compact Button and Navigation states, with monospaced numerals when alignment benefits.
- A tree view inherits Navigation row density, Dropdown keyboard discipline, and subtle border indentation rather than nested cards.
- A calendar grid inherits Table alignment, Radio-like single selection, Checkbox-like multi-selection, and accent-subtle selected cells.

The completed component should look as though this design system always included it.

# Do and Don't

## Do

- Use warm neutral relationships consistently.
- Build hierarchy with typography and space first.
- Use thin rules to separate regions.
- Keep accent rare and meaningful.
- Keep default controls near 46px high.
- Keep floating surfaces sharp and viewport-aware.
- Preserve explicit keyboard focus.
- Pair state color with text or symbols.
- Adapt composition at narrow widths.
- Keep motion short and physically small.
- Use skeletons that mirror final geometry.
- Prefer one clear primary action per local group.

## Don't

- Do not recreate layouts, content, entities, or flows from any unrelated reference.
- Do not use generic rounded white cards as the default container.
- Do not add strong shadows to ordinary content.
- Do not introduce arbitrary high-saturation colors.
- Do not make every control pill-shaped.
- Do not use mono type for long reading.
- Do not reduce mobile design to font scaling.
- Do not hide important actions before secondary metadata.
- Do not communicate state through color alone.
- Do not add ornamental motion.
- Do not invent new tokens before testing existing combinations.
- Do not let a new component become a stylistic exception.

# AI Implementation Priority

When rules appear to conflict, resolve them in this order:

1. Design DNA.
2. Color relationships.
3. Typography hierarchy.
4. Spacing and density.
5. Component proportions.
6. Surface, border, and radius treatment.
7. Interaction states and accessibility.
8. Motion character.
9. Decorative details.

Maintaining the overall visual character is more important than reproducing any single isolated numeric value. Accessibility and platform correctness remain hard constraints; preserve the aesthetic through the next-highest-priority means when an exact value conflicts with either.

# Instructions to AI Frontend Agents

You are not recreating an existing application.

You are implementing a new product using this visual language.

Do not infer business requirements from this document. Do not recreate pages, content, navigation structure, entities, or workflows from any other application. Use this document only as a visual and interaction design system.

When implementing a new interface or modifying an existing product surface:

1. Identify the real business state and available data.
2. Identify the user's most likely next action.
3. Build hierarchy from task priority before applying visual language.
4. Apply the established visual system without turning editorial character into a mandatory hero.
5. Do not invent content, statistics, recommendations, or behavior to balance the layout.
6. Do not showcase all tokens, component styles, or hierarchy levels at once.
7. Verify that hierarchy remains clear in grayscale.
8. Verify that accent remains scarce and semantic.
9. Check for template-like symmetry, repeated cards, and unnecessary full-width dividers.
10. Recompose at responsive breakpoints so current state and primary action remain first.

In all implementations:

- Preserve the warm neutral color relationships.
- Preserve the editorial typography hierarchy.
- Preserve the spacious outer layout and moderate component density.
- Preserve control heights, padding, and component proportions.
- Preserve square corners, fine borders, and restrained surface elevation.
- Preserve the scarcity and semantic role of the brick-red accent.
- Preserve subtle hover, clear focus, explicit disabled, and stable loading states.
- Preserve short durations, small movement, and non-spring motion.
- Choose structure and semantics according to the new product's actual requirements.
- Compose new components from the documented visual primitives.
- Keep accessibility, keyboard behavior, and reduced-motion support complete.
- Treat every unspecific decorative choice as neutral, quiet, and functional.

The new product should feel as though it was designed by the same design team, while remaining structurally and semantically appropriate for its own requirements.

**Same design language. Same visual taste. Same interaction character. Different product, business, and content.**

---

## Course selector and course list (added for multi-course)

The course selector and the course list page reuse this design system instead of
introducing a second look:

* it sits in `site-header` between the brand and `header-actions`, using the same
  `--font-data` label style, the same `1px solid var(--line-strong)` underline
  affordance as `.header-link`, and the same `--accent` hover/`--accent-dark`
  active treatment;
* the picker is a native `<details>`/`<summary>` with plain `<a>` links, so
  course selection works with JavaScript disabled, with the keyboard, and on
  touch; the open menu reuses the `.size-picker-menu` look (sheet background,
  `--line` border, single soft shadow) rather than a new popover style;
* the course list page uses the existing heading pattern (`section-label` +
  `h1` + lead paragraph), the existing `.status` chips for the per-course state
  (`可学习` / `等待更新` / `已停用` / `未部署` / `不可用`), and the existing
  `.button` variants for the primary action;
* state variants only tint the card background with `--paper-deep`
  (`.course-card-disabled`, `.course-card-unavailable`, `.course-card-stale`,
  `.course-card-undeployed`) so a non-servable course reads as de-emphasised
  without inventing new colours;
* a course card is a column (`display: flex; flex-direction: column`) whose last
  child is pinned to the bottom (`margin-top: auto`) and whose launcher keeps its
  content width (`align-self: flex-start`, since a flex column would stretch it).
  Grid rows stretch every card to the same height, so the four characters of
  `开始学习` share one baseline across a row even when one card's English title
  takes two lines and another's three — the launcher is never positioned by the
  length of the copy above it;
* responsive behaviour hooks into the existing breakpoints: at
  `max-width: 820px` the toolbar becomes two rows — the brand and the account
  actions (`用户名` / `退出登录`) keep the first row, and the course picker owns a
  full-width second row (`flex: 1 0 100%` plus explicit `order`) instead of
  shrinking next to the brand and pushing an action onto a wrapped line; its menu
  becomes an inline list (no floating overlay on a narrow screen), and the course
  grid collapses to a single column. Below `640px` the low-priority `当前课程`
  label is dropped (the picker keeps the course title with ellipsis and
  `切换课程`), and long titles truncate instead of wrapping the toolbar.

The header always names the course the current page belongs to, and every page's
forms post back to the same `course_id`, so the selector is navigation only — it
never resets progress and never restarts the service.

### What each surface actually lists

The two course surfaces are deliberately different, and neither invents a course:

* **Header menu** (`base.html`): only courses that are *declared by this worker's
  catalogue and enabled*. A declared, enabled course that is `unavailable` or
  `stale` **is** listed — with its state appended as a `.course-status` suffix
  (`（unavailable）` / `（stale）`) — because the learner must be able to navigate to
  it and get an honest 503. Courses that are `disabled` (`enabled: false`) and
  `undeployed` (known to the database but absent from this worker's catalogue) are
  **not** listed in the header menu at all. The last entry is always
  `全部课程`, which links to `/courses`.
* **`/courses` page** (`courses.html`): renders **every** `CourseState` this worker
  knows, including `disabled` and `undeployed`, each as a `.course-card` with the
  status chip `可学习` / `等待更新` / `已停用` / `未部署` / `不可用` and the state-specific
  tint class (`.course-card-disabled`, `.course-card-stale`, `.course-card-undeployed`,
  `.course-card-unavailable`). Only a `ready` card carries the primary
  `开始学习` action; the others explain themselves with a `.launcher-note` instead.
  An empty catalogue falls back to the `.empty-state` block.

Because the state is recomputed against the database on every request, a course can
move between `可学习` and `等待更新` without any page or process reload.

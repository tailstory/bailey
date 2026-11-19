# Bailey

Tailstory's mascot, a border collie, as small SVG files that link to each other.

| Folder | Holds |
| --- | --- |
| `parts/` | The only drawn art. |
| `type/` | The wordmark and single letters, outlined. |
| `marks/` | The peek mark and the monograms. |
| `tiles/` | App tiles: a rounded square plus one mark. |
| `lockups/` | A part beside the word. |
| `reference/` | Pairings kept for reference only. |
| `dist/` | Generated, never edited. [`sheet.svg`](dist/sheet.svg) shows everything. |

## Links

Everything outside `parts/` and `type/` only places other files:

```xml
<use href="../parts/tail.svg#art" x="312.35" y="149.13" width="935.29" height="575.61"/>
```

Design apps and GitHub's preview do not follow these links, so those files look empty there. Chrome follows them when the repo is served (`python -m http.server`).

## Themes

Art is `fill="currentColor"`. A tile's square is `class="ground"`. A theme in `bailey.toml` sets those two colours, and a new theme is a new table.

## Build

```
uv run build.py
```

Rebuilds `dist/`: every file in every theme as a self-contained SVG, plus the PNG sizes in `bailey.toml`. Commit `dist/` with the change that caused it.

## Rules

- One tail: `parts/tail.svg`. Never draw another.
- Cut one shape out of another with a `<mask>` on a `<g>` around the art, as in `marks/t-peek.svg`.
- The letters are Inter Tight ExtraBold outlines (SIL Open Font License, `type/OFL.txt`).

# Sample design kit

A design kit brands every `*.slides.html` deck the web viewer renders: colors,
fonts, a logo on every slide, and layout classes.

## Use it

Copy this folder to `.omnigent/design-kit/` at the workspace root:

```sh
mkdir -p .omnigent
cp -R examples/design-kits/sample .omnigent/design-kit
```

Then open any `*.slides.html` file. The deck toolbar shows "Sample Kit", and
slides use the kit's colors, fonts, and logo.

## Format

`kit.json` (only `name` is required):

- `colors`: `primary`, `secondary`, `accent`, `background`, `text` (CSS colors).
- `fonts.heading` / `fonts.body`: `family`, optional `weight`, optional `src`
  (a `.woff2`, `.woff`, `.ttf`, or `.otf` file in the kit folder). Without
  `src` the family must be installed on the viewer's machine.
- `logo`: `src` (`.svg`, `.png`, `.jpg`, `.webp`, `.gif`) and optional
  `position` (`top-left`, `top-right`, `bottom-left`, `bottom-right`).
- `css`: a stylesheet defining layout classes such as `.layout-title`.

Asset paths are relative to the kit folder and each file must be under 2 MB.
Decks can use `var(--kit-primary)`, `var(--kit-font-heading)`, and the other
`--kit-*` tokens, and put layout classes on a section:

```html
<section class="layout-title"><h1>Quarterly review</h1></section>
```

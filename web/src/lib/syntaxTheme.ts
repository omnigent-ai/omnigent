import type { ThemeRegistrationRaw } from "shiki";

type OttoInkPalette = {
  background: string;
  comment: string;
  escape: string;
  foreground: string;
  function: string;
  invalid: string;
  keyword: string;
  number: string;
  operator: string;
  property: string;
  string: string;
  type: string;
};

function createOttoInkTheme(
  name: string,
  type: "light" | "dark",
  palette: OttoInkPalette,
): ThemeRegistrationRaw {
  return {
    name,
    type,
    colors: {
      "editor.background": palette.background,
      "editor.foreground": palette.foreground,
    },
    settings: [
      {
        scope: ["comment", "punctuation.definition.comment", "string.comment"],
        settings: { foreground: palette.comment, fontStyle: "italic" },
      },
      {
        scope: [
          "keyword",
          "storage",
          "storage.type",
          "storage.modifier",
          "constant.language",
          "variable.language.this",
        ],
        settings: { foreground: palette.keyword },
      },
      {
        scope: ["string", "string.quoted", "string.template", "attribute.value"],
        settings: { foreground: palette.string },
      },
      {
        scope: ["constant.character.escape", "string.regexp constant.character.escape"],
        settings: { foreground: palette.escape },
      },
      {
        scope: ["constant.numeric", "number", "keyword.other.unit"],
        settings: { foreground: palette.number },
      },
      {
        scope: [
          "entity.name.type",
          "entity.name.class",
          "support.type",
          "support.class",
          "namespace",
          "type.identifier",
        ],
        settings: { foreground: palette.type },
      },
      {
        scope: [
          "entity.name.function",
          "support.function",
          "variable.function",
          "meta.function-call entity.name.function",
        ],
        settings: { foreground: palette.function },
      },
      {
        scope: [
          "property",
          "meta.property-name",
          "meta.object-literal.key",
          "variable.other.property",
          "entity.other.attribute-name",
          "attribute.name",
        ],
        settings: { foreground: palette.property },
      },
      {
        scope: ["keyword.operator", "punctuation", "delimiter", "meta.brace"],
        settings: { foreground: palette.operator },
      },
      {
        scope: ["entity.name.tag", "support.constant"],
        settings: { foreground: palette.function },
      },
      {
        scope: ["invalid", "invalid.illegal", "message.error"],
        settings: { foreground: palette.invalid },
      },
    ],
  };
}

export const LIGHT_SYNTAX_THEME_NAME = "omnigent-otto-ink-light";
export const DARK_SYNTAX_THEME_NAME = "omnigent-otto-ink-dark";

export const LIGHT_SYNTAX_THEME = createOttoInkTheme(LIGHT_SYNTAX_THEME_NAME, "light", {
  background: "#f7f6f3",
  comment: "#969097",
  escape: "#0b7f73",
  foreground: "#39353a",
  function: "#176fa6",
  invalid: "#c8324c",
  keyword: "#b72f6e",
  number: "#a56812",
  operator: "#766f76",
  property: "#9a6517",
  string: "#237d63",
  type: "#0f7773",
});

export const DARK_SYNTAX_THEME = createOttoInkTheme(DARK_SYNTAX_THEME_NAME, "dark", {
  background: "#1b191c",
  comment: "#817a81",
  escape: "#5ed6c6",
  foreground: "#eee9ec",
  function: "#6bb8f0",
  invalid: "#ff6b85",
  keyword: "#ff7fb5",
  number: "#f0b45a",
  operator: "#aaa3aa",
  property: "#e9b76a",
  string: "#66cfa9",
  type: "#62d1c4",
});

export const SYNTAX_THEMES: [ThemeRegistrationRaw, ThemeRegistrationRaw] = [
  LIGHT_SYNTAX_THEME,
  DARK_SYNTAX_THEME,
];

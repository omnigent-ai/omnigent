import type { CodeHighlighterPlugin, HighlightOptions, ThemeInput } from "streamdown";

// streamdown exports the plugin interface but not its HighlightResult type;
// recover it from the highlight method's signature.
type HighlightResult = NonNullable<ReturnType<CodeHighlighterPlugin["highlight"]>>;

// Streamdown's `code` plugin (@streamdown/code) statically imports Shiki's
// engine, including its WASM regex engine. Importing it eagerly pulls that
// engine into the main entry chunk even when no code block ever renders.
//
// This wrapper defers the @streamdown/code import until the first highlight
// call, mirroring the lazy Monaco/Shiki pattern elsewhere in the app, so the
// engine splits into its own chunk loaded on demand. It satisfies the same
// CodeHighlighterPlugin contract Streamdown consumes: getThemes() returns the
// default themes synchronously, and highlight() returns null while the engine
// loads, resolving tokens through the callback once it's ready.

const DEFAULT_THEMES: [ThemeInput, ThemeInput] = ["github-light", "github-dark"];

let realCode: CodeHighlighterPlugin | null = null;
let codePromise: Promise<CodeHighlighterPlugin> | null = null;

const loadCode = (): Promise<CodeHighlighterPlugin> => {
  // oxlint-disable-next-line eslint-plugin-promise(prefer-await-to-then)
  codePromise ??= import("@streamdown/code").then(({ code }) => {
    realCode = code;
    return code;
  });
  return codePromise;
};

export const lazyCodePlugin: CodeHighlighterPlugin = {
  name: "shiki",
  type: "code-highlighter",
  getThemes: () => realCode?.getThemes() ?? DEFAULT_THEMES,
  getSupportedLanguages: () => realCode?.getSupportedLanguages() ?? [],
  // Streamdown never calls this on the render path; stay optimistic until the
  // real plugin (which knows the bundled languages) has loaded.
  supportsLanguage: (language) => realCode?.supportsLanguage(language) ?? true,
  highlight: (
    options: HighlightOptions,
    // oxlint-disable-next-line eslint-plugin-promise(prefer-await-to-callbacks)
    callback?: (result: HighlightResult) => void,
  ): HighlightResult | null => {
    // Engine already loaded — delegate synchronously so the real plugin's
    // token cache (sync hit path) keeps working unchanged.
    if (realCode) {
      return realCode.highlight(options, callback);
    }

    // First call: kick off the lazy load, report "not ready", and resolve
    // through the callback once the engine tokenizes.
    // oxlint-disable-next-line eslint-plugin-promise(prefer-await-to-then)
    void loadCode().then((plugin) => {
      const result = plugin.highlight(options, callback);
      if (result && callback) {
        callback(result);
      }
    });
    return null;
  },
};

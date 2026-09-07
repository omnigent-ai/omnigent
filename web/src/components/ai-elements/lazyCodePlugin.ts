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
  // Streamdown never calls supportsLanguage/getSupportedLanguages on the render
  // path (zero call sites in streamdown's dist), and the real plugin's
  // highlight() falls back to "text" for unknown languages anyway, so an
  // optimistic pre-load answer is safe — unsupported code just renders as
  // plain text once the engine loads.
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

    // First call before the engine finishes loading: report "not ready" by
    // returning null. Streamdown's HighlightedCodeBlockBody keeps the raw code
    // in state and re-renders when the callback fires (it calls setState in the
    // callback), so we resolve tokens through the callback once loaded.
    //
    // Fire the callback at most once: on a synchronous cache hit the real
    // plugin returns the result without invoking the callback, so we invoke it;
    // otherwise the plugin invokes it later. The `fired` guard makes a double
    // invocation impossible regardless of which path the real plugin takes.
    // oxlint-disable-next-line eslint-plugin-promise(prefer-await-to-then)
    void loadCode().then((plugin) => {
      let fired = false;
      const fireOnce = (result: HighlightResult) => {
        if (fired) return;
        fired = true;
        callback?.(result);
      };
      const sync = plugin.highlight(options, fireOnce);
      if (sync) {
        fireOnce(sync);
      }
    });
    return null;
  },
};

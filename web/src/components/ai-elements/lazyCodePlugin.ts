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

/** Retry spacing while a session bind/backfill is holding the pre-warm off. */
const PREWARM_RETRY_MS = 500;

let prewarmScheduled = false;

/**
 * Warm the code highlighter from idle time, off any session-open path.
 *
 * The first highlight in a tab pays Shiki's one-time engine + grammar
 * compilation — a ~half-second main-thread block that otherwise lands in
 * the middle of the first opened session's transcript render. A throwaway
 * highlight while the shell idles moves that cost to before any code
 * block exists. `busy` gates it: while a conversation is binding or
 * backfilling (e.g. a direct /c/<id> page load), the warm-up defers and
 * retries, so it can never add jank to the load it exists to protect.
 */
export function schedulePrewarmCodeHighlighter(busy: () => boolean): void {
  if (prewarmScheduled || typeof window === "undefined") return;
  prewarmScheduled = true;
  // The timeout backstop matters: environments that produce no frames
  // (headless, background tabs) can starve requestIdleCallback entirely,
  // and a late warm-up is a warm-up that lands mid-session-open.
  const idle: (cb: () => void) => void =
    typeof window.requestIdleCallback === "function"
      ? (cb) => window.requestIdleCallback(() => cb(), { timeout: 1_200 })
      : (cb) => window.setTimeout(cb, 1_200);
  const attempt = (): void => {
    if (realCode !== null) return; // a real code block already warmed it
    if (busy()) {
      window.setTimeout(() => idle(attempt), PREWARM_RETRY_MS);
      return;
    }
    // oxlint-disable-next-line eslint-plugin-promise(prefer-await-to-then)
    void loadCode().then((plugin) => {
      // One tiny highlight per common language: the first call initializes
      // the engine and its grammar; the follow-up pays python's marginal
      // grammar so tool-heavy transcripts open clean too. (A session's own
      // large code blocks still pay their first content tokenization —
      // that part is content-cached, not engine-cached.)
      plugin.highlight({ code: "const warm = 1;", language: "typescript", themes: DEFAULT_THEMES });
      idle(() => {
        plugin.highlight({ code: "warm = 1", language: "python", themes: DEFAULT_THEMES });
      });
    });
  };
  idle(attempt);
}

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

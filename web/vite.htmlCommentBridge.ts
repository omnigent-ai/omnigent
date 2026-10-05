import { readFileSync } from "node:fs";
import path from "node:path";
import type { Plugin } from "vite";

const BRIDGE_MODULE = path.resolve(__dirname, "src/shell/htmlCommentBridge.ts");
const BRIDGE_FRAME_FILE = path.resolve(__dirname, "src/shell/htmlCommentBridgeFrame.js");
const BRIDGE_URL_EXPRESSION = 'new URL("./htmlCommentBridgeFrame.js", import.meta.url)';
const PATH_PLACEHOLDER = "__OMNIGENT_HTML_COMMENT_BRIDGE_PATH__";

/**
 * Emits the HTML-preview comment bridge as a standalone script file and points
 * `BRIDGE_SCRIPT_URL` at it. The srcdoc preview inherits the embedder's CSP, so
 * the bridge must load by URL from the app's own script origin; Vite's default
 * `new URL(..., import.meta.url)` handling inlines it as a `data:` URL in
 * library builds, and `data:` scripts are blocked by the same CSP. The output
 * keeps a `./`-relative `new URL("./assets/…", import.meta.url)` so a
 * downstream bundler (the Databricks monolith's rspack) can re-emit the file
 * onto its own CDN, as it does for the Monaco worker.
 */
export function htmlCommentBridgeAsset(): Plugin {
  let referenceId: string | undefined;
  return {
    name: "omnigent:html-comment-bridge-asset",
    apply: "build",
    enforce: "pre",
    transform(code, id) {
      if (id !== BRIDGE_MODULE) return null;
      if (!code.includes(BRIDGE_URL_EXPRESSION)) {
        this.error(`${BRIDGE_MODULE} no longer contains ${BRIDGE_URL_EXPRESSION}`);
      }
      referenceId = this.emitFile({
        type: "asset",
        name: "htmlCommentBridgeFrame.js",
        source: readFileSync(BRIDGE_FRAME_FILE, "utf8"),
      });
      this.addWatchFile(BRIDGE_FRAME_FILE);
      return {
        code: code.replace(BRIDGE_URL_EXPRESSION, `new URL(${PATH_PLACEHOLDER}, import.meta.url)`),
        map: null,
      };
    },
    renderChunk(code, chunk) {
      if (!referenceId || !code.includes(PATH_PLACEHOLDER)) return null;
      let relative = path.posix.relative(
        path.posix.dirname(chunk.fileName),
        this.getFileName(referenceId),
      );
      if (!relative.startsWith(".")) relative = `./${relative}`;
      return { code: code.replaceAll(PATH_PLACEHOLDER, JSON.stringify(relative)), map: null };
    },
  };
}

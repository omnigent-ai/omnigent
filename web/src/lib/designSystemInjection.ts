// Kind-agnostic design-system injection: the system's tokens and fonts as a
// <style>, plus `ds:` references in the content rewritten to data: URIs. The
// folder is untrusted, so only CSS and data: URIs from it reach the output,
// every path is confined to the folder, and assets are size-capped.

import {
  FONT_MIME,
  IMAGE_MIME,
  extension,
  kitDataUri,
  kitText,
  type KitFile,
} from "@/shell/codeViewerHelpers";

export const DS_STYLESHEET = "colors_and_type.css";
export const DS_ASSET_MAX_BYTES = 2 * 1024 * 1024;
export const DS_DECK_MAX_BYTES = 20 * 1024 * 1024;
export const DS_MAX_ASSETS = 200;

const DS_MIME: Record<string, string> = { ...IMAGE_MIME, ...FONT_MIME };
const DS_PATH_RE = /^[\w.-]+(?:\/[\w.-]+)*$/;
const CSS_URL_RE = /url\(\s*("[^"]*"|'[^']*'|[^)'"\s]*)\s*\)/gi;
const ATTR_DS_RE = /(\s(?:src|href)\s*=\s*)(?:"ds:([^"]*)"|'ds:([^']*)'|ds:([^\s"'>]+))/gi;
const URL_DS_RE = /url\(\s*(["']?)ds:([^"')\s]*)\1\s*\)/gi;

/** Read a file relative to the design-system folder; `null` when it does not exist. */
export type DesignSystemRead = (path: string) => Promise<KitFile | null>;
type AssetUri = (path: string) => Promise<string>;

export interface DesignSystemInjection {
  /** `<style>` to inject before the content's own styles, or "". */
  style: string;
  /** The content with every `ds:` reference rewritten to a data: URI. */
  content: string;
}

/** A `ds:` (or stylesheet-relative) path, confined to the folder, image or font only. */
export function resolveDsPath(ref: string): string {
  const path = ref.replace(/^ds:/, "");
  if (!DS_PATH_RE.test(path) || path.split("/").some((s) => s === "." || s === "..")) {
    throw new Error(`${ref} must be a relative path inside the design system`);
  }
  if (!DS_MIME[extension(path)]) {
    throw new Error(`${ref} must be an image or font (${Object.keys(DS_MIME).join(", ")})`);
  }
  return path;
}

/** Data URIs per path, read one at a time so the per-deck cap stops further reads. */
async function loadAssets(paths: Iterable<string>, read: DesignSystemRead) {
  const uris = new Map<string, string>();
  let total = 0;
  for (const path of paths) {
    // oxlint-disable-next-line no-await-in-loop
    const file = await read(path);
    if (!file) throw new Error(`${path} not found in the design system`);
    const encoded = kitDataUri(file, path, DS_MIME[extension(path)]);
    if (encoded.length > DS_ASSET_MAX_BYTES) {
      throw new Error(`${path} is larger than ${DS_ASSET_MAX_BYTES / 1024 / 1024} MB`);
    }
    total += encoded.length;
    if (total > DS_DECK_MAX_BYTES) {
      throw new Error(`design-system assets are larger than ${DS_DECK_MAX_BYTES / 1024 / 1024} MB`);
    }
    uris.set(path, encoded);
  }
  return uris;
}

/** Run `replace` over every match of `re`, one match at a time. */
async function replaceAsync(
  text: string,
  re: RegExp,
  replace: (match: RegExpExecArray) => Promise<string>,
): Promise<string> {
  let out = "";
  let at = 0;
  for (const m of text.matchAll(re)) {
    // oxlint-disable-next-line no-await-in-loop
    out += text.slice(at, m.index) + (await replace(m as RegExpExecArray));
    at = m.index + m[0].length;
  }
  return out + text.slice(at);
}

/**
 * The system stylesheet, safe to inject: `@import` stripped, relative and
 * `ds:` urls (including `@font-face` sources) inlined, remote urls dropped.
 */
export async function processDesignSystemCss(css: string, asset: AssetUri): Promise<string> {
  assertSafeCss(css, false);
  const stripped = css.replace(/@import\b[^;]*;?/gi, "");
  const out = await replaceAsync(stripped, CSS_URL_RE, async ([whole, arg]) => {
    const quote = /^["']/.test(arg) ? arg[0] : "";
    const value = (quote ? arg.slice(1, -1) : arg).trim();
    if (/^data:/i.test(value)) return whole;
    if (!/^ds:/i.test(value) && /^(?:[a-z][\w+.-]*:|\/)/i.test(value)) return "none";
    return `url(${quote}${await asset(resolveDsPath(value.replace(/^\.\//, "")))}${quote})`;
  });
  assertSafeCss(out, true);
  return out;
}

/**
 * Fail closed rather than sanitize: no way to close the `<style>`, no CSS
 * escapes, and in the final CSS no `@import` and only data: urls.
 */
function assertSafeCss(css: string, final: boolean): void {
  const fail = (reason: string) => {
    throw new Error(`${DS_STYLESHEET} ${reason}`);
  };
  if (/<\/style/i.test(css)) fail('must not contain "</style"');
  if (css.includes("\\")) fail("must not contain backslash escapes");
  if (/image-set\(/i.test(css)) fail("must not use image-set()");
  if (!final) return;
  if (/@import/i.test(css)) fail("must not use @import");
  if (/url\((?!\s*["']?data:)/i.test(css)) fail("has a url() that is not a data: URI");
}

/** Rewrite `ds:` in `src` and `href` attributes and CSS `url()` to data: URIs. */
export async function rewriteDsReferences(content: string, asset: AssetUri): Promise<string> {
  const attrs = await replaceAsync(content, ATTR_DS_RE, async ([, lead, dq, sq, bare]) => {
    const ref = `ds:${dq ?? sq ?? bare}`;
    return `${lead}"${await asset(resolveDsPath(ref))}"`;
  });
  return replaceAsync(
    attrs,
    URL_DS_RE,
    async ([, quote, path]) => `url(${quote}${await asset(resolveDsPath(`ds:${path}`))}${quote})`,
  );
}

/**
 * Load the system's stylesheet and the assets `content` references. Throws an
 * Error whose message is user-facing.
 */
export async function injectDesignSystem(
  content: string,
  read: DesignSystemRead,
): Promise<DesignSystemInjection> {
  // A dry pass validates and counts every path before any asset is read.
  const paths = new Set<string>();
  const collect: AssetUri = async (path) => {
    paths.add(path);
    if (paths.size > DS_MAX_ASSETS) {
      throw new Error(`the deck references more than ${DS_MAX_ASSETS} design-system assets`);
    }
    return "data:,";
  };
  await rewriteDsReferences(content, collect);
  const sheet = await read(DS_STYLESHEET);
  const source = sheet ? kitText(sheet, DS_STYLESHEET) : "";
  if (source) await processDesignSystemCss(source, collect);
  const uris = await loadAssets(paths, read);
  const asset: AssetUri = async (path) => uris.get(path)!;
  const css = source ? await processDesignSystemCss(source, asset) : "";
  const rewritten = await rewriteDsReferences(content, asset);
  return {
    style: css ? `<style data-omnigent-design-system>\n${css}\n</style>` : "",
    content: rewritten,
  };
}

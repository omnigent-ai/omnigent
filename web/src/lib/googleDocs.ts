const GOOGLE_DOCS_ORIGIN = "https://docs.google.com";
const EDITOR_PATH =
  /^\/document\/(?:u\/[0-9]+\/)?d\/([A-Za-z0-9_-]+)(?:\/edit)?\/?(?:\?([^#]*))?(?:#(.*))?$/;
const TAB_ID = /^t\.[A-Za-z0-9_-]+$/;
const CONTEXT_QUERY_PARAMS = new Set([
  "authuser",
  "ouid",
  "pli",
  "sharing",
  "usp",
  "utm_campaign",
  "utm_content",
  "utm_medium",
  "utm_source",
  "utm_term",
]);

const URL_MAX_LENGTH = 2048;

export interface GoogleDocsReference {
  documentId: string;
  tabId: string | null;
  canonicalUrl: string;
}

export type GoogleDocsLink =
  | { kind: "embed"; reference: GoogleDocsReference }
  | { kind: "external"; originalUrl: string }
  | { kind: "invalid"; reason: string };

/** Classify a pasted or chat URL without discarding an external navigation target. */
export function classifyGoogleDocsUrl(originalUrl: string): GoogleDocsLink {
  const url = originalUrl.trim();
  const originLength = GOOGLE_DOCS_ORIGIN.length;
  const match = EDITOR_PATH.exec(url.slice(originLength));
  // URL parsers may repair malformed paths or strip control characters.
  if (
    url.slice(0, originLength).toLowerCase() !== GOOGLE_DOCS_ORIGIN ||
    /[\s\u0085\\]/.test(url) ||
    Array.from(url).some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127) ||
    !match
  ) {
    return { kind: "invalid", reason: "Expected an HTTPS Google Docs editor URL" };
  }

  const [, documentId, query = "", fragment = ""] = match;
  const queryParams = new URLSearchParams("?" + query);
  const fragmentParams = new URLSearchParams("?" + fragment);
  // External access and navigation targets can depend on the full original link.
  if (
    [...queryParams.keys()].some((key) => key !== "tab" && !CONTEXT_QUERY_PARAMS.has(key)) ||
    (fragment &&
      (!fragmentParams.has("tab") || [...fragmentParams.keys()].some((key) => key !== "tab")))
  ) {
    return { kind: "external", originalUrl };
  }

  const queryTabs = queryParams.getAll("tab");
  const fragmentTabs = fragmentParams.getAll("tab");
  if (
    queryTabs.length > 1 ||
    fragmentTabs.length > 1 ||
    [...queryTabs, ...fragmentTabs].some((tab) => !TAB_ID.test(tab))
  ) {
    return { kind: "invalid", reason: "Invalid Google Docs tab" };
  }

  const [queryTab] = queryTabs;
  const [fragmentTab] = fragmentTabs;
  if (queryTab && fragmentTab && queryTab !== fragmentTab) {
    return { kind: "external", originalUrl };
  }

  const tabId = queryTab ?? fragmentTab ?? null;
  const canonicalUrl =
    GOOGLE_DOCS_ORIGIN + "/document/d/" + documentId + "/edit" + (tabId ? "?tab=" + tabId : "");
  if (canonicalUrl.length > URL_MAX_LENGTH) {
    return { kind: "invalid", reason: "Google Docs URL exceeds 2048 characters" };
  }
  return { kind: "embed", reference: { documentId, tabId, canonicalUrl } };
}

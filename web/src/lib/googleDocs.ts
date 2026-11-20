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

export const GOOGLE_DOCS_SELECTION_LABEL = "ui.google_docs_url";
export const GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX = "ui.google_docs.document.";
const LABEL_KEY_MAX_LENGTH = 128;
const LABEL_VALUE_MAX_LENGTH = 256;

export interface GoogleDocsReference {
  documentId: string;
  tabId: string | null;
  canonicalUrl: string;
}

export type GoogleDocsLink =
  | { kind: "embed"; reference: GoogleDocsReference }
  | { kind: "external"; originalUrl: string }
  | { kind: "invalid"; reason: string };

export interface GoogleDocsSessionState {
  selected: GoogleDocsReference | null;
  documents: ReadonlyMap<string, GoogleDocsReference>;
}

function documentLabel(documentId: string): string {
  return GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + documentId;
}

/** Classify a pasted or chat URL without discarding an external navigation target. */
export function classifyGoogleDocsUrl(originalUrl: string): GoogleDocsLink {
  const url = originalUrl.trim();
  const originLength = GOOGLE_DOCS_ORIGIN.length;
  const match = EDITOR_PATH.exec(url.slice(originLength));
  // URL parsers may repair malformed paths or strip control characters.
  if (
    url.slice(0, originLength).toLowerCase() !== GOOGLE_DOCS_ORIGIN ||
    /[\s\\]/.test(url) ||
    Array.from(url).some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127) ||
    !match
  ) {
    return { kind: "invalid", reason: "Expected an HTTPS Google Docs editor URL" };
  }

  const [, documentId, query = "", fragment = ""] = match;
  const queryParams = new URLSearchParams("?" + query);
  const fragmentParams = new URLSearchParams("?" + fragment);
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
  if (
    [...queryParams.keys()].some((key) => key !== "tab" && !CONTEXT_QUERY_PARAMS.has(key)) ||
    (fragment && (!fragmentTab || [...fragmentParams.keys()].some((key) => key !== "tab"))) ||
    (queryTab && fragmentTab && queryTab !== fragmentTab)
  ) {
    return { kind: "external", originalUrl };
  }

  const tabId = queryTab ?? fragmentTab ?? null;
  const canonicalUrl =
    GOOGLE_DOCS_ORIGIN + "/document/d/" + documentId + "/edit" + (tabId ? "?tab=" + tabId : "");
  if (documentLabel(documentId).length > LABEL_KEY_MAX_LENGTH) {
    return { kind: "invalid", reason: "Google Docs label key exceeds 128 characters" };
  }
  if (canonicalUrl.length > LABEL_VALUE_MAX_LENGTH) {
    return { kind: "invalid", reason: "Google Docs URL exceeds 256 characters" };
  }
  return { kind: "embed", reference: { documentId, tabId, canonicalUrl } };
}

function readReference(url: string | undefined): GoogleDocsReference | null {
  if (!url) return null;
  const link = classifyGoogleDocsUrl(url);
  return link.kind === "embed" ? link.reference : null;
}

/** Read selected-only legacy labels and matching document entries. */
export function readGoogleDocsLabels(
  labels: Readonly<Record<string, string>>,
): GoogleDocsSessionState {
  const documents = new Map<string, GoogleDocsReference>();
  for (const [key, url] of Object.entries(labels)) {
    if (!key.startsWith(GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX)) continue;
    const reference = readReference(url);
    if (reference && key === documentLabel(reference.documentId)) {
      documents.set(reference.documentId, reference);
    }
  }
  const selected = readReference(labels[GOOGLE_DOCS_SELECTION_LABEL]);
  if (selected) documents.set(selected.documentId, selected);
  return { selected, documents };
}

/** Build the atomic label update for an explicit save. Null unlinks. */
export function buildGoogleDocsLabelPatch(
  labels: Readonly<Record<string, string>>,
  url: string | null,
): Record<string, string> {
  let selected: GoogleDocsReference | null = null;
  if (url !== null) {
    const link = classifyGoogleDocsUrl(url);
    if (link.kind === "external")
      throw new Error("This Google Docs target must be opened in Google Docs");
    if (link.kind === "invalid") throw new Error(link.reason);
    selected = link.reference;
  }
  const previous = readReference(labels[GOOGLE_DOCS_SELECTION_LABEL]);
  const patch: Record<string, string> = {
    [GOOGLE_DOCS_SELECTION_LABEL]: selected?.canonicalUrl ?? "",
  };
  if (previous && previous.documentId !== selected?.documentId) {
    const previousKey = documentLabel(previous.documentId);
    // Legacy selections can lack a document row entirely.
    if (labels[previousKey]) patch[previousKey] = "";
  }
  if (selected) patch[documentLabel(selected.documentId)] = selected.canonicalUrl;
  return patch;
}

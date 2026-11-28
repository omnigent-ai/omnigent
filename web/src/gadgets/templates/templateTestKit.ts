// Shared mechanics for the template tests: the JSON Schema keywords the server accepts, the
// in-memory collections a page writes to, and booting a template's page in a VM context against
// jsdom. Scenarios and assertions stay in each template's own test.
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";

/**
 * The JSON Schema keywords a revision's collections may use. The server's list
 * (`_SCHEMA_KEYS` in omnigent/gadgets/service.py) is checked against this file by
 * tests/gadgets/test_gadget_schema_keywords.py. Relative to the vitest root (web/).
 */
export const SERVER_SCHEMA_KEYWORDS: ReadonlySet<string> = new Set(
  JSON.parse(readFileSync("src/gadgets/templates/gadgetSchemaKeywords.json", "utf8")) as string[],
);

/** Every keyword a schema uses, through its properties, items and additionalProperties. */
export function schemaKeywords(schema: unknown, out = new Set<string>()): Set<string> {
  if (schema && typeof schema === "object" && !Array.isArray(schema)) {
    for (const [key, value] of Object.entries(schema)) {
      out.add(key);
      if (key === "properties") {
        Object.values(value as Record<string, unknown>).forEach((child) =>
          schemaKeywords(child, out),
        );
      } else if (key === "items" || key === "additionalProperties") {
        schemaKeywords(value, out);
      }
    }
  }
  return out;
}

/** Bytes in a value's compact JSON, as the server measures a revision's collections. */
export function jsonBytes(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).length;
}

export interface TemplateDoc {
  id: string;
  version: string;
  value: unknown;
  updatedAt?: string;
}
export type TemplateListener = (snapshot: { status: string; items: unknown[] }) => void;
export interface TemplateWrite {
  collection: string;
  op: string;
  id: string;
  value: unknown;
}

/**
 * In-memory collections for a page under test: each records the page's writes and keeps the
 * listener the page watches it with, so a test can push snapshots.
 */
export function recordingCollections() {
  const listeners: Record<string, TemplateListener> = {};
  const stored: Record<string, Record<string, TemplateDoc>> = {};
  const writes: TemplateWrite[] = [];
  /** Continuation pages a test serves, by collection and cursor; an `Error` fails that read. */
  const pages: Record<
    string,
    Record<string, { items: TemplateDoc[]; nextCursor: string | null } | Error>
  > = {};
  /** How many of the next writes to a collection fail, by collection. */
  const failing: Record<string, number> = {};
  const failWrite = (name: string) => {
    if (!failing[name]) return false;
    failing[name] -= 1;
    return true;
  };
  const collection = (name: string) => {
    const docs = (stored[name] ??= {});
    return {
      watch: (listener: TemplateListener) => {
        listeners[name] = listener;
        return { dispose: () => undefined };
      },
      list: (page: { cursor?: string }) => {
        const served = pages[name]?.[page?.cursor ?? ""] ?? { items: [], nextCursor: null };
        return served instanceof Error ? Promise.reject(served) : Promise.resolve(served);
      },
      get: (id: string) => Promise.resolve(docs[id] ?? null),
      create: (value: unknown, options: { id?: string }) => {
        if (failWrite(name)) return Promise.reject(new Error("write failed"));
        const doc = { id: options?.id ?? "x", version: "v1", value };
        writes.push({ collection: name, op: "create", id: doc.id, value });
        docs[doc.id] = doc;
        return Promise.resolve(doc);
      },
      replace: (id: string, value: unknown) => {
        if (failWrite(name)) return Promise.reject(new Error("write failed"));
        const doc = { id, version: "v2", value };
        writes.push({ collection: name, op: "replace", id, value });
        docs[id] = doc;
        return Promise.resolve(doc);
      },
      delete: () => Promise.resolve({ deleted: true }),
    };
  };
  return { listeners, writes, pages, failing, collection };
}

/**
 * Runs the page's scripts in order in one context that is its own window, against jsdom, with
 * `client` as the SDK's connection. As in a browser, a script that throws reports the error to
 * the window's error listeners and the next script still runs; unless `allowErrors`, the boot
 * then fails with the first error, so a broken page cannot pass by its own error handling.
 */
export function bootTemplatePage(
  html: string,
  client: unknown,
  { allowErrors = false }: { allowErrors?: boolean } = {},
): void {
  const body = html.slice(html.indexOf("<body>") + "<body>".length, html.indexOf("</body>"));
  const scripts = [...body.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
  document.body.innerHTML = body.replace(/<script[^>]*>[\s\S]*?<\/script>/g, "");
  const windowListeners: Record<string, ((event: unknown) => void)[]> = {};
  const context: Record<string, unknown> = {
    document,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => undefined,
    omnigent: { connect: () => Promise.resolve(client), isOmnigentError: () => false },
    addEventListener: (type: string, listener: (event: unknown) => void) => {
      (windowListeners[type] ??= []).push(listener);
    },
  };
  context.window = context;
  createContext(context);
  const thrown: unknown[] = [];
  for (const script of scripts) {
    try {
      runInContext(script, context);
    } catch (error) {
      thrown.push(error);
      (windowListeners.error ?? []).forEach((listener) =>
        listener({ error, message: String(error) }),
      );
    }
  }
  if (thrown.length > 0 && !allowErrors) {
    throw new Error(`A script in the page threw: ${String(thrown[0])}`);
  }
}

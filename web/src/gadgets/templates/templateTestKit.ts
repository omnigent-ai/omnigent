// Shared mechanics for the template tests: the JSON Schema keywords the server accepts, and
// booting a template's page in a VM context against jsdom. Scenarios and assertions stay in
// each template's own test.
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

/**
 * Runs the page's scripts in order in one context that is its own window, against jsdom, with
 * `client` as the SDK's connection. As in a browser, a script that throws reports the error to
 * the window's error listeners and the next script still runs.
 */
export function bootTemplatePage(html: string, client: unknown): void {
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
  for (const script of scripts) {
    try {
      runInContext(script, context);
    } catch (error) {
      (windowListeners.error ?? []).forEach((listener) =>
        listener({ error, message: String(error) }),
      );
    }
  }
}

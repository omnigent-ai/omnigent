import { runInNewContext } from "node:vm";

import { waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { WORK_MAP_HTML, WORK_MAP_REVISION } from "./workMapTemplate";

interface Doc {
  id: string;
  version: string;
  value: unknown;
  updatedAt?: string;
}
type Listener = (snapshot: { status: string; items: unknown[] }) => void;

const MAP = {
  scanned_at: "2026-11-19T10:00:00Z",
  window_days: 3,
  sessions_read: 9,
  headline: "Two efforts are moving and one review blocks a fix.",
  areas: [
    {
      id: "rollout",
      title: "Checkout flag rollout",
      state: "active",
      related: ["reviews"],
      items: [{ id: "ramp", title: "Ramp to 5%", state: "done" }],
      next_steps: [
        {
          id: "ramp-10",
          label: "Ramp the flag to 10%",
          kind: "act",
          why: "Errors stayed flat.",
          evidence: { session_id: "s1", quote: "no change in 5xx" },
        },
        { id: "check-p95", label: "Check p95 after the ramp", kind: "monitor" },
      ],
    },
    {
      id: "reviews",
      title: "Reviews",
      state: "blocked",
      next_steps: [{ id: "review-fix", label: "Review the cache fix", kind: "act" }],
    },
  ],
  sessions: [
    { id: "s1", title: "Ramp checkout flag", area: "rollout", why: "Watch errors", status: "idle" },
  ],
};

// The real inline script runs against jsdom with the SDK faked at window.omnigent, the external
// boundary. Each collection's watch listener is kept so the test can push snapshots.
function startMap({
  preview = false,
  canAct = true,
  canWrite = true,
}: { preview?: boolean; canAct?: boolean; canWrite?: boolean } = {}) {
  const listeners: Record<string, Listener> = {};
  let actionListener: Listener | null = null;
  const creates: { collection: string; value: unknown; options: unknown }[] = [];
  const requests: { action: string; params: unknown }[] = [];
  const collection = (name: string) => ({
    watch: (listener: Listener) => {
      listeners[name] = listener;
      return { dispose: () => undefined };
    },
    create: (value: unknown, options: { id?: string }) => {
      creates.push({ collection: name, value, options });
      return Promise.resolve({ id: options?.id ?? "x", version: "v1", value });
    },
    delete: () => Promise.resolve({ deleted: true }),
  });
  const client = {
    context: {
      app: { id: "app1", name: "My work map" },
      viewer: { id: "1001", displayName: "viewer@example.com" },
      permissions: { writeData: canWrite, publish: canAct },
      preview,
    },
    hasCapability: () => true,
    can: (requirement: string) => (requirement === "write" ? canWrite : canAct),
    ui: { colorScheme: "light" },
    data: { collection },
    sessions: { canList: true, list: () => Promise.resolve({ items: [] }) },
    navigation: { openSession: () => Promise.resolve({ opened: true }) },
    agent: {
      request: (action: string, params: unknown) => {
        requests.push({ action, params });
        return Promise.resolve({ actionId: "a1", delivery: "sent" });
      },
      watch: (listener: Listener) => {
        actionListener = listener;
        return { dispose: () => undefined, refresh: () => undefined };
      },
    },
  };
  const bodyStart = WORK_MAP_HTML.indexOf("<body>") + "<body>".length;
  const scriptStart = WORK_MAP_HTML.lastIndexOf("<script>");
  const scriptEnd = WORK_MAP_HTML.lastIndexOf("</script>");
  document.body.innerHTML = WORK_MAP_HTML.slice(bodyStart, scriptStart);
  runInNewContext(WORK_MAP_HTML.slice(scriptStart + "<script>".length, scriptEnd), {
    window: { omnigent: { connect: () => Promise.resolve(client), isOmnigentError: () => false } },
    document,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => undefined,
  });
  const emit = (name: string, items: Doc[]) => listeners[name]?.({ status: "live", items });
  const emitActions = (items: unknown[]) => actionListener?.({ status: "live", items });
  return { emit, emitActions, creates, requests, ready: () => listeners.maps !== undefined };
}

const byId = (id: string) => document.getElementById(id) as HTMLElement;
const button = (name: string) =>
  [...document.querySelectorAll("button")].find((node) => node.textContent?.includes(name)) as
    HTMLButtonElement | undefined;

afterEach(() => {
  document.body.replaceChildren();
});

describe("work map template", () => {
  it("starts empty with a scan button and seeds nothing", async () => {
    const { emit, creates, requests, ready } = startMap();
    await waitFor(() => expect(ready()).toBe(true));
    emit("maps", []);

    await waitFor(() => expect(byId("empty").hidden).toBe(false));
    expect(byId("content").hidden).toBe(true);
    expect(creates).toHaveLength(0);

    byId("scan-empty").click();
    expect(requests).toEqual([{ action: "scan", params: { window_days: 3 } }]);
  });

  it("shows an example map in a template preview", async () => {
    const { emit, creates, ready } = startMap({ preview: true, canAct: false });
    await waitFor(() => expect(ready()).toBe(true));
    emit("maps", []);

    expect(creates).toHaveLength(1);
    expect(creates[0]).toMatchObject({
      collection: "maps",
      options: { id: "current" },
      value: { example: true },
    });
    // The example shows as soon as it is saved, without waiting for the next poll.
    await waitFor(() => expect(byId("content").hidden).toBe(false));
    expect(byId("meta").textContent).toContain("Example map");
    expect(byId("empty").hidden).toBe(true);
  });

  it("draws the map, ranks blocked work first and runs a next step", async () => {
    const { emit, emitActions, requests, ready } = startMap();
    await waitFor(() => expect(ready()).toBe(true));
    emit("maps", [{ id: "current", version: "v1", value: MAP }]);

    await waitFor(() => expect(byId("content").hidden).toBe(false));
    expect(document.querySelectorAll("#map .node")).toHaveLength(2);
    expect(document.querySelectorAll("#map .edge")).toHaveLength(1);
    expect(byId("headline").textContent).toBe(MAP.headline);
    const chips = [...document.querySelectorAll("#next-list .next-chip")].map(
      (chip) => chip.textContent,
    );
    expect(chips[0]).toContain("Review the cache fix");
    expect(byId("card-rollout").textContent).toContain("Ramp checkout flag");

    const step = byId("card-rollout").querySelector(".step-main") as HTMLButtonElement;
    step.click();
    expect(requests).toEqual([
      {
        action: "do_next_step",
        params: { next_step: "ramp-10", label: "Ramp the flag to 10%", area: "rollout" },
      },
    ]);

    emitActions([
      { id: "a1", action: "do_next_step", params: { next_step: "ramp-10" }, status: "running" },
    ]);
    await waitFor(() => expect(byId("card-rollout").textContent).toContain("Working on it"));

    emit("step_runs", [
      {
        id: "ramp-10",
        version: "v2",
        value: { label: "Ramp the flag to 10%", state: "done", outcome: "At 10%." },
      },
    ]);
    emitActions([
      { id: "a1", action: "do_next_step", params: { next_step: "ramp-10" }, status: "done" },
    ]);
    await waitFor(() => expect(byId("card-rollout").textContent).toContain("Done: At 10%."));
    expect(byId("done-section").hidden).toBe(false);
  });

  it("leaves out next steps the viewer hid", async () => {
    const { emit, ready } = startMap();
    await waitFor(() => expect(ready()).toBe(true));
    emit("maps", [{ id: "current", version: "v1", value: MAP }]);
    emit("dismissed", [{ id: "check-p95", version: "v1", value: { at: "2026-11-19T11:00:00Z" } }]);

    await waitFor(() => expect(byId("card-rollout").textContent).toContain("1 next step hidden"));
    expect(byId("card-rollout").textContent).not.toContain("Check p95 after the ramp");
  });

  it("tells a viewer without edit access that scanning is the owner's", async () => {
    const { emit, ready } = startMap({ canAct: false, canWrite: false });
    await waitFor(() => expect(ready()).toBe(true));
    emit("maps", [{ id: "current", version: "v1", value: MAP }]);

    await waitFor(() => expect(byId("notice").textContent).toContain("owner’s thingy session"));
    expect(button("Rescan")?.disabled).toBe(true);
    expect(document.querySelector(".step-x")).toBeNull();
  });
});

// The server refuses other JSON Schema keywords (omnigent/thingies/service.py).
const SUPPORTED_KEYWORDS = new Set([
  "type",
  "properties",
  "required",
  "additionalProperties",
  "items",
  "enum",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "exclusiveMaximum",
  "minLength",
  "maxLength",
  "minItems",
  "maxItems",
  "minProperties",
  "maxProperties",
  "title",
  "description",
  "default",
  "$schema",
]);

function keywords(schema: unknown, out: Set<string>): Set<string> {
  if (schema && typeof schema === "object" && !Array.isArray(schema)) {
    for (const [key, value] of Object.entries(schema)) {
      out.add(key);
      if (key === "properties") {
        Object.values(value as Record<string, unknown>).forEach((child) => keywords(child, out));
      } else if (key === "items" || key === "additionalProperties") {
        keywords(value, out);
      }
    }
  }
  return out;
}

describe("work map revision", () => {
  it("uses only schema keywords the server accepts", () => {
    for (const schema of Object.values(WORK_MAP_REVISION.collections)) {
      for (const keyword of keywords(schema, new Set())) {
        expect(SUPPORTED_KEYWORDS.has(keyword), keyword).toBe(true);
      }
    }
  });
});

import { createContext, runInContext } from "node:vm";

import { waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  WORK_RADAR_GUIDE,
  WORK_RADAR_HTML,
  WORK_RADAR_REVISION,
  WORK_RADAR_VIEW_END,
  WORK_RADAR_VIEW_START,
} from "./workRadarTemplate";

interface Doc {
  id: string;
  version: string;
  value: unknown;
  updatedAt?: string;
}
type Listener = (snapshot: { status: string; items: unknown[] }) => void;

const REVISION = "a".repeat(32);
const PREVIOUS = "b".repeat(32);

const RADAR = {
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

const LAYOUT = {
  structure: "Rollout stages and review lanes",
  why: "One rollout and a review queue.",
  revision_id: REVISION,
  previous_revision_id: PREVIOUS,
  changed_at: "2026-11-19T10:05:00Z",
  change_note: "Added a lane for reviews",
};

/** The template with its view region replaced, as the thingy session publishes a design. */
function withView(style: string, script: string): string {
  const start = WORK_RADAR_HTML.indexOf(WORK_RADAR_VIEW_START);
  const end = WORK_RADAR_HTML.indexOf(WORK_RADAR_VIEW_END) + WORK_RADAR_VIEW_END.length;
  const region = `${WORK_RADAR_VIEW_START}\n<style id="lanes-style">${style}</style>\n<script>${script}</script>\n${WORK_RADAR_VIEW_END}`;
  return WORK_RADAR_HTML.slice(0, start) + region + WORK_RADAR_HTML.slice(end);
}

const LANES_VIEW = withView(
  ".lane { display: block; }",
  `Radar.view({ name: 'Lanes', render: function (root, model, kit) {
    var lanes = root.firstChild || root.appendChild(kit.el('div', { class: 'lanes' }));
    kit.keyed(lanes, model.areas, function (area) { return area.id; }, function (area) {
      return kit.el('section', { class: 'lane', id: 'lane-' + area.id }, [kit.el('h3', { text: area.title })]
        .concat(kit.openSteps(area).map(function (step) { return kit.stepButton(area, step); })));
    });
  } });`,
);

// The page's scripts run in order in one context that is its own window, against jsdom, with the
// SDK faked at window.omnigent, the external boundary. As in a browser, a script that throws
// reports the error to the window's error listeners and the next script still runs.
function startRadar({
  html = WORK_RADAR_HTML,
  preview = false,
  canAct = true,
  canWrite = true,
  delivery = "sent",
}: {
  html?: string;
  preview?: boolean;
  canAct?: boolean;
  canWrite?: boolean;
  /** How the host answers a request: `starting` while the viewer has no thingy session yet. */
  delivery?: "sent" | "starting";
} = {}) {
  const listeners: Record<string, Listener> = {};
  let actionListener: Listener | null = null;
  const stored: Record<string, Record<string, Doc>> = {};
  const writes: { collection: string; op: string; id: string; value: unknown }[] = [];
  const requests: { action: string; params: unknown }[] = [];
  const automations: unknown[] = [];
  const collection = (name: string) => {
    const docs = (stored[name] ??= {});
    return {
      watch: (listener: Listener) => {
        listeners[name] = listener;
        return { dispose: () => undefined };
      },
      get: (id: string) => Promise.resolve(docs[id] ?? null),
      create: (value: unknown, options: { id?: string }) => {
        const doc = { id: options?.id ?? "x", version: "v1", value };
        writes.push({ collection: name, op: "create", id: doc.id, value });
        docs[doc.id] = doc;
        return Promise.resolve(doc);
      },
      replace: (id: string, value: unknown) => {
        const doc = { id, version: "v2", value };
        writes.push({ collection: name, op: "replace", id, value });
        docs[id] = doc;
        return Promise.resolve(doc);
      },
      delete: () => Promise.resolve({ deleted: true }),
    };
  };
  const client = {
    context: {
      app: { id: "app1", name: "Work radar" },
      revisionId: REVISION,
      viewer: { id: "1001", displayName: "viewer@example.com" },
      permissions: { writeData: canWrite, publish: canAct },
      preview,
    },
    hasCapability: () => true,
    can: (requirement: string) => (requirement === "write" ? canWrite : canAct),
    ui: { colorScheme: "light" },
    data: { collection },
    sessions: { canList: true, list: () => Promise.resolve({ items: [] }) },
    navigation: {
      openSession: () => Promise.resolve({ opened: true }),
      newAutomation: (params: unknown) => {
        automations.push(params);
        return Promise.resolve({ opened: true });
      },
    },
    agent: {
      request: (action: string, params: unknown) => {
        requests.push({ action, params });
        return Promise.resolve({ actionId: "a1", delivery });
      },
      watch: (listener: Listener) => {
        actionListener = listener;
        return { dispose: () => undefined, refresh: () => undefined };
      },
    },
  };
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
  const emit = (name: string, items: Doc[]) => listeners[name]?.({ status: "live", items });
  const emitActions = (items: unknown[]) => actionListener?.({ status: "live", items });
  const reports = () => writes.filter((write) => write.collection === "render_reports");
  return {
    emit,
    emitActions,
    writes,
    reports,
    requests,
    automations,
    ready: () => listeners.radar !== undefined,
  };
}

const byId = (elementId: string) => document.getElementById(elementId) as HTMLElement;
const button = (name: string) =>
  [...document.querySelectorAll("button")].find((node) => node.textContent?.includes(name)) as
    HTMLButtonElement | undefined;

afterEach(() => {
  document.body.replaceChildren();
});

describe("work radar template", () => {
  it("starts empty with a scan button and seeds nothing", async () => {
    const { emit, writes, requests, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", []);

    await waitFor(() => expect(byId("empty").hidden).toBe(false));
    expect(byId("content").hidden).toBe(true);
    expect(writes).toHaveLength(0);

    byId("scan-empty").click();
    expect(requests).toEqual([{ action: "scan", params: { window_days: 3 } }]);
  });

  it("shows the first scan's progress, then that it is designing the page", async () => {
    const { emit, emitActions, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", []);
    emitActions([{ id: "a1", action: "scan", params: { window_days: 3 }, status: "running" }]);

    await waitFor(() => expect(byId("progress").hidden).toBe(false));
    expect(byId("progress").children[0].getAttribute("data-now")).toBe("true");
    expect(byId("empty-run").textContent).toContain("Reading your recent sessions");
    expect(byId("empty-run").textContent).toContain("usually 1 to 3 minutes");

    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    await waitFor(() =>
      expect(byId("run").textContent).toContain("Designing a page for your work"),
    );
    expect(byId("empty").hidden).toBe(true);
  });

  it("shows an example in a template preview", async () => {
    const { emit, writes, ready } = startRadar({ preview: true, canAct: false });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", []);

    expect(writes).toHaveLength(1);
    expect(writes[0]).toMatchObject({
      collection: "radar",
      id: "current",
      value: { example: true },
    });
    // The example shows as soon as it is saved, without waiting for the next poll.
    await waitFor(() => expect(byId("content").hidden).toBe(false));
    expect(byId("meta").textContent).toContain("Example");
    expect(byId("look").textContent).toContain("designs this page around your work");
    expect(byId("empty").hidden).toBe(true);
  });

  it("draws the plain view, ranks blocked work first and runs a next step", async () => {
    const { emit, emitActions, requests, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);

    await waitFor(() => expect(byId("content").hidden).toBe(false));
    expect(byId("headline").textContent).toBe(RADAR.headline);
    const chips = [...document.querySelectorAll("#next-list .next-chip")].map(
      (chip) => chip.textContent,
    );
    expect(chips[0]).toContain("Review the cache fix");
    const cards = [...document.querySelectorAll("#view .area")].map((card) => card.id);
    expect(cards).toEqual(["card-reviews", "card-rollout"]);
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

  it("asks the viewer to start the thingy session when a scan waits for it", async () => {
    const { emit, ready } = startRadar({ delivery: "starting" });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", []);

    byId("scan-empty").click();

    await waitFor(() => expect(byId("scan-empty").textContent).toBe("Start my thingy session"));
    expect((byId("scan-empty") as HTMLButtonElement).disabled).toBe(false);
    expect(byId("empty-run").textContent).toContain("Start your thingy session in New session");
  });

  it("follows the scan still running when a later one was merged into it", async () => {
    const { emit, emitActions, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("layout", [{ id: "current", version: "v1", value: LAYOUT }]);
    emitActions([
      { id: "a0", action: "scan", params: { window_days: 3 }, status: "running" },
      {
        id: "a1",
        action: "scan",
        params: { window_days: 3 },
        status: "done",
        summary: "Merged into the equivalent open scan a0",
      },
    ]);

    await waitFor(() => expect(byId("run").textContent).toContain("Reading your recent sessions"));
    expect(button("Scanning")?.disabled).toBe(true);

    emitActions([
      { id: "a0", action: "scan", params: { window_days: 3 }, status: "done", summary: "4 areas" },
      { id: "a1", action: "scan", params: { window_days: 3 }, status: "done", summary: "Merged" },
    ]);
    await waitFor(() => expect(byId("run").textContent).toBe("Scan done"));
  });

  it("shows a finished next step as done in Next up, even once the radar drops it", async () => {
    const { emit, emitActions, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    const step = { next_step: "ramp-10", label: "Ramp the flag to 10%", area: "rollout" };
    emitActions([{ id: "a1", action: "do_next_step", params: step, status: "running" }]);
    await waitFor(() => expect(byId("card-rollout").textContent).toContain("Working on it"));

    // The session reports the action and rewrites the radar without the step, and writes no run.
    const rollout = { ...RADAR.areas[0], next_steps: RADAR.areas[0].next_steps.slice(1) };
    emit("radar", [
      { id: "current", version: "v2", value: { ...RADAR, areas: [rollout, RADAR.areas[1]] } },
    ]);
    emitActions([
      { id: "a1", action: "do_next_step", params: step, status: "done", summary: "At 10%." },
    ]);

    await waitFor(() =>
      expect(byId("next-list").querySelector(".next-chip.finished")?.textContent).toContain(
        "Ramp the flag to 10%",
      ),
    );
    const row = byId("done-list").querySelector("li") as HTMLElement;
    expect(row.dataset.fresh).toBe("true");
    expect(row.textContent).toContain("Done just now: At 10%.");
  });

  it("leaves out next steps the viewer hid", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("dismissed", [{ id: "check-p95", version: "v1", value: { at: "2026-11-19T11:00:00Z" } }]);

    await waitFor(() => expect(byId("card-rollout").textContent).toContain("1 next step hidden"));
    expect(byId("card-rollout").textContent).not.toContain("Check p95 after the ramp");
  });

  it("tells a viewer without edit access that scanning is the owner's", async () => {
    const { emit, ready } = startRadar({ canAct: false, canWrite: false });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);

    await waitFor(() => expect(byId("notice").textContent).toContain("owner’s thingy session"));
    expect(button("Rescan")?.disabled).toBe(true);
    expect(document.querySelector(".step-x")).toBeNull();
    expect(byId("automate").hidden).toBe(true);
  });

  it("opens Automations for a next step the viewer runs often", async () => {
    const { emit, automations, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", []);
    await waitFor(() => expect(byId("automate").hidden).toBe(false));

    byId("automate").click();

    expect(automations).toEqual([
      expect.objectContaining({
        name: "Next step from Work radar",
        prompt: expect.stringContaining("thingy_id app1"),
      }),
    ]);
  });
});

describe("work radar designs", () => {
  it("draws a design with the kit's pieces and reports that it drew", async () => {
    const { emit, emitActions, reports, requests, ready } = startRadar({ html: LANES_VIEW });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("layout", [{ id: "current", version: "v1", value: LAYOUT }]);

    await waitFor(() => expect(byId("lane-rollout")).toBeTruthy());
    expect(document.querySelector("#view .area")).toBeNull();
    // Next up stays the kit's.
    expect(byId("next-list").querySelectorAll(".next-chip").length).toBeGreaterThan(0);
    await waitFor(() =>
      expect(reports()).toEqual([
        expect.objectContaining({
          op: "create",
          id: REVISION,
          value: expect.objectContaining({ state: "ok", view: "Lanes" }),
        }),
      ]),
    );

    (byId("lane-rollout").querySelector(".next-chip") as HTMLButtonElement).click();
    expect(requests.at(-1)).toMatchObject({
      action: "do_next_step",
      params: { next_step: "ramp-10" },
    });
    emitActions([
      { id: "a1", action: "do_next_step", params: { next_step: "ramp-10" }, status: "running" },
    ]);
    await waitFor(() => expect(byId("lane-rollout").textContent).toContain("Working on it"));
  });

  it("says what the look is and what changed, and offers the previous look", async () => {
    const { emit, emitActions, requests, ready } = startRadar({ html: LANES_VIEW });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("layout", [{ id: "current", version: "v1", value: LAYOUT }]);

    await waitFor(() =>
      expect(byId("look").textContent).toContain("Rollout stages and review lanes"),
    );
    expect(byId("look").textContent).toContain("Added a lane for reviews");

    button("Back to the previous look")?.click();
    expect(requests.at(-1)).toEqual({ action: "restore_look", params: { revision_id: PREVIOUS } });
    emitActions([
      { id: "a1", action: "restore_look", params: { revision_id: PREVIOUS }, status: "running" },
    ]);
    await waitFor(() =>
      expect(byId("look").textContent).toContain("Going back to the previous look"),
    );
  });

  it("falls back to the plain view when a design throws while drawing", async () => {
    const html = withView(
      ".lane { color: red; }",
      "Radar.view({ name: 'Broken', render: function () { throw new Error('lanes exploded'); } });",
    );
    const { emit, reports, ready } = startRadar({ html });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("layout", [{ id: "current", version: "v1", value: LAYOUT }]);

    await waitFor(() => expect(byId("card-rollout")).toBeTruthy());
    expect(byId("lanes-style").getAttribute("media")).toBe("not all");
    await waitFor(() => expect(byId("look").dataset.kind).toBe("failed"));
    expect(byId("look").textContent).toContain("lanes exploded");
    expect(button("Back to the previous look")).toBeTruthy();
    await waitFor(() =>
      expect(reports()).toEqual([
        expect.objectContaining({
          id: REVISION,
          value: expect.objectContaining({
            state: "failed",
            error: expect.stringContaining("lanes exploded"),
          }),
        }),
      ]),
    );
  });

  it("falls back to the plain view when a design's script fails to load", async () => {
    const html = withView("", "Radar.view(lanesThatDoNotExist.render);");
    const { emit, reports, ready } = startRadar({ html });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);

    await waitFor(() => expect(byId("card-rollout")).toBeTruthy());
    expect(byId("look").textContent).toContain("could not be drawn");
    await waitFor(() =>
      expect(reports()).toEqual([
        expect.objectContaining({
          value: expect.objectContaining({
            state: "failed",
            error: expect.stringContaining("lanesThatDoNotExist"),
          }),
        }),
      ]),
    );
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

describe("work radar revision", () => {
  it("uses only schema keywords the server accepts", () => {
    for (const schema of Object.values(WORK_RADAR_REVISION.collections)) {
      for (const keyword of keywords(schema, new Set())) {
        expect(SUPPORTED_KEYWORDS).toContain(keyword);
      }
    }
  });

  it("has one view region a design replaces, after the kit and before the start", () => {
    const html = WORK_RADAR_HTML;
    expect(html.split(WORK_RADAR_VIEW_START)).toHaveLength(2);
    expect(html.split(WORK_RADAR_VIEW_END)).toHaveLength(2);
    expect(html.indexOf('<script id="radar-kit">')).toBeLessThan(
      html.indexOf(WORK_RADAR_VIEW_START),
    );
    expect(html.indexOf(WORK_RADAR_VIEW_END)).toBeLessThan(html.indexOf("Radar.start()"));
  });

  it("fits the server's limits", () => {
    expect(new TextEncoder().encode(WORK_RADAR_GUIDE).length).toBeLessThanOrEqual(16 * 1024);
    expect(new TextEncoder().encode(WORK_RADAR_HTML).length).toBeLessThan(1024 * 1024);
  });

  it("declares the actions the page requests", () => {
    expect(WORK_RADAR_REVISION.actions?.map((action) => action.name)).toEqual([
      "scan",
      "do_next_step",
      "restore_look",
    ]);
  });
});

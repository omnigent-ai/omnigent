import { waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  WHERE_WAS_I_GUIDE,
  WHERE_WAS_I_HTML,
  WHERE_WAS_I_REVISION,
  WHERE_WAS_I_VIEW_END,
  WHERE_WAS_I_VIEW_START,
} from "./whereWasITemplate";
import {
  SERVER_SCHEMA_KEYWORDS,
  bootTemplatePage,
  jsonBytes,
  schemaKeywords,
  recordingCollections,
  type TemplateDoc,
  type TemplateListener,
} from "./templateTestKit";

type Doc = TemplateDoc;
type Listener = TemplateListener;

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
          confidence: 0.8,
          why: "Errors stayed flat.",
          evidence: { session_id: "s1", quote: "no change in 5xx" },
        },
        { id: "check-p95", label: "Check p95 after the ramp", kind: "monitor", confidence: 0.75 },
      ],
    },
    {
      id: "reviews",
      title: "Reviews",
      state: "blocked",
      next_steps: [
        { id: "review-fix", label: "Review the cache fix", kind: "act", confidence: 0.9 },
      ],
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

/** The template with its view region replaced, as the gadget session publishes a design. */
function withView(style: string, script: string): string {
  const start = WHERE_WAS_I_HTML.indexOf(WHERE_WAS_I_VIEW_START);
  const end = WHERE_WAS_I_HTML.indexOf(WHERE_WAS_I_VIEW_END) + WHERE_WAS_I_VIEW_END.length;
  const region = `${WHERE_WAS_I_VIEW_START}\n<style id="lanes-style">${style}</style>\n<script>${script}</script>\n${WHERE_WAS_I_VIEW_END}`;
  return WHERE_WAS_I_HTML.slice(0, start) + region + WHERE_WAS_I_HTML.slice(end);
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
  html = WHERE_WAS_I_HTML,
  preview = false,
  canAct = true,
  canWrite = true,
  delivery = "sent",
  allowErrors = false,
}: {
  html?: string;
  preview?: boolean;
  canAct?: boolean;
  canWrite?: boolean;
  /** How the host answers a request: `starting` while the viewer has no gadget session yet. */
  delivery?: "sent" | "starting";
  /** A design that is meant to throw while the page loads. */
  allowErrors?: boolean;
} = {}) {
  const { listeners, writes, collection } = recordingCollections();
  let actionListener: Listener | null = null;
  const requests: { action: string; params: unknown }[] = [];
  const automations: unknown[] = [];
  const client = {
    context: {
      app: { id: "app1", name: "Where was I?" },
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
  bootTemplatePage(html, client, { allowErrors });
  const emit = (name: string, items: Doc[]) => listeners[name]?.({ status: "live", items });
  const emitSnapshot = (name: string, snapshot: { status: string; items: unknown[] }) =>
    listeners[name]?.(snapshot);
  const emitActions = (items: unknown[]) => actionListener?.({ status: "live", items });
  const reports = () => writes.filter((write) => write.collection === "render_reports");
  return {
    emit,
    emitSnapshot,
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

describe("where was I template", () => {
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

    expect(writes.map((write) => write.collection)).toEqual(["radar", "patterns"]);
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
    // The example shows its sure steps with why now, what it learned, and a hit ratio.
    await waitFor(() => expect(byId("hit").hidden).toBe(false));
    const shown = [...byId("next-items").children].map((item) => item.id);
    expect(shown).toEqual(["next-ramp-10", "next-checkout-telemetry", "next-review-cache"]);
    expect(byId("next-checkout-telemetry").textContent).toContain("Easy to forget.");
    expect(byId("learned-list").textContent).toContain("Remove the flag (often forgotten)");
  });

  it("draws the plain view, ranks blocked work first and runs a next step", async () => {
    const { emit, emitActions, requests, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);

    await waitFor(() => expect(byId("content").hidden).toBe(false));
    expect(byId("headline").textContent).toBe(RADAR.headline);
    const chips = [...document.querySelectorAll("#next-items .next-chip")].map(
      (chip) => chip.textContent,
    );
    expect(chips[0]).toContain("Review the cache fix");
    const cards = [...document.querySelectorAll("#view .area")].map((card) => card.id);
    expect(cards).toEqual(["card-reviews", "card-rollout"]);
    // Next up owns its steps, so the area cards do not repeat them.
    expect(byId("card-rollout").textContent).toContain("Ramp checkout flag");
    expect(byId("card-rollout").textContent).not.toContain("Ramp the flag to 10%");

    const step = byId("next-ramp-10").querySelector(".next-chip") as HTMLButtonElement;
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
    await waitFor(() => expect(byId("next-ramp-10").textContent).toContain("Working on it"));

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
    await waitFor(() => expect(byId("done-list").textContent).toContain("At 10%."));
    expect(byId("next-list").textContent).toContain("Done");
  });

  it("asks the viewer to start the gadget session when a scan waits for it", async () => {
    const { emit, ready } = startRadar({ delivery: "starting" });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", []);

    byId("scan-empty").click();

    await waitFor(() => expect(byId("scan-empty").textContent).toBe("Create my gadget session"));
    expect((byId("scan-empty") as HTMLButtonElement).disabled).toBe(false);
    expect(byId("empty-run").textContent).toContain("Create your gadget session, and it");
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
    await waitFor(() => expect(byId("next-ramp-10").textContent).toContain("Working on it"));

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

  it("shows one chip for a finished next step while its run record still says running", async () => {
    const { emit, emitActions, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    const step = { next_step: "ramp-10", label: "Ramp the flag to 10%", area: "rollout" };
    emit("step_runs", [
      {
        id: "ramp-10",
        version: "v1",
        value: { label: step.label, area: "rollout", state: "running" },
      },
    ]);
    emitActions([{ id: "a1", action: "do_next_step", params: step, status: "running" }]);
    await waitFor(() => expect(byId("next-section").textContent).toContain("Working on it"));

    // The action ends, then drops out of the ledger page, before the run record is updated.
    emitActions([{ id: "a1", action: "do_next_step", params: step, status: "done" }]);
    emitActions([]);

    await waitFor(() =>
      expect(byId("next-list").querySelector(".next-chip.finished")).not.toBeNull(),
    );
    const chips = [...byId("next-list").children].filter((chip) =>
      chip.textContent?.includes("Ramp the flag to 10%"),
    );
    expect(chips).toHaveLength(1);
    expect(byId("next-section").textContent).not.toContain("Working on it");
  });

  it("keeps counting a scan from its request after the page reloads", async () => {
    const { emit, emitActions, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("layout", [{ id: "current", version: "v1", value: LAYOUT }]);
    const requestedAt = new Date(Date.now() - 150_000).toISOString();
    const scan = { id: "a1", action: "scan", params: { window_days: 3 }, requestedAt };
    emitActions([{ ...scan, status: "running" }]);

    await waitFor(() => expect(byId("run").textContent).toContain("Reading your recent sessions"));
    expect(byId("run").textContent).toMatch(/2:3\d/);

    // The scan wrote its records: the status says so instead of reading again.
    const written = { ...RADAR, scanned_at: new Date().toISOString() };
    emit("radar", [{ id: "current", version: "v2", value: written }]);
    await waitFor(() =>
      expect(byId("run").textContent).toContain("Map updated. Finishing the scan"),
    );
  });

  it("shows text the sessions wrote without Markdown, and the look line's note as a clause", async () => {
    const { emit, ready } = startRadar({ html: LANES_VIEW });
    await waitFor(() => expect(ready()).toBe(true));
    const quoted = {
      ...RADAR.areas[0],
      next_steps: [
        {
          ...RADAR.areas[0].next_steps[0],
          evidence: { session_id: "s1", quote: "Choose **approve** or `request changes`" },
        },
      ],
    };
    emit("radar", [
      { id: "current", version: "v1", value: { ...RADAR, areas: [quoted, RADAR.areas[1]] } },
    ]);
    emit("layout", [
      { id: "current", version: "v1", value: { ...LAYOUT, change_note: "Grouped by goals." } },
    ]);

    await waitFor(() => expect(byId("look").textContent).toContain("Grouped by goals, "));
    expect(byId("look").textContent).not.toContain("goals.,");
    // Next up shows the quote; the design here only lists step buttons.
    const plainView = startRadar();
    await waitFor(() => expect(plainView.ready()).toBe(true));
    plainView.emit("radar", [
      { id: "current", version: "v1", value: { ...RADAR, areas: [quoted, RADAR.areas[1]] } },
    ]);
    await waitFor(() =>
      expect(byId("next-ramp-10").textContent).toContain("“Choose approve or request changes”"),
    );
  });

  it("waits for a newly published look instead of showing an error", async () => {
    const { emit, emitSnapshot, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emitSnapshot("radar", {
      status: "stale",
      items: [{ id: "current", version: "v1", value: RADAR }],
      error: { code: "REVISION_CHANGED", message: "A newer revision of this app was published." },
    } as { status: string; items: unknown[] });

    await waitFor(() => expect(byId("notice").textContent).toBe("Loading the new look…"));
    expect(byId("notice").getAttribute("data-kind")).toBeNull();
  });

  it("leaves out next steps the viewer hid", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("dismissed", [{ id: "check-p95", version: "v1", value: { at: "2026-11-19T11:00:00Z" } }]);

    await waitFor(() =>
      expect(byId("card-rollout").textContent).toContain("You hid a next step here."),
    );
    expect(byId("card-rollout").textContent).not.toContain("Check p95 after the ramp");
    expect(byId("next-section").textContent).not.toContain("Check p95 after the ramp");
  });

  it("tells a viewer without edit access that scanning is the owner's", async () => {
    const { emit, ready } = startRadar({ canAct: false, canWrite: false });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);

    await waitFor(() => expect(byId("notice").textContent).toContain("owner’s gadget session"));
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
        name: "Next step from Where was I?",
        prompt: expect.stringContaining("gadget_id app1"),
      }),
    ]);
  });
});

const daysAgo = (days: number) => new Date(Date.now() - days * 86_400_000).toISOString();

const PATTERNS = {
  updated_at: "2026-11-19T10:00:00Z",
  history_days: 30,
  patterns: [
    {
      id: "flag-rollout",
      name: "Flag rollout",
      bar: 0.8,
      steps: [
        { id: "ramp", label: "Ramp" },
        { id: "cleanup", label: "Remove the flag", forgotten: true },
      ],
    },
  ],
};

/** A radar whose one area has these next steps. */
function radarWith(nextSteps: Record<string, unknown>[]) {
  return {
    ...RADAR,
    areas: [
      { id: "rollout", title: "Checkout flag rollout", state: "active", next_steps: nextSteps },
    ],
  };
}

describe("where was I next up", () => {
  it("shows only the steps it is sure the viewer wants, with why now and their pattern", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("patterns", [{ id: "current", version: "v1", value: PATTERNS }]);
    emit("radar", [
      {
        id: "current",
        version: "v1",
        value: radarWith([
          {
            id: "cleanup",
            label: "Remove the checkout flag",
            kind: "act",
            confidence: 0.85,
            pattern: "flag-rollout",
            nudge: "forgotten",
            why_now: "It has been at 100% for two weeks; you usually forget this one.",
            evidence: { session_id: "s1", quote: "100% everywhere" },
          },
          // Below its pattern's bar of 0.8, though above the default.
          {
            id: "metric",
            label: "Check p95",
            kind: "monitor",
            confidence: 0.75,
            pattern: "flag-rollout",
          },
          // Sure enough with the default bar.
          { id: "review", label: "Review the fix", kind: "act", confidence: 0.72 },
          // No estimate: never shown.
          { id: "guess", label: "Write a blog post", kind: "act" },
        ]),
      },
    ]);

    await waitFor(() => expect(byId("next-items").children).toHaveLength(2));
    expect([...byId("next-items").children].map((item) => item.id)).toEqual([
      "next-cleanup",
      "next-review",
    ]);
    const item = byId("next-cleanup");
    expect(item.textContent).toContain("Easy to forget.");
    expect(item.textContent).toContain("you usually forget this one");
    expect(item.textContent).toContain("Your pattern: Flag rollout");
    expect(item.textContent).toContain("“100% everywhere”");
    expect(byId("card-rollout").textContent).not.toContain("Check p95");
    expect(byId("card-rollout").textContent).not.toContain("Write a blog post");
    // Next up never counts its steps.
    expect(byId("next-section").querySelector("h2")?.textContent).toBe("Next up");
    expect(document.body.textContent).not.toMatch(/\d+ (next )?steps?\b/);
    expect(byId("next-empty").hidden).toBe(true);
    expect(byId("learned-list").textContent).toContain("Remove the flag (often forgotten)");
  });

  it("says plainly that nothing needs the viewer when no step is sure enough", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [
      {
        id: "current",
        version: "v1",
        value: radarWith([{ id: "maybe", label: "Maybe ramp", kind: "act", confidence: 0.4 }]),
      },
    ]);

    await waitFor(() => expect(byId("next-section").hidden).toBe(false));
    expect(byId("next-empty").hidden).toBe(false);
    expect(byId("next-empty").textContent).toContain("Nothing needs you right now.");
    expect(byId("next-items").children).toHaveLength(0);
    expect(document.body.textContent).not.toContain("Maybe ramp");
  });

  it("hides a step with ×, then records the reason the viewer taps", async () => {
    const { emit, writes, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("patterns", [{ id: "current", version: "v1", value: PATTERNS }]);
    emit("radar", [
      {
        id: "current",
        version: "v1",
        value: radarWith([
          {
            id: "cleanup",
            label: "Remove the flag",
            kind: "act",
            confidence: 0.9,
            pattern: "flag-rollout",
          },
        ]),
      },
    ]);
    await waitFor(() => expect(byId("next-cleanup")).toBeTruthy());

    (byId("next-cleanup").querySelector(".step-x") as HTMLButtonElement).click();

    await waitFor(() =>
      expect(byId("notice").textContent).toContain("Hidden “Remove the flag”. Why?"),
    );
    expect(writes.at(-1)).toMatchObject({
      collection: "dismissed",
      op: "create",
      id: "cleanup",
      value: { label: "Remove the flag", pattern: "flag-rollout" },
    });
    await waitFor(() => expect(document.getElementById("next-cleanup")).toBeNull());

    (byId("notice").querySelector('[data-reason="already_done"]') as HTMLButtonElement).click();

    await waitFor(() =>
      expect(byId("notice").textContent).toBe("Thanks. Next up learns from that."),
    );
    expect(writes.at(-1)).toMatchObject({
      collection: "dismissed",
      op: "replace",
      id: "cleanup",
      value: { label: "Remove the flag", pattern: "flag-rollout", reason: "already_done" },
    });
    // The notice asked already, so Next up does not ask again.
    expect(byId("why-hidden").hidden).toBe(true);
  });

  it("still asks why a step was hidden after a reload", async () => {
    const { emit, writes, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("dismissed", [
      {
        id: "check-p95",
        version: "v1",
        value: {
          at: new Date(Date.now() - 3600_000).toISOString(),
          label: "Check p95 after the ramp",
        },
      },
      { id: "old", version: "v1", value: { at: daysAgo(9), label: "An old step" } },
      {
        id: "answered",
        version: "v1",
        value: { at: daysAgo(1), label: "Answered", reason: "not_relevant" },
      },
    ]);

    await waitFor(() => expect(byId("why-hidden").hidden).toBe(false));
    expect(byId("why-hidden").textContent).toContain("You hid “Check p95 after the ramp”. Why?");
    expect(byId("why-hidden").textContent).not.toContain("An old step");
    expect(byId("why-hidden").textContent).not.toContain("Answered");

    (byId("why-hidden").querySelector('[data-reason="not_relevant"]') as HTMLButtonElement).click();

    await waitFor(() => expect(byId("why-hidden").hidden).toBe(true));
    expect(writes.at(-1)).toMatchObject({
      collection: "dismissed",
      op: "replace",
      id: "check-p95",
      value: { label: "Check p95 after the ramp", reason: "not_relevant" },
    });
  });

  it("shows a step that did not work as a retry, with what went wrong", async () => {
    const { emit, requests, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("step_runs", [
      {
        id: "ramp-10",
        version: "v1",
        value: {
          label: "Ramp the flag to 10%",
          state: "failed",
          outcome: "No access to the flag.",
        },
      },
    ]);

    await waitFor(() =>
      expect(byId("next-ramp-10").querySelector(".next-chip")?.textContent).toContain("Try again"),
    );
    const chip = byId("next-ramp-10").querySelector(".next-chip") as HTMLButtonElement;
    expect(chip.dataset.run).toBe("failed");
    expect(byId("next-ramp-10").querySelector(".next-run")?.textContent).toContain(
      "Did not work: No access to the flag.",
    );

    chip.click();
    expect(requests.at(-1)).toMatchObject({
      action: "do_next_step",
      params: { next_step: "ramp-10" },
    });
  });

  it("shows the hit ratio quietly, counting a click as soon as the viewer makes it", async () => {
    const { emit, requests, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    const ledger = [
      { step: "cleanup", pattern: "flag-rollout", shown_at: daysAgo(1), outcome: "open" },
      { step: "a", pattern: "flag-rollout", shown_at: daysAgo(1), outcome: "clicked" },
      { step: "b", pattern: "flag-rollout", shown_at: daysAgo(2), outcome: "clicked" },
      { step: "c", shown_at: daysAgo(3), outcome: "dismissed", reason: "not_relevant" },
      // Open for over a week: ignored.
      { step: "d", shown_at: daysAgo(10), outcome: "open" },
      // Older than the window.
      { step: "e", shown_at: daysAgo(40), outcome: "clicked" },
    ];
    emit("patterns", [{ id: "current", version: "v1", value: { ...PATTERNS, ledger } }]);
    emit("radar", [
      {
        id: "current",
        version: "v1",
        value: radarWith([
          {
            id: "cleanup",
            label: "Remove the flag",
            kind: "act",
            confidence: 0.9,
            pattern: "flag-rollout",
          },
        ]),
      },
    ]);

    await waitFor(() => expect(byId("hit").hidden).toBe(false));
    expect(byId("hit").textContent).toContain(
      "Next up hit ratio, last 30 days: 2 of 4 clicked (50%)",
    );
    // The page's headline area carries no ratio; it sits under the page.
    expect(byId("headline").textContent).not.toContain("hit ratio");
    expect(byId("learned-list").textContent).toContain("Next up hits: 2 of 2");

    (byId("next-cleanup").querySelector(".next-chip") as HTMLButtonElement).click();

    expect(requests.at(-1)).toEqual({
      action: "do_next_step",
      params: {
        next_step: "cleanup",
        label: "Remove the flag",
        area: "rollout",
        pattern: "flag-rollout",
      },
    });
    await waitFor(() => expect(byId("hit").textContent).toContain("3 of 5 clicked (60%)"));
  });
});

describe("where was I designs", () => {
  it("draws a design with the kit's pieces and reports that it drew", async () => {
    const { emit, emitActions, reports, requests, ready } = startRadar({ html: LANES_VIEW });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("layout", [{ id: "current", version: "v1", value: LAYOUT }]);

    await waitFor(() => expect(byId("lane-rollout")).toBeTruthy());
    expect(document.querySelector("#view .area")).toBeNull();
    // Next up stays the kit's.
    expect(byId("next-items").querySelectorAll(".next-chip").length).toBeGreaterThan(0);
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
    const { emit, reports, ready } = startRadar({ html, allowErrors: true });
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

describe("where was I revision", () => {
  it("uses only schema keywords the server accepts", () => {
    for (const schema of Object.values(WHERE_WAS_I_REVISION.collections)) {
      for (const keyword of schemaKeywords(schema)) {
        expect(SERVER_SCHEMA_KEYWORDS).toContain(keyword);
      }
    }
  });

  it("has one view region a design replaces, after the kit and before the start", () => {
    const html = WHERE_WAS_I_HTML;
    expect(html.split(WHERE_WAS_I_VIEW_START)).toHaveLength(2);
    expect(html.split(WHERE_WAS_I_VIEW_END)).toHaveLength(2);
    expect(html.indexOf('<script id="radar-kit">')).toBeLessThan(
      html.indexOf(WHERE_WAS_I_VIEW_START),
    );
    expect(html.indexOf(WHERE_WAS_I_VIEW_END)).toBeLessThan(html.indexOf("Radar.start()"));
  });

  it("tells the session to publish a design as one edit of the view region", () => {
    expect(WHERE_WAS_I_GUIDE).toContain("`base_revision_id` the published revision and one edit");
    expect(WHERE_WAS_I_GUIDE).toContain(`start: "${WHERE_WAS_I_VIEW_START}"`);
    expect(WHERE_WAS_I_GUIDE).toContain("Do not send `html`");
    expect(WHERE_WAS_I_GUIDE).toContain("at least one visual element that carries meaning");
  });

  it("fits the server's limits", () => {
    expect(new TextEncoder().encode(WHERE_WAS_I_GUIDE).length).toBeLessThanOrEqual(16 * 1024);
    expect(new TextEncoder().encode(WHERE_WAS_I_HTML).length).toBeLessThan(1024 * 1024);
    // The server bounds the whole collections object, and how many there are.
    expect(Object.keys(WHERE_WAS_I_REVISION.collections).length).toBeLessThanOrEqual(16);
    expect(jsonBytes(WHERE_WAS_I_REVISION.collections)).toBeLessThanOrEqual(16 * 1024);
  });

  it("declares the actions the page requests", () => {
    expect(WHERE_WAS_I_REVISION.actions?.map((action) => action.name)).toEqual([
      "scan",
      "do_next_step",
      "restore_look",
    ]);
    const doStep = WHERE_WAS_I_REVISION.actions?.find((action) => action.name === "do_next_step");
    expect(Object.keys(doStep?.params?.properties ?? {})).toContain("pattern");
  });

  it("keeps the learned patterns in one record and tells the session to aim for clicks", () => {
    expect(Object.keys(WHERE_WAS_I_REVISION.collections)).toContain("patterns");
    expect(WHERE_WAS_I_REVISION.constraints).toContainEqual(
      expect.objectContaining({ name: "one_patterns", collection: "patterns", max: 1 }),
    );
    expect(WHERE_WAS_I_GUIDE).toContain("Next up is judged by its hit ratio");
    expect(WHERE_WAS_I_GUIDE).toContain("Never add a pattern from general knowledge alone");
    expect(WHERE_WAS_I_GUIDE).toContain("never fill the list");
    expect(WHERE_WAS_I_GUIDE).toContain("without counts");
  });
});

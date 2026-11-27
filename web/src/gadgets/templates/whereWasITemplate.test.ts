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
  tasks: [
    {
      id: "rollout",
      title: "Checkout flag rollout",
      state: "active",
      stage: "post_merge",
      step: "flag_rollout",
      depends_on: ["reviews"],
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
      stage: "post_pr",
      next_steps: [
        { id: "review-fix", label: "Review the cache fix", kind: "act", confidence: 0.9 },
      ],
    },
  ],
  sessions: [
    { id: "s1", title: "Ramp checkout flag", task: "rollout", why: "Watch errors", status: "idle" },
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
    kit.keyed(lanes, model.tasks, function (task) { return task.id; }, function (task) {
      return kit.el('section', { class: 'lane', id: 'lane-' + task.id }, [kit.el('h3', { text: task.title }), kit.track(task)]
        .concat(kit.openSteps(task).map(function (step) { return kit.stepButton(task, step); })));
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
  listSessions = () => Promise.resolve({ items: [] }),
}: {
  html?: string;
  preview?: boolean;
  canAct?: boolean;
  canWrite?: boolean;
  /** How the host answers a request: `starting` while the viewer has no gadget session yet. */
  delivery?: "sent" | "starting";
  /** A design that is meant to throw while the page loads. */
  allowErrors?: boolean;
  /** The viewer's session list, one page per call, as the host answers sessions.v1.list. */
  listSessions?: (params: { limit?: number; after?: string }) => Promise<unknown>;
} = {}) {
  const { listeners, writes, pages, failing, collection } = recordingCollections();
  let actionListener: Listener | null = null;
  const requests: { action: string; params: unknown }[] = [];
  const automations: unknown[] = [];
  // Whether the viewer's gadget session is being created, as the host tells the SDK.
  let sessionStarting = false;
  const startingListeners = new Set<(starting: boolean) => void>();
  const setStarting = (starting: boolean) => {
    sessionStarting = starting;
    for (const listener of startingListeners) listener(starting);
  };
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
    sessions: { canList: true, list: listSessions },
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
      starting: () => sessionStarting,
      onStarting: (listener: (starting: boolean) => void) => {
        startingListeners.add(listener);
        return () => startingListeners.delete(listener);
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
    setStarting,
    pages,
    failing,
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
    expect(shown).toEqual(["next-ramp-25", "next-rerun-ci"]);
    expect(byId("next-ramp-25").textContent).toContain("Due now.");
    expect(byId("next-ramp-25").textContent).toContain("How search-service ships: Flagged change");
    expect(byId("learned-list").textContent).toContain("Impact report (often forgotten)");
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
    const cards = [...document.querySelectorAll("#view .task")].map((card) => card.id);
    expect(cards).toEqual(["card-reviews", "card-rollout"]);
    // Next up owns its steps, so the task rows do not repeat them.
    expect(byId("card-rollout").textContent).toContain("Ramp checkout flag");
    expect(byId("card-rollout").textContent).not.toContain("Ramp the flag to 10%");

    const step = byId("next-ramp-10").querySelector(".next-chip") as HTMLButtonElement;
    step.click();
    expect(requests).toEqual([
      {
        action: "do_next_step",
        params: { next_step: "ramp-10", label: "Ramp the flag to 10%", task: "rollout" },
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

  it("treats IDs and values such as constructor like any other", async () => {
    const { emit, requests, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [
      {
        id: "current",
        version: "v1",
        value: {
          ...RADAR,
          tasks: [
            {
              id: "constructor",
              title: "Builder cleanup",
              state: "constructor",
              depends_on: ["toString"],
              next_steps: [
                {
                  id: "constructor",
                  label: "Delete the old builder",
                  kind: "act",
                  confidence: 0.9,
                  nudge: "constructor",
                },
              ],
            },
          ],
        },
      },
    ]);

    await waitFor(() => expect(byId("content").hidden).toBe(false));
    expect(byId("card-constructor").textContent).toContain("Builder cleanup");
    const chip = byId("next-constructor").querySelector(".next-chip") as HTMLButtonElement;
    expect(chip.textContent).toContain("Delete the old builder");
    expect(byId("next-constructor").textContent).not.toContain("function");
    chip.click();
    expect(requests).toEqual([
      {
        action: "do_next_step",
        params: { next_step: "constructor", label: "Delete the old builder", task: "constructor" },
      },
    ]);
  });

  it("says when a scan's page budget ended before every session was listed", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [
      { id: "current", version: "v1", value: { ...RADAR, sessions_unchecked: true } },
    ]);
    await waitFor(() =>
      expect(byId("meta").textContent).toContain("older sessions not all checked"),
    );
    // Sessions are listed by creation, so the guide reads a fixed number of pages by activity
    // instead of stopping at the first page created before the window.
    expect(WHERE_WAS_I_GUIDE).toContain("at most 4 pages");
    expect(WHERE_WAS_I_GUIDE).not.toContain("until rows were created before the window");
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

  it("shows a scan waiting while its gadget session is being created, then ready again", async () => {
    const { emit, ready, requests, setStarting } = startRadar({ delivery: "starting" });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", []);
    byId("scan-empty").click();
    await waitFor(() => expect(byId("scan-empty").textContent).toBe("Create my gadget session"));

    // The viewer clicked Create session: the scan waits instead of asking again.
    setStarting(true);
    await waitFor(() => expect(byId("scan-empty").textContent).toBe("Starting session…"));
    const scan = byId("scan-empty") as HTMLButtonElement;
    expect(scan.disabled).toBe(true);
    expect(scan.hasAttribute("aria-busy")).toBe(true);
    scan.click();
    expect(requests).toHaveLength(1);

    // The create failed or was left: the viewer can start it again.
    setStarting(false);
    await waitFor(() => expect(byId("scan-empty").textContent).toBe("Create my gadget session"));
    expect((byId("scan-empty") as HTMLButtonElement).disabled).toBe(false);
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
      { id: "a0", action: "scan", params: { window_days: 3 }, status: "done", summary: "4 tasks" },
      { id: "a1", action: "scan", params: { window_days: 3 }, status: "done", summary: "Merged" },
    ]);
    await waitFor(() => expect(byId("run").textContent).toBe("Scan done"));
  });

  it("shows a finished next step as done in Next up, even once the radar drops it", async () => {
    const { emit, emitActions, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    const step = { next_step: "ramp-10", label: "Ramp the flag to 10%", task: "rollout" };
    emitActions([{ id: "a1", action: "do_next_step", params: step, status: "running" }]);
    await waitFor(() => expect(byId("next-ramp-10").textContent).toContain("Working on it"));

    // The session reports the action and rewrites the radar without the step, and writes no run.
    const rollout = { ...RADAR.tasks[0], next_steps: RADAR.tasks[0].next_steps.slice(1) };
    emit("radar", [
      { id: "current", version: "v2", value: { ...RADAR, tasks: [rollout, RADAR.tasks[1]] } },
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
    const step = { next_step: "ramp-10", label: "Ramp the flag to 10%", task: "rollout" };
    emit("step_runs", [
      {
        id: "ramp-10",
        version: "v1",
        value: { label: step.label, task: "rollout", state: "running" },
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

  it("offers a step again, on a click, whose old run record still says running", async () => {
    // A fresh page: the run record says running, written hours ago, and no action is tracked
    // for it any more (its runner stopped before updating the record).
    const { emit, emitActions, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    const step = { label: "Ramp the flag to 10%", task: "rollout" };
    const hoursAgo = new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString();
    emit("step_runs", [
      {
        id: "ramp-10",
        version: "v1",
        value: { ...step, state: "running", updated: hoursAgo },
      },
    ]);
    emitActions([]);

    await waitFor(() =>
      expect(byId("next-section").textContent).toContain("Stopped without saying it finished"),
    );
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
      ...RADAR.tasks[0],
      next_steps: [
        {
          ...RADAR.tasks[0].next_steps[0],
          evidence: { session_id: "s1", quote: "Choose **approve** or `request changes`" },
        },
      ],
    };
    emit("radar", [
      { id: "current", version: "v1", value: { ...RADAR, tasks: [quoted, RADAR.tasks[1]] } },
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
      { id: "current", version: "v1", value: { ...RADAR, tasks: [quoted, RADAR.tasks[1]] } },
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

  it("reads hidden steps and runs on later pages instead of taking them as gone", async () => {
    const { emitSnapshot, emit, pages, writes, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    // A short first page: the server's budget can end a page before its limit.
    pages.dismissed = {
      p2: {
        items: [{ id: "check-p95", version: "v1", value: { at: "2026-11-19T11:00:00Z" } }],
        nextCursor: null,
      },
    };
    emitSnapshot("dismissed", { status: "live", items: [], nextCursor: "p2" } as never);

    await waitFor(() =>
      expect(byId("card-rollout").textContent).toContain("You hid a next step here."),
    );
    expect(byId("next-section").textContent).not.toContain("Check p95 after the ramp");
    // Known as hidden, so nothing creates it again.
    expect(writes.filter((write) => write.collection === "dismissed")).toEqual([]);

    pages.step_runs = {
      p2: {
        items: [
          {
            id: "ramp-10",
            version: "v1",
            value: { label: "Ramp the flag to 10%", state: "running" },
          },
        ],
        nextCursor: null,
      },
    };
    emitSnapshot("step_runs", { status: "live", items: [], nextCursor: "p2" } as never);
    await waitFor(() => expect(document.body.textContent).toContain("Working on it"));
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

/** A radar whose one task has these next steps. */
function radarWith(nextSteps: Record<string, unknown>[]) {
  return {
    ...RADAR,
    tasks: [
      {
        id: "rollout",
        title: "Checkout flag rollout",
        state: "active",
        stage: "post_merge",
        next_steps: nextSteps,
      },
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
    // The page's headline carries no ratio; it sits under the page.
    expect(byId("headline").textContent).not.toContain("hit ratio");
    expect(byId("learned-list").textContent).toContain("Next up hits: 2 of 2");

    (byId("next-cleanup").querySelector(".next-chip") as HTMLButtonElement).click();

    expect(requests.at(-1)).toEqual({
      action: "do_next_step",
      params: {
        next_step: "cleanup",
        label: "Remove the flag",
        task: "rollout",
        pattern: "flag-rollout",
      },
    });
    await waitFor(() => expect(byId("hit").textContent).toContain("3 of 5 clicked (60%)"));
  });
});

const TRACKED = {
  scanned_at: "2026-11-19T10:00:00Z",
  window_days: 3,
  sessions_read: 6,
  tasks: [
    {
      id: "cache-fix",
      title: "Cache warm-up fix",
      state: "waiting",
      stage: "post_pr",
      step: "merge_deps",
      depends_on: ["client"],
      links: [{ kind: "pr", label: "PR #1490", url: "https://example.com/pr/1490" }],
      track: [
        { id: "commit", label: "Commit and push", stage: "pre_pr", status: "done" },
        { id: "approval", label: "Get approval", stage: "post_pr", status: "done" },
        { id: "merge_deps", label: "Merge dependencies", stage: "post_pr", status: "blocked" },
        { id: "merge", label: "Merge", stage: "post_pr", status: "todo" },
        { id: "flag_rollout", label: "Ramp the flag", stage: "post_merge", status: "na" },
        { id: "metrics", label: "Check metrics and errors", stage: "post_rollout", status: "na" },
      ],
    },
    {
      id: "client",
      title: "Client 4.2 release",
      state: "active",
      stage: "post_pr",
      step: "ci",
      next_steps: [
        {
          id: "rerun-ci",
          label: "Rerun the UI shard",
          kind: "act",
          confidence: 0.8,
          track_step: "ci",
        },
      ],
    },
    { id: "docs", title: "Docs fix", state: "done", stage: "done" },
  ],
  sessions: [
    { id: "s1", title: "Cache warm-up", task: "cache-fix", status: "idle" },
    { id: "s2", title: "Client CI", task: "client", status: "idle" },
    { id: "s3", title: "Read about indexes", other: "Research", status: "idle" },
    { id: "s4", title: "Orphan session", task: "gone", status: "idle" },
  ],
};

describe("where was I tasks", () => {
  it("lays every task on the same four stages, with done, blocked and not-needed steps", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: TRACKED }]);

    await waitFor(() => expect(byId("card-cache-fix")).toBeTruthy());
    const stages = (taskId: string) =>
      [...byId(`card-${taskId}`).querySelectorAll(".track-stage")].map((stage) =>
        stage.getAttribute("data-stage"),
      );
    expect(stages("cache-fix")).toEqual(["pre_pr", "post_pr", "post_merge", "post_rollout"]);
    expect(stages("client")).toEqual(["pre_pr", "post_pr", "post_merge", "post_rollout"]);
    const dots = [...byId("card-cache-fix").querySelectorAll(".track-dot")].map((dot) =>
      dot.getAttribute("data-s"),
    );
    expect(dots).toEqual(["done", "done", "blocked", "todo", "na", "na"]);
    expect(byId("card-cache-fix").querySelector(".track")?.getAttribute("aria-label")).toBe(
      "Commit and push done, Get approval done, Merge dependencies blocked, Merge to do",
    );
    // A stage with only steps it doesn't need reads as empty, not as work to do.
    expect(
      byId("card-cache-fix")
        .querySelector('.track-stage[data-stage="post_merge"]')
        ?.getAttribute("data-empty"),
    ).toBe("true");
    expect(byId("card-cache-fix").querySelector(".task-now")?.textContent).toBe(
      "Post PR: Merge dependencies",
    );
    expect(byId("card-cache-fix").textContent).toContain("PR #1490");
  });

  it("builds a track from the default lifecycle when the session wrote none", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: TRACKED }]);

    await waitFor(() => expect(byId("card-client")).toBeTruthy());
    const dots = [...byId("card-client").querySelectorAll(".track-dot")].map((dot) =>
      dot.getAttribute("data-s"),
    );
    // Commit, open PR, request review, address review, approval done; CI now; the rest to do.
    expect(dots.slice(0, 7)).toEqual(["done", "done", "done", "done", "done", "current", "todo"]);
    expect(byId("card-client").querySelector(".task-now")?.textContent).toBe("Post PR: Pass CI");
    const order = [...document.querySelectorAll("#view .task")].map((card) => card.id);
    expect(order).toEqual(["card-client", "card-cache-fix"]);
    // Shipped work leaves the rows for the kit's own Shipped recently list.
    expect(byId("shipped").hidden).toBe(false);
    expect(byId("shipped-list").textContent).toContain(
      TRACKED.tasks.find((task) => task.id === "docs")!.title,
    );
  });

  it("shows what waits on what, and jumps to the task it waits on", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: TRACKED }]);

    await waitFor(() => expect(byId("card-cache-fix")).toBeTruthy());
    const waits = byId("card-cache-fix").querySelector('.chip[data-kind="waits"]') as HTMLElement;
    expect(waits.textContent).toBe("Waits on Client 4.2 release");
    expect(byId("card-client").querySelector('.chip[data-kind="blocks"]')?.textContent).toBe(
      "Blocks Cache warm-up fix",
    );
    const scrolled: Element[] = [];
    const scrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this);
    };
    try {
      waits.click();
    } finally {
      Element.prototype.scrollIntoView = scrollIntoView;
    }
    expect(scrolled).toEqual([byId("card-client")]);
    expect(byId("card-client").getAttribute("data-focused")).toBe("true");
  });

  it("says a task waits once when waiting_on names the task it depends on", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    const value = {
      ...TRACKED,
      tasks: TRACKED.tasks.map((task) =>
        task.id === "cache-fix" ? { ...task, waiting_on: "the Client 4.2 release to merge" } : task,
      ),
    };
    emit("radar", [{ id: "current", version: "v1", value }]);

    await waitFor(() => expect(byId("card-cache-fix")).toBeTruthy());
    const waits = Array.from(
      byId("card-cache-fix").querySelectorAll('.chip[data-kind="waits"]'),
      (chip) => chip.textContent,
    );
    expect(waits).toEqual(["Waits on Client 4.2 release"]);
  });

  it("lists every session the scan kept, under its task or among the others", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: TRACKED }]);

    await waitFor(() => expect(byId("others").hidden).toBe(false));
    expect(byId("card-cache-fix").textContent).toContain("Cache warm-up");
    expect(byId("card-client").textContent).toContain("Client CI");
    // Not a change, and filed under a task the radar no longer has: both still show.
    expect(byId("others-count").textContent).toBe("2 other sessions");
    expect(byId("others-list").textContent).toContain("Read about indexes");
    expect(byId("others-list").textContent).toContain("Research");
    expect(byId("others-list").textContent).toContain("Orphan session");
  });

  it("reads live status past the first page, and says a session isn't checked until then", async () => {
    let secondPage: (page: unknown) => void = () => undefined;
    const asked: { after?: string }[] = [];
    const { emit, ready } = startRadar({
      listSessions: (params) => {
        asked.push(params);
        if (!params.after) {
          return Promise.resolve({
            items: [{ id: "unrelated", status: "idle" }],
            nextCursor: "c2",
          });
        }
        return new Promise((resolve) => {
          secondPage = resolve;
        });
      },
    });
    await waitFor(() => expect(ready()).toBe(true));
    const quiet = { id: "s2", title: "Ramp notes", task: "rollout", status: "idle" };
    emit("radar", [
      { id: "current", version: "v1", value: { ...RADAR, sessions: [...RADAR.sessions, quiet] } },
    ]);
    await waitFor(() => expect(button("Show live status")).toBeDefined());

    const link = () => document.querySelector('.session-link[data-omnigent-label="Ramp notes"]');
    const card = () => document.querySelector('.session[data-omnigent-label="Ramp checkout flag"]');
    // Saved as idle, before live status is on.
    await waitFor(() =>
      expect(link()?.querySelector(".live-dot")?.getAttribute("data-live")).toBe("idle"),
    );

    button("Show live status")?.click();
    // The first page doesn't have them: not checked, rather than the saved idle shown as live.
    await waitFor(() => expect(asked).toEqual([{ limit: 100 }, { limit: 100, after: "c2" }]));
    await waitFor(() => expect(link()?.getAttribute("title")).toBe("Not checked yet"));
    expect(link()?.querySelector(".live-dot")?.getAttribute("data-live")).toBe("unchecked");
    expect(card()?.textContent).toContain("Not checked yet");

    secondPage({
      items: [
        { id: "s1", status: "idle", needsInput: true },
        { id: "s2", status: "idle", needsInput: true },
      ],
      nextCursor: null,
    });
    await waitFor(() => expect(link()?.getAttribute("title")).toBe("Needs your input"));
    expect(link()?.querySelector(".live-dot")?.getAttribute("data-live")).toBe("needs_input");
    expect(card()?.textContent).toContain("Needs your input");
    expect(card()?.querySelector(".live-dot")?.getAttribute("data-live")).toBe("needs_input");
  });

  it("names where a next step sits on its task's track", async () => {
    const { emit, requests, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: TRACKED }]);

    await waitFor(() => expect(byId("next-rerun-ci")).toBeTruthy());
    const chip = byId("next-rerun-ci").querySelector(".next-chip") as HTMLButtonElement;
    expect(chip.textContent).toContain("Client 4.2 release · Post PR: Pass CI");
    chip.click();
    expect(requests.at(-1)).toEqual({
      action: "do_next_step",
      params: { next_step: "rerun-ci", label: "Rerun the UI shard", task: "client" },
    });
  });
});

describe("where was I designs", () => {
  it("draws a design with the kit's pieces and reports that it drew", async () => {
    const { emit, emitActions, reports, requests, ready } = startRadar({ html: LANES_VIEW });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("layout", [{ id: "current", version: "v1", value: LAYOUT }]);

    await waitFor(() => expect(byId("lane-rollout")).toBeTruthy());
    expect(document.querySelector("#view .task")).toBeNull();
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

  it("reports a failed design again after a report that didn't save", async () => {
    const html = withView(
      "",
      "Radar.view({ name: 'Broken', render: function () { throw new Error('lanes exploded'); } });",
    );
    const { emit, reports, failing, ready } = startRadar({ html });
    await waitFor(() => expect(ready()).toBe(true));
    failing.render_reports = 1;
    emit("radar", [{ id: "current", version: "v1", value: RADAR }]);
    emit("layout", [{ id: "current", version: "v1", value: LAYOUT }]);
    await waitFor(() => expect(byId("look").dataset.kind).toBe("failed"));
    // The first report didn't save; a later draw reports it again, so the agent still learns
    // the design failed.
    await waitFor(() => expect(failing.render_reports).toBe(0));
    emit("radar", [{ id: "current", version: "v2", value: RADAR }]);
    await waitFor(() =>
      expect(reports()).toEqual([
        expect.objectContaining({ value: expect.objectContaining({ state: "failed" }) }),
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

describe("where was I kit pieces in designs", () => {
  // A board that puts the kit's pieces straight into each card, as the live session's design did.
  const CARDS_VIEW = withView(
    "",
    `Radar.view({ name: 'Cards', render: function (root, model, kit) {
      kit.keyed(root, model.open, function (task) { return task.id; }, function (task) {
        var card = kit.el('article', { class: 'card', id: 'design-' + task.id });
        card.replaceChildren(kit.el('h3', { text: task.title }), kit.track(task), kit.taskChips(task), kit.taskSessions(task));
        return card;
      });
    } });`,
  );

  it("hands designs elements, so pieces go straight into a card even when a task has none", async () => {
    const { emit, reports, ready } = startRadar({ html: CARDS_VIEW });
    await waitFor(() => expect(ready()).toBe(true));
    // A task with no links, dependencies or sessions, beside ones that have them.
    const lone = {
      id: "lone",
      title: "Lone change",
      state: "active",
      stage: "pre_pr",
      step: "commit",
    };
    const value = { ...TRACKED, tasks: [...TRACKED.tasks, lone] };
    emit("radar", [{ id: "current", version: "v1", value }]);
    emit("layout", [{ id: "current", version: "v1", value: LAYOUT }]);

    await waitFor(() => expect(byId("design-lone")).toBeTruthy());
    // Nothing reads as stray data, and the pieces that exist are drawn.
    const text = byId("view").textContent ?? "";
    expect(text).not.toContain("[object");
    expect(text).not.toMatch(/\bnull\b/);
    expect(byId("design-cache-fix").querySelector(".session-link")?.textContent).toContain(
      "Cache warm-up",
    );
    await waitFor(() =>
      expect(reports()).toEqual([
        expect.objectContaining({ value: expect.objectContaining({ state: "ok" }) }),
      ]),
    );
  });

  it("fails a design that draws data where an element belongs, so its session notices", async () => {
    const html = withView(
      "",
      `Radar.view({ name: 'Leaky', render: function (root, model, kit) {
        var card = kit.el('article', { class: 'card' });
        card.replaceChildren.apply(card, [kit.el('h3', { text: 'Board' })].concat(kit.sessionsOf(model.open[0])));
        root.replaceChildren(card, null);
      } });`,
    );
    const { emit, reports, ready } = startRadar({ html });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: TRACKED }]);
    emit("layout", [{ id: "current", version: "v1", value: LAYOUT }]);

    // The plain view takes over, and render_reports tells the session what went wrong.
    await waitFor(() => expect(byId("card-client")).toBeTruthy());
    await waitFor(() =>
      expect(reports()).toEqual([
        expect.objectContaining({
          value: expect.objectContaining({
            state: "failed",
            error: expect.stringContaining("[object Object]"),
          }),
        }),
      ]),
    );
  });

  it("keeps shipped work on the page when a design draws only what is in flight", async () => {
    const { emit, ready } = startRadar({ html: CARDS_VIEW });
    await waitFor(() => expect(ready()).toBe(true));
    emit("radar", [{ id: "current", version: "v1", value: TRACKED }]);
    emit("layout", [{ id: "current", version: "v1", value: LAYOUT }]);

    await waitFor(() => expect(byId("design-client")).toBeTruthy());
    expect(byId("design-docs")).toBeNull();
    expect(byId("shipped").hidden).toBe(false);
    expect(byId("shipped-list").textContent).toContain("Docs fix");
  });

  it("names what a task waits on as part of the sentence, briefly", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    const long =
      "An acme-web source checkout for the PR branch before anyone can address the comments";
    const value = {
      ...TRACKED,
      tasks: [
        { ...TRACKED.tasks[1], waiting_on: long },
        {
          ...TRACKED.tasks[0],
          id: "sdk",
          depends_on: [],
          waiting_on: "SDK review from the API team",
        },
      ],
    };
    emit("radar", [{ id: "current", version: "v1", value }]);

    await waitFor(() => expect(byId("card-client")).toBeTruthy());
    const chip = byId("card-client").querySelector(".chip[data-kind='waits']") as HTMLElement;
    expect(chip.textContent?.startsWith("Waits on an acme-web source checkout")).toBe(true);
    expect(chip.textContent!.length).toBeLessThan(60);
    expect(chip.title).toBe(long);
    // A name stays as written.
    expect(byId("card-sdk").querySelector(".chip[data-kind='waits']")?.textContent).toBe(
      "Waits on SDK review from the API team",
    );
  });
});

describe("where was I failed next steps", () => {
  const failedRun = (updated: string) => ({
    id: "rerun-ci",
    version: "v1",
    value: {
      label: "Rerun the UI shard",
      state: "failed",
      outcome: "No checkout of the branch.",
      updated,
    },
  });

  it("holds a step that failed when a later scan offers it again without saying what changed", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    // The run failed, then a scan (scanned_at after the failure) offered the same step again.
    emit("step_runs", [failedRun("2026-11-19T09:00:00Z")]);
    emit("radar", [{ id: "current", version: "v1", value: TRACKED }]);

    await waitFor(() => expect(byId("card-client")).toBeTruthy());
    expect(byId("next-rerun-ci")).toBeNull();
    expect(byId("card-client").querySelector(".held-note")?.textContent).toContain(
      "didn’t work last time: No checkout of the branch",
    );
  });

  it("offers it again once the scan says what changed", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("step_runs", [failedRun("2026-11-19T09:00:00Z")]);
    const value = {
      ...TRACKED,
      tasks: TRACKED.tasks.map((task) =>
        task.id === "client"
          ? {
              ...task,
              next_steps: [
                { ...task.next_steps![0], retry_because: "The branch is checked out now." },
              ],
            }
          : task,
      ),
    };
    emit("radar", [{ id: "current", version: "v1", value }]);

    await waitFor(() => expect(byId("next-rerun-ci")).toBeTruthy());
    expect(byId("card-client").querySelector(".held-note")).toBeNull();
  });

  it("still shows a failure that happened after the last scan as a retry", async () => {
    const { emit, ready } = startRadar();
    await waitFor(() => expect(ready()).toBe(true));
    emit("step_runs", [failedRun("2026-11-19T11:00:00Z")]);
    emit("radar", [{ id: "current", version: "v1", value: TRACKED }]);

    await waitFor(() =>
      expect(byId("next-rerun-ci")?.querySelector(".next-chip")?.textContent).toContain(
        "Try again",
      ),
    );
  });
});

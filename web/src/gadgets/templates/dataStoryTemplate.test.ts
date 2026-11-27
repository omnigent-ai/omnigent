import { waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  DATA_STORY_GUIDE,
  DATA_STORY_HTML,
  DATA_STORY_REVISION,
  DATA_STORY_VIEW_END,
  DATA_STORY_VIEW_START,
} from "./dataStoryTemplate";
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

const IDEAS = {
  scanned_at: "2026-11-19T10:00:00Z",
  window_days: 7,
  sessions_read: 9,
  ideas: [
    {
      id: "signups",
      title: "Did the launch move signups?",
      question: "How did weekly signups change after the launch?",
      why: "Two sessions queried signups by week.",
      effort: "quick",
      sources: [{ name: "signups", kind: "table" }],
      sessions: [{ id: "s1", title: "Launch metrics", quote: "signups are **up**" }],
    },
    { id: "latency", title: "Slow dashboards", question: "Which dashboards are slowest?" },
  ],
};

const STORY = {
  idea_id: "signups",
  title: "Did the launch move signups?",
  question: "How did weekly signups change after the launch?",
  built_at: "2026-11-19T10:05:00Z",
  headline: { label: "Weekly signups", value: "702", delta: "+62%", direction: "up", good: "up" },
  summary: "Signups rose after the launch and stayed up.",
  sections: [
    { id: "trend", heading: "Signups climbed", body: "They jumped.", chart: "weekly" },
    { id: "where", heading: "Referrals led", takeaway: "Referrals doubled." },
  ],
  sources: [{ name: "signups", detail: "one row per signup" }],
  caveats: "October is incomplete.",
};

const WEEKLY = {
  title: "Weekly signups",
  kind: "line",
  series: [
    {
      name: "Signups",
      points: [
        { x: "Sep 1", y: 410 },
        { x: "Sep 8", y: 432 },
        { x: "Sep 15", y: 520 },
      ],
    },
  ],
  annotations: [{ x: "Sep 15", label: "Launch" }],
};

const LAYOUT = {
  structure: "Before and after",
  why: "The story is about a launch.",
  revision_id: REVISION,
  previous_revision_id: PREVIOUS,
  changed_at: "2026-11-19T10:06:00Z",
  change_note: "Marked the launch on every chart",
};

/** The template with its view region replaced, as the gadget session publishes a design. */
function withView(style: string, script: string): string {
  const start = DATA_STORY_HTML.indexOf(DATA_STORY_VIEW_START);
  const end = DATA_STORY_HTML.indexOf(DATA_STORY_VIEW_END) + DATA_STORY_VIEW_END.length;
  const region = `${DATA_STORY_VIEW_START}\n<style id="design-style">${style}</style>\n<script>${script}</script>\n${DATA_STORY_VIEW_END}`;
  return DATA_STORY_HTML.slice(0, start) + region + DATA_STORY_HTML.slice(end);
}

const BEFORE_AFTER = withView(
  ".split { display: grid; }",
  `Story.view({ name: 'Before and after', render: function (root, model, kit) {
    var box = root.firstChild || root.appendChild(kit.el('div', { class: 'split' }));
    box.replaceChildren(kit.headline(model.story), kit.chart('weekly', { bare: true }));
  } });`,
);

interface Records {
  ideas?: unknown;
  story?: unknown;
  series?: Record<string, unknown>;
  layout?: unknown;
}

// The page's scripts run in order in one context that is its own window, against jsdom, with the
// SDK faked at window.omnigent, the external boundary. As in a browser, a script that throws
// reports the error to the window's error listeners and the next script still runs.
function startStory({
  html = DATA_STORY_HTML,
  preview = false,
  canAct = true,
  canWrite = true,
  allowErrors = false,
  failRequests = false,
}: {
  html?: string;
  preview?: boolean;
  canAct?: boolean;
  canWrite?: boolean;
  /** A design that is meant to throw while the page loads. */
  allowErrors?: boolean;
  /** Every action request fails, as when the gadget session can't be reached. */
  failRequests?: boolean;
} = {}) {
  const { listeners, writes, pages, failing, collection } = recordingCollections();
  let actionListener: Listener | null = null;
  const requests: { action: string; params: unknown }[] = [];
  const opened: string[] = [];
  const client = {
    context: {
      app: { id: "app1", name: "Data story" },
      revisionId: REVISION,
      viewer: { id: "1001", displayName: "viewer@example.com" },
      permissions: { writeData: canWrite, publish: canAct },
      preview,
    },
    hasCapability: () => true,
    can: (requirement: string) => (requirement === "write" ? canWrite : canAct),
    ui: { colorScheme: "light" },
    data: { collection },
    navigation: {
      openSession: (sessionId: string) => {
        opened.push(sessionId);
        return Promise.resolve({ opened: true });
      },
      newAutomation: () => Promise.resolve({ opened: true }),
    },
    agent: {
      request: (action: string, params: unknown) => {
        requests.push({ action, params });
        if (failRequests) return Promise.reject(new Error("The gadget session is unreachable"));
        return Promise.resolve({ actionId: "a1", delivery: "sent" });
      },
      watch: (listener: Listener) => {
        actionListener = listener;
        return { dispose: () => undefined, refresh: () => undefined };
      },
    },
  };
  bootTemplatePage(html, client, { allowErrors });
  const emit = (name: string, items: Doc[], nextCursor: string | null = null) =>
    listeners[name]?.({ status: "live", items, nextCursor } as never);
  const emitActions = (items: unknown[]) => actionListener?.({ status: "live", items });
  /** Loads the page with these records, as the first polls answer. */
  const load = ({ ideas = null, story = null, series = {}, layout = null }: Records = {}) => {
    emit("ideas", ideas ? [{ id: "current", version: "v1", value: ideas }] : []);
    emit("story", story ? [{ id: "current", version: "v1", value: story }] : []);
    emit(
      "series",
      Object.entries(series).map(([key, value]) => ({ id: key, version: "v1", value })),
    );
    emit("layout", layout ? [{ id: "current", version: "v1", value: layout }] : []);
  };
  return {
    emit,
    emitActions,
    load,
    pages,
    failing,
    writes,
    requests,
    opened,
    reports: () => writes.filter((write) => write.collection === "render_reports"),
    ready: () => listeners.story !== undefined,
  };
}

const byId = (elementId: string) => document.getElementById(elementId) as HTMLElement;
const button = (name: string) =>
  [...document.querySelectorAll("button")].find((node) => node.textContent?.includes(name)) as
    HTMLButtonElement | undefined;

afterEach(() => {
  document.body.replaceChildren();
});

describe("data story template", () => {
  it("starts empty with a button that finds stories, and seeds nothing", async () => {
    const { load, writes, requests, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    load();

    await waitFor(() => expect(byId("empty").hidden).toBe(false));
    expect(byId("content").hidden).toBe(true);
    expect(byId("ideas").hidden).toBe(true);
    expect(writes).toHaveLength(0);

    byId("find-empty").click();
    expect(requests).toEqual([{ action: "find_stories", params: { window_days: 7 } }]);
  });

  it("shows the search's progress in the empty state", async () => {
    const { load, emitActions, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    load();
    emitActions([{ id: "a1", action: "find_stories", params: {}, status: "running" }]);

    await waitFor(() => expect(byId("progress").hidden).toBe(false));
    expect(byId("empty-run").textContent).toContain("Reading your recent sessions for data work");
    expect(byId("empty-run").textContent).toContain("usually 1 to 3 minutes");
    expect(byId("find-empty").textContent).toBe("Looking…");
  });

  it("offers the ideas as cards, and builds the one the viewer picks or describes", async () => {
    const { load, requests, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    load({ ideas: IDEAS });

    await waitFor(() => expect(byId("ideas").hidden).toBe(false));
    expect(byId("empty").hidden).toBe(true);
    expect((byId("ideas") as HTMLDetailsElement).open).toBe(true);
    const cards = document.querySelectorAll("#idea-list .idea");
    expect(cards).toHaveLength(2);
    // Quotes come from session messages, so their Markdown marks are dropped.
    expect(cards[0].textContent).toContain("Launch metrics: “signups are up”");
    expect(cards[0].textContent).toContain("Quick");

    (cards[0].querySelector("button.build") as HTMLButtonElement).click();
    const field = byId("custom-text") as HTMLInputElement;
    field.value = "Weekly active users by plan";
    byId("custom-go").click();
    // Typed before the first one was sent: that text stays when the first clears.
    field.value = "Errors by service";
    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await waitFor(() => expect(field.value).toBe(""));
    expect(requests).toEqual([
      {
        action: "build_story",
        params: { idea_id: "signups", title: "Did the launch move signups?" },
      },
      { action: "build_story", params: { request: "Weekly active users by plan" } },
      { action: "build_story", params: { request: "Errors by service" } },
    ]);
  });

  it("keeps the described story's text when its request fails, by click or Enter", async () => {
    const { load, requests, ready } = startStory({ failRequests: true });
    await waitFor(() => expect(ready()).toBe(true));
    load({ ideas: IDEAS });
    await waitFor(() => expect(byId("ideas").hidden).toBe(false));
    const field = byId("custom-text") as HTMLInputElement;

    field.value = "Weekly active users by plan";
    byId("custom-go").click();
    await waitFor(() => expect(document.body.textContent).toContain("unreachable"));
    expect(field.value).toBe("Weekly active users by plan");

    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await waitFor(() => expect(requests).toHaveLength(2));
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(field.value).toBe("Weekly active users by plan");
  });

  it("cites sessions with links that wrap with the text and open by click or Enter", async () => {
    const { load, opened, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    load({ ideas: IDEAS });

    await waitFor(() => expect(document.querySelector("#idea-list .cite")).not.toBeNull());
    const link = document.querySelector("#idea-list .cite") as HTMLElement;
    // A button would stay one box on its own line when it wraps; a span flows with the text.
    expect(link.tagName).toBe("SPAN");
    expect(link.getAttribute("role")).toBe("link");
    expect(link.tabIndex).toBe(0);
    link.click();
    link.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(opened).toEqual(["s1", "s1"]);
  });

  it("shows the ideas and Suggest others until a story is built, then only the story", async () => {
    const { load, emit, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    load({ ideas: IDEAS });

    await waitFor(() => expect(byId("ideas").hidden).toBe(false));
    expect((byId("ideas") as HTMLDetailsElement).open).toBe(true);
    expect(byId("find").hidden).toBe(false);
    expect(byId("find").textContent).toBe("Suggest others");
    expect(byId("window-label").hidden).toBe(false);

    emit("series", [{ id: "weekly", version: "v1", value: WEEKLY }]);
    emit("story", [{ id: "current", version: "v1", value: STORY }]);
    await waitFor(() => expect(byId("content").hidden).toBe(false));
    // Another story is a new gadget from the template, so a built one offers no other ideas.
    expect(byId("ideas").hidden).toBe(true);
    expect(byId("find").hidden).toBe(true);
    expect(byId("window-label").hidden).toBe(true);
    expect(document.body.textContent).not.toContain("Other stories");
    expect(byId("refresh").hidden).toBe(false);
  });

  it("shows a build's stages, then that it is designing once the story is written", async () => {
    const { load, emit, emitActions, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    load({ ideas: IDEAS });
    emitActions([
      {
        id: "a1",
        action: "build_story",
        params: { idea_id: "signups", title: "Did the launch move signups?" },
        status: "running",
        requestedAt: "2026-11-19T10:00:30Z",
      },
    ]);

    await waitFor(() => expect(byId("building").hidden).toBe(false));
    expect(byId("building-what").textContent).toBe("“Did the launch move signups?”");
    const stages = () =>
      [...byId("build-stages").children].map((item) => item.getAttribute("data-now"));
    expect(stages()).toEqual(["true", "false", "false"]);
    // While one builds, the ideas can't start another.
    expect((document.querySelector("#idea-list button.build") as HTMLButtonElement).disabled).toBe(
      true,
    );

    emit("story", [{ id: "current", version: "v1", value: STORY }]);
    await waitFor(() => expect(stages()).toEqual(["false", "true", "false"]));
    expect(byId("build-run").textContent).toContain("Designing the page");
  });

  it("draws the plain story with its headline, annotated chart, sources and caveats", async () => {
    const { load, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    load({ ideas: IDEAS, story: STORY, series: { weekly: WEEKLY } });

    await waitFor(() => expect(byId("content").hidden).toBe(false));
    expect(byId("title").textContent).toBe(STORY.title);
    expect(byId("headline").textContent).toBe(STORY.question);
    const view = byId("view");
    expect(view.querySelector(".headline-card")?.textContent).toContain("702");
    expect(view.querySelector(".headline-card .delta")?.getAttribute("data-tone")).toBe("good");
    expect(view.querySelectorAll(".section")).toHaveLength(2);
    expect(view.querySelector("#section-trend svg.chart")).not.toBeNull();
    expect(view.querySelector("#section-trend .note-text")?.textContent).toBe("Launch");
    expect(byId("about").textContent).toContain("one row per signup");
    expect(byId("about").textContent).toContain("Caveats: October is incomplete.");
    // A built story is the whole page: its ideas are gone.
    expect(byId("ideas").hidden).toBe(true);
    expect(byId("refresh").hidden).toBe(false);
  });

  it("draws a chart named like a built-in as missing until its series arrives", async () => {
    const { load, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    const story = {
      ...STORY,
      sections: [STORY.sections[0], { ...STORY.sections[1], chart: "constructor" }],
    };
    load({ ideas: IDEAS, story, series: { weekly: WEEKLY } });
    await waitFor(() => expect(byId("content").hidden).toBe(false));
    const view = byId("view");
    expect(view.querySelector("#section-where")?.textContent).toContain(
      "No data for this chart yet.",
    );
    expect(view.querySelector("#section-trend svg.chart")).not.toBeNull();
    expect(byId("about").textContent).toContain("one row per signup");

    load({
      ideas: IDEAS,
      story,
      series: { weekly: WEEKLY, constructor: { ...WEEKLY, title: "Referrals" } },
    });
    await waitFor(() => expect(view.querySelector("#section-where svg.chart")).not.toBeNull());
  });

  it.each([
    ["negative", [-40, -10, -25]],
    ["equal negative", [-20, -20, -20]],
  ])("keeps %s bars inside the chart, growing from zero", async (_name, values) => {
    const { load, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    const losses = {
      ...WEEKLY,
      kind: "bar",
      series: [
        {
          name: "Change",
          points: values.map((y, index) => ({ x: `Week ${index + 1}`, y })),
        },
      ],
      annotations: [],
    };
    load({ ideas: IDEAS, story: STORY, series: { weekly: losses } });
    await waitFor(() =>
      expect(byId("view").querySelectorAll("#section-trend rect.bar")).toHaveLength(3),
    );
    const chart = byId("view").querySelector("#section-trend svg.chart") as SVGSVGElement;
    const height = Number(chart.getAttribute("viewBox")?.split(" ")[3]);
    for (const bar of chart.querySelectorAll("rect.bar")) {
      const top = Number(bar.getAttribute("y"));
      const bottom = top + Number(bar.getAttribute("height"));
      expect(top).toBeGreaterThanOrEqual(0);
      expect(bottom).toBeLessThanOrEqual(height);
    }
  });

  it("draws every chart when the series take more than one page", async () => {
    const { emit, load, pages, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    const story = {
      ...STORY,
      sections: [STORY.sections[0], { ...STORY.sections[1], chart: "referrals" }],
    };
    const referrals = { id: "referrals", version: "v1", value: { ...WEEKLY, title: "Referrals" } };
    const weekly = { id: "weekly", version: "v1", value: WEEKLY };
    load({ ideas: IDEAS, story });
    // Pages end at a byte budget, so a page can stop short and continue.
    pages.series = { p2: { items: [referrals], nextCursor: null } };
    emit("series", [weekly], "p2");
    const chart = (section: string) => byId("view").querySelector(`#section-${section} svg.chart`);
    await waitFor(() => expect(chart("where")).not.toBeNull());
    expect(chart("trend")).not.toBeNull();

    // A continuation that fails keeps the chart it found before; the next poll tries again.
    pages.series = { p3: new Error("The connection dropped") };
    emit("series", [{ ...weekly, version: "v2" }], "p3");
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(chart("where")).not.toBeNull();
    expect(byId("view").textContent).not.toContain("No data for this chart yet");
  });

  it("finds a chart past the first ten pages over the next polls", async () => {
    const { emit, load, pages, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    // Eleven accepted charts of 400 points each are near the 32 KiB record limit, so the
    // server's 48 KiB page budget gives one per page: eleven pages in all.
    const big = (n: number) => ({
      id: `c${n}`,
      version: "v1",
      value: {
        ...WEEKLY,
        title: `Chart ${n}`,
        annotations: [],
        series: [
          {
            name: "Signups",
            points: Array.from({ length: 400 }, (_, i) => ({ x: `Day ${i + 1}`, y: 400 + n + i })),
          },
        ],
      },
    });
    const story = {
      ...STORY,
      sections: [
        { ...STORY.sections[0], chart: "c0" },
        { ...STORY.sections[1], chart: "c10" },
      ],
    };
    load({ ideas: IDEAS, story });
    pages.series = {};
    for (let n = 1; n <= 10; n += 1) {
      pages.series[`p${n}`] = { items: [big(n)], nextCursor: n < 10 ? `p${n + 1}` : null };
    }
    const chart = (section: string) => byId("view").querySelector(`#section-${section} svg.chart`);

    // The first poll reads ten pages and stops before the last chart.
    emit("series", [big(0)], "p1");
    await waitFor(() => expect(byId("content").hidden).toBe(false));
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(chart("where")).toBeNull();
    // The next poll goes on from there and finds it.
    emit("series", [{ ...big(0), version: "v2" }], "p1");
    await waitFor(() => expect(chart("where")).not.toBeNull());
    expect(byId("view").textContent).not.toContain("No data for this chart yet");
  });

  it("refreshes the data and shows the refresh running", async () => {
    const { load, emitActions, requests, ready } = startStory();
    await waitFor(() => expect(ready()).toBe(true));
    load({ story: STORY, series: { weekly: WEEKLY } });
    await waitFor(() => expect(byId("refresh").hidden).toBe(false));

    byId("refresh").click();
    expect(requests).toEqual([{ action: "refresh_data", params: {} }]);
    emitActions([{ id: "a1", action: "refresh_data", params: {}, status: "running" }]);
    await waitFor(() => expect(byId("run").textContent).toContain("Refreshing the data"));
  });

  it("shows an example story in a template preview", async () => {
    const { load, writes, ready } = startStory({ preview: true, canAct: false });
    await waitFor(() => expect(ready()).toBe(true));
    load();

    expect(writes.map((write) => write.collection).sort()).toEqual([
      "ideas",
      "series",
      "series",
      "story",
    ]);
    await waitFor(() => expect(byId("content").hidden).toBe(false));
    expect(byId("meta").textContent).toContain("Example");
    expect(byId("look").textContent).toContain("An example");
    expect(byId("refresh").hidden).toBe(true);
    expect(byId("empty").hidden).toBe(true);
  });

  it("tells a viewer without edit access that building stories is the owner's", async () => {
    const { load, ready } = startStory({ canAct: false, canWrite: false });
    await waitFor(() => expect(ready()).toBe(true));
    load({ story: STORY });

    await waitFor(() => expect(byId("notice").textContent).toContain("owner’s gadget session"));
    expect(byId("refresh").hidden).toBe(true);
  });
});

describe("data story designs", () => {
  it("draws a design with the kit's pieces and reports that it drew", async () => {
    const { load, reports, ready } = startStory({ html: BEFORE_AFTER });
    await waitFor(() => expect(ready()).toBe(true));
    load({ story: STORY, series: { weekly: WEEKLY }, layout: LAYOUT });

    await waitFor(() => expect(byId("view").querySelector(".split")).not.toBeNull());
    expect(byId("view").querySelector(".split svg.chart")).not.toBeNull();
    expect(byId("look").textContent).toContain("Look: Before and after");
    expect(byId("look").textContent).toContain("Marked the launch on every chart");
    await waitFor(() =>
      expect(reports()).toEqual([
        expect.objectContaining({ id: REVISION, value: expect.objectContaining({ state: "ok" }) }),
      ]),
    );
  });

  it("asks for the previous look by its revision", async () => {
    const { load, requests, ready } = startStory({ html: BEFORE_AFTER });
    await waitFor(() => expect(ready()).toBe(true));
    load({ story: STORY, series: { weekly: WEEKLY }, layout: LAYOUT });

    await waitFor(() => expect(button("Back to the previous look")).toBeDefined());
    button("Back to the previous look")!.click();
    expect(requests).toEqual([{ action: "restore_look", params: { revision_id: PREVIOUS } }]);
  });

  it("falls back to the plain story when a design throws while drawing", async () => {
    const broken = withView(
      "",
      "Story.view({ name: 'Broken', render: function () { return undefined.length; } });",
    );
    const { load, reports, ready } = startStory({ html: broken });
    await waitFor(() => expect(ready()).toBe(true));
    load({ story: STORY, series: { weekly: WEEKLY }, layout: LAYOUT });

    await waitFor(() => expect(byId("view").querySelector(".headline-card")).not.toBeNull());
    expect(byId("look").getAttribute("data-kind")).toBe("failed");
    expect(byId("look").textContent).toContain("This look could not be drawn");
    expect(button("Back to the previous look")).toBeDefined();
    await waitFor(() =>
      expect(reports()).toEqual([
        expect.objectContaining({ value: expect.objectContaining({ state: "failed" }) }),
      ]),
    );
  });

  it("reports a failed design again after a report that didn't save", async () => {
    const broken = withView(
      "",
      "Story.view({ name: 'Broken', render: function () { return undefined.length; } });",
    );
    const { load, emit, reports, failing, ready } = startStory({ html: broken });
    await waitFor(() => expect(ready()).toBe(true));
    failing.render_reports = 1;
    load({ story: STORY, series: { weekly: WEEKLY }, layout: LAYOUT });
    await waitFor(() => expect(byId("look").getAttribute("data-kind")).toBe("failed"));
    // The first report didn't save; a later draw reports it again, so the agent still learns
    // the design failed.
    await waitFor(() => expect(failing.render_reports).toBe(0));
    emit("story", [{ id: "current", version: "v2", value: STORY }]);
    await waitFor(() =>
      expect(reports()).toEqual([
        expect.objectContaining({ value: expect.objectContaining({ state: "failed" }) }),
      ]),
    );
  });

  it("falls back to the plain story when a design's script fails to load", async () => {
    const broken = withView("", "Story.view({ name: 'Broken', render: function () {} ");
    const { load, ready } = startStory({ html: broken, allowErrors: true });
    await waitFor(() => expect(ready()).toBe(true));
    load({ story: STORY, series: { weekly: WEEKLY } });

    await waitFor(() => expect(byId("view").querySelector(".headline-card")).not.toBeNull());
    expect(byId("look").getAttribute("data-kind")).toBe("failed");
  });
});

describe("data story revision", () => {
  it("submits nothing through a form, which the sandboxed frame would drop", () => {
    expect(DATA_STORY_HTML).not.toMatch(/<form[\s>]/);
  });

  it("uses only schema keywords the server accepts", () => {
    for (const schema of Object.values(DATA_STORY_REVISION.collections)) {
      for (const keyword of schemaKeywords(schema)) {
        expect(SERVER_SCHEMA_KEYWORDS).toContain(keyword);
      }
    }
  });

  it("has one view region a design replaces, after the kit and before the start", () => {
    const html = DATA_STORY_HTML;
    expect(html.split(DATA_STORY_VIEW_START)).toHaveLength(2);
    expect(html.split(DATA_STORY_VIEW_END)).toHaveLength(2);
    expect(html.indexOf('<script id="story-kit">')).toBeLessThan(
      html.indexOf(DATA_STORY_VIEW_START),
    );
    expect(html.indexOf(DATA_STORY_VIEW_END)).toBeLessThan(html.indexOf("Story.start()"));
  });

  it("tells the session to build from real data and publish a design as one edit", () => {
    expect(DATA_STORY_GUIDE).toContain("never invent or extrapolate data");
    expect(DATA_STORY_GUIDE).toContain("`base_revision_id` the published revision and one edit");
    expect(DATA_STORY_GUIDE).toContain(`start: "${DATA_STORY_VIEW_START}"`);
    expect(DATA_STORY_GUIDE).toContain("Never read whole transcripts");
  });

  it("fits the server's limits", () => {
    expect(new TextEncoder().encode(DATA_STORY_GUIDE).length).toBeLessThanOrEqual(16 * 1024);
    expect(new TextEncoder().encode(DATA_STORY_HTML).length).toBeLessThan(1024 * 1024);
    // The server bounds the whole collections object, and how many there are.
    expect(Object.keys(DATA_STORY_REVISION.collections).length).toBeLessThanOrEqual(16);
    expect(jsonBytes(DATA_STORY_REVISION.collections)).toBeLessThanOrEqual(16 * 1024);
  });

  it("declares the actions the page requests", () => {
    expect(DATA_STORY_REVISION.actions?.map((action) => action.name)).toEqual([
      "find_stories",
      "build_story",
      "refresh_data",
      "restore_look",
    ]);
  });
});

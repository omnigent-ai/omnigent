// The real-page audit's page steps against a two-view fixture page: each page a step leads to
// is audited whole, what renders along the way is noted, and requests held on purpose settle
// only as the declared operations.

import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ReactNode, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { settingsEntry } from "@/pages/settings/search/catalog/types";
import { SettingsItem } from "@/pages/settings/search/SettingsItem";
import { settingsControlProps } from "@/pages/settings/search/settingsControl";
import { settingsOverlayProps } from "@/pages/settings/search/settingsOwnership";

import { type SettingsRoute, settingsRouteTable } from "./settingsPageRoutes";
import {
  type HeldRequest,
  type PageAction,
  type PageStep,
  runPageAction,
  wholePageProblems,
} from "./settingsPageSteps";

const FIRST = settingsEntry({
  id: "fixture.first",
  section: "general",
  label: "First",
  description: "The first view.",
  parts: { next: "Next view" },
});
const SECOND = settingsEntry({
  id: "fixture.second",
  section: "general",
  label: "Second",
  description: "The second view.",
  parts: {
    back: "Back to first",
    save: "Save second",
    cancel: "Cancel save",
    quiet: "Save quietly",
    both: "Fetch both",
    load: "Load more",
  },
  // Cancel shows only while a save is in flight.
  presence: { cancel: { states: ["saving"] } },
});
const CATALOG = [FIRST, SECOND];
const ENTRIES = new Map(CATALOG.map((entry) => [entry.id, entry]));
const ON_FIRST = { [FIRST.id]: "It's on the first view." };
const ON_SECOND = { [SECOND.id]: "It's on the second view." };
const SAVE_KEY = ["fixture", "save"];
const BOTH_KEY = ["fixture", "both"];

/**
 * Two views. The second's Save posts (keyed) and shows "Saving…" then "Saved"; Save quietly
 * posts without a key; Fetch both runs one query that makes two requests; Load more runs a
 * query that makes none.
 */
function StepsPage({ extra, hideStatus }: { extra?: ReactNode; hideStatus?: boolean }) {
  const client = useQueryClient();
  const [view, setView] = useState<"first" | "second">("first");
  const save = useMutation({
    mutationKey: SAVE_KEY,
    mutationFn: () => fetch("/v1/save", { method: "POST" }),
  });
  const quiet = useMutation({ mutationFn: () => fetch("/v1/quiet", { method: "POST" }) });
  const [loading, setLoading] = useState(false);
  // Some pages stop showing a save's status while it's still in flight.
  const [statusHidden, setStatusHidden] = useState(false);
  useQuery({
    queryKey: ["fixture", "more"],
    queryFn: () => new Promise<never>(() => {}),
    enabled: loading,
  });
  const fetchBoth = () =>
    void client.fetchQuery({
      queryKey: BOTH_KEY,
      queryFn: () => Promise.all([fetch("/v1/a"), fetch("/v1/b")]),
    });
  const control = (part: keyof typeof SECOND.parts, onClick: () => void) => (
    <button type="button" {...settingsControlProps(SECOND, part)} onClick={onClick}>
      {SECOND.parts[part]}
    </button>
  );
  if (view === "first") {
    return (
      <SettingsItem entry={FIRST}>
        <span>First</span>
        <button
          type="button"
          {...settingsControlProps(FIRST, "next")}
          onClick={() => setView("second")}
        >
          Next view
        </button>
      </SettingsItem>
    );
  }
  return (
    <>
      <SettingsItem entry={SECOND}>
        <span>Second</span>
        {control("back", () => setView("first"))}
        {control("save", () => {
          save.mutate();
          if (hideStatus) setTimeout(() => setStatusHidden(true), 50);
        })}
        {statusHidden && <p>Status hidden</p>}
        {save.isPending && !statusHidden && (
          <>
            <p>Saving…</p>
            {control("cancel", () => {})}
          </>
        )}
        {save.isPending && statusHidden && control("cancel", () => {})}
        {save.isSuccess && <p>Saved</p>}
        {control("quiet", () => quiet.mutate())}
        {control("both", fetchBoth)}
        {control("load", () => setLoading(true))}
      </SettingsItem>
      {extra}
    </>
  );
}

const releases: (() => void)[] = [];
const releaseAll = () => {
  for (const release of releases.splice(0)) release();
};

function renderSteps(extra?: ReactNode, hideStatus = false) {
  const held = (method: "GET" | "POST", url: string) => ({
    method,
    url,
    body: {},
    hold: new Promise<void>((done) => {
      releases.push(done);
    }),
  });
  const routes = settingsRouteTable([
    held("POST", "/v1/save"),
    held("POST", "/v1/quiet"),
    held("GET", "/v1/a"),
    held("GET", "/v1/b"),
  ]);
  vi.stubGlobal("fetch", routes.fetch);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <StepsPage extra={extra} hideStatus={hideStatus} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const observedRows = new Set<string>();
  const observedParts = new Set<string>();
  const snapshot = {
    catalog: CATALOG,
    entries: ENTRIES,
  };
  const run = (steps: PageStep[], hides: Record<string, string> = {}) => {
    const action: PageAction = { name: "views", steps };
    return runPageAction({
      action,
      where: "fixtures › web",
      client,
      routes,
      snapshot: { ...snapshot, hides },
      observedRows,
      observedParts,
      timeout: 500,
    });
  };
  // The scenario's own audit of the page before any step, as the real harness makes it.
  const auditInitial = (hides: Record<string, string>) =>
    wholePageProblems({ ...snapshot, where: "fixtures › web", hides });
  return { run, auditInitial, observedRows, observedParts };
}

const click = (name: string) => () => userEvent.click(screen.getByRole("button", { name }));
const NEXT: PageStep = {
  name: "next",
  run: click("Next view"),
  witness: { row: SECOND.id },
  hides: ON_FIRST,
};
const BACK: PageStep = {
  name: "back",
  run: click("Back to first"),
  witness: { row: FIRST.id },
  hides: ON_SECOND,
};
const SAVING: HeldRequest = {
  request: "POST /v1/save",
  witness: "Saving…",
  operation: { mutationKey: SAVE_KEY },
};
const SAVE: PageStep = {
  name: "save",
  run: click("Save second"),
  witness: "Saving…",
  states: { [SECOND.id]: ["saving"] },
  held: [SAVING],
};

afterEach(() => {
  releaseAll();
  cleanup();
  vi.unstubAllGlobals();
});

describe("a page action", () => {
  it("audits each page a step leads to, and notes its rows and parts", async () => {
    const { run, observedRows, observedParts } = renderSteps();
    expect(await run([NEXT])).toEqual([]);
    expect([...observedRows]).toEqual([SECOND.id]);
    expect([...observedParts]).toContain("fixture.second:save");
  });

  it("reports what's wrong with the page a step leads to, as a page of its own", async () => {
    const { run } = renderSteps(<button type="button">Stray</button>);
    expect(await run([{ ...NEXT, hides: {} }])).toEqual([
      'fixtures › web › views › next: button "Stray": has no owner in the Settings catalog',
      "fixtures › web › views › next: fixture.first: renders 0 rows, expected 1",
    ]);
  });

  it("reports a row it hides that renders anyway", async () => {
    const { run } = renderSteps();
    expect(await run([{ ...NEXT, hides: { ...ON_FIRST, ...ON_SECOND } }])).toEqual([
      "fixtures › web › views › next: fixture.second renders, though it's hidden here (It's on the second view.)",
    ]);
  });

  it("replaces the page's hides at each step that gives them, and carries them otherwise", async () => {
    const { run, auditInitial } = renderSteps();
    // The first page hides the second's row; the next page hides the first's; Back, the second's.
    expect(auditInitial(ON_SECOND)).toEqual([]);
    expect(await run([NEXT, BACK], ON_SECOND)).toEqual([]);
    cleanup();
    // Back without hides of its own still hides the first row, which is back.
    const second = renderSteps();
    expect(await second.run([NEXT, { ...BACK, hides: undefined }], ON_SECOND)).toEqual([
      "fixtures › web › views › back: fixture.first renders, though it's hidden here (It's on the first view.)",
      "fixtures › web › views › back: fixture.second: renders 0 rows, expected 1",
    ]);
  });

  it("replaces the page's states at each step that gives them, so {} clears them", async () => {
    const { run } = renderSteps();
    const done: PageStep = {
      name: "done",
      run: async () => releaseAll(),
      witness: "Saved",
      states: {},
    };
    expect(await run([NEXT, SAVE, done])).toEqual([]);
    cleanup();
    // Without states of its own, saving carries over, so Cancel is expected though it's gone.
    const again = renderSteps();
    expect(await again.run([NEXT, SAVE, { ...done, states: undefined }])).toEqual([
      'fixtures › web › views › done: fixture.second: part "cancel" doesn\'t render',
    ]);
  });

  it("expects a state-only part only in a state a step gives", async () => {
    const { run } = renderSteps();
    expect(await run([NEXT, { ...SAVE, states: undefined }])).toEqual([
      'fixtures › web › views › save: fixture.second: part "cancel" renders, though none of the states it shows in is driven',
    ]);
  });

  it("settles around a held request once what it shows meanwhile is on the page", async () => {
    const { run } = renderSteps();
    expect(await run([NEXT, SAVE])).toEqual([]);
  });

  it("fails a step whose held request finishes before its page is audited", async () => {
    const { run } = renderSteps();
    // The save is held, then answered while the step waits for "Saved".
    const finishes: PageStep = {
      ...SAVE,
      run: async () => {
        await click("Save second")();
        setTimeout(releaseAll, 50);
      },
      witness: "Saved",
    };
    await expect(run([NEXT, finishes])).rejects.toThrow(
      "fixtures › web › views › save lost its held state before the audit: POST /v1/save isn't waiting any more",
    );
  });

  it("fails a step whose held request stops showing what it shows meanwhile before the audit", async () => {
    const { run } = renderSteps(undefined, true);
    await expect(run([NEXT, { ...SAVE, witness: "Status hidden" }])).rejects.toThrow(
      'fixtures › web › views › save lost its held state before the audit: "Saving…" isn\'t showing while POST /v1/save waits',
    );
  });

  it("fails a step whose page never shows its witness", async () => {
    const { run } = renderSteps();
    await expect(run([{ ...NEXT, witness: "Third view" }])).rejects.toThrow(
      'fixtures › web › views › next never showed "Third view"',
    );
  });

  it("fails a step whose held request never shows what it shows meanwhile", async () => {
    const { run } = renderSteps();
    await expect(
      run([NEXT, { ...SAVE, held: [{ ...SAVING, witness: "Uploading…" }] }]),
    ).rejects.toThrow(
      'fixtures › web › views › save never showed "Uploading…" while POST /v1/save waited',
    );
  });

  it("fails a step that declares a held request the page never asks for", async () => {
    const { run } = renderSteps();
    await expect(run([{ ...NEXT, held: [SAVING] }])).rejects.toThrow(
      "fixtures › web › views › next holds POST /v1/save, but the page never asked for it",
    );
  });

  it("fails a step that leaves a request in flight it doesn't declare", async () => {
    const { run } = renderSteps();
    await expect(run([NEXT, { ...SAVE, held: undefined }])).rejects.toThrow(
      "fixtures › web › views › save never settled; in flight: POST /v1/save",
    );
  });

  it("waits for a request made outside React Query, too", async () => {
    const { run } = renderSteps();
    const raw: PageStep = {
      name: "raw",
      run: async () => {
        void fetch("/v1/b");
      },
      witness: { row: SECOND.id },
    };
    await expect(run([NEXT, raw])).rejects.toThrow(
      "fixtures › web › views › raw never settled; in flight: GET /v1/b",
    );
  });

  it("waits for a query that runs without a request, too", async () => {
    const { run } = renderSteps();
    const load: PageStep = { name: "load", run: click("Load more"), witness: { row: SECOND.id } };
    await expect(run([NEXT, load])).rejects.toThrow("fixtures › web › views › load never settled");
  });
});

describe("a held request's operation", () => {
  const both = (): PageStep => ({
    name: "both",
    run: click("Fetch both"),
    witness: { row: SECOND.id },
    held: [
      { request: "GET /v1/a", witness: { row: SECOND.id }, operation: { queryKey: BOTH_KEY } },
      { request: "GET /v1/b", witness: { row: SECOND.id }, operation: { queryKey: BOTH_KEY } },
    ],
  });

  it("lets one declared query wait on several held requests", async () => {
    const { run } = renderSteps();
    expect(await run([NEXT, both()])).toEqual([]);
  });

  it("doesn't let held requests stand in for another query that's running", async () => {
    const { run } = renderSteps();
    // Both declared holds start, so they're in flight and named; the unrelated query isn't.
    const bothAndMore: PageStep = {
      ...both(),
      name: "both and more",
      run: async () => {
        await click("Fetch both")();
        await click("Load more")();
      },
    };
    await expect(run([NEXT, bothAndMore])).rejects.toThrow(
      "fixtures › web › views › both and more never settled",
    );
  });

  it("matches a mutation only by the key it declares, and never a keyless one", async () => {
    const wrong = renderSteps();
    await expect(
      wrong.run([NEXT, { ...SAVE, held: [{ ...SAVING, operation: { mutationKey: ["other"] } }] }]),
    ).rejects.toThrow("fixtures › web › views › save never settled");
    cleanup();
    const keyless = renderSteps();
    const quiet: PageStep = {
      name: "quiet",
      run: click("Save quietly"),
      witness: { row: SECOND.id },
      held: [{ request: "POST /v1/quiet", witness: { row: SECOND.id } }],
    };
    await expect(keyless.run([NEXT, quiet])).rejects.toThrow(
      "fixtures › web › views › quiet never settled",
    );
  });

  it("fails a step whose declared operation isn't the one running at the audit", async () => {
    const { run } = renderSteps();
    // The request is held and allowed, but it's made outside React Query, so no query runs.
    const raw: PageStep = {
      name: "raw",
      run: async () => {
        void fetch("/v1/a");
      },
      witness: { row: SECOND.id },
      held: [
        { request: "GET /v1/a", witness: { row: SECOND.id }, operation: { queryKey: BOTH_KEY } },
      ],
    };
    await expect(run([NEXT, raw])).rejects.toThrow(
      'fixtures › web › views › raw lost its held state before the audit: GET /v1/a\'s query ["fixture","both"] isn\'t fetching',
    );
  });
});

// A row whose Fix opens a dialog that keeps its origin; Repair fixes it, so Fix goes away.
const CAPTURE = settingsEntry({
  id: "fixture.capture",
  section: "general",
  label: "Capture",
  description: "Something a dialog fixes.",
  parts: { fix: "Fix it", repair: "Repair" },
  presence: { fix: { states: ["fixable"] }, repair: { overlay: "fix" } },
  overlays: { fix: { captured: true, dialogActions: ["Close"] } },
});

function CapturedPage() {
  const [open, setOpen] = useState(false);
  const [fixed, setFixed] = useState(false);
  return (
    <>
      <SettingsItem entry={CAPTURE}>
        <span>Capture</span>
        {!fixed && (
          <button
            type="button"
            {...settingsControlProps(CAPTURE, "fix")}
            onClick={() => setOpen(true)}
          >
            Fix it
          </button>
        )}
      </SettingsItem>
      {open && (
        <div role="dialog" {...settingsOverlayProps(CAPTURE, "fix")}>
          <button
            type="button"
            {...settingsControlProps(CAPTURE, "repair")}
            onClick={() => setFixed(true)}
          >
            Repair
          </button>
          <button type="button" data-settings-dialog-action="Close" onClick={() => setOpen(false)}>
            Close
          </button>
        </div>
      )}
    </>
  );
}

describe("a page action through a captured dialog", () => {
  const ORIGIN = "fixture.capture:fix";
  const renderCaptured = (answers: SettingsRoute[] = []) => {
    const routes = settingsRouteTable(answers);
    vi.stubGlobal("fetch", routes.fetch);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <CapturedPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const entries = new Map([[CAPTURE.id, CAPTURE]]);
    return (steps: PageStep[]) =>
      runPageAction({
        action: { name: "fix", steps },
        where: "fixtures › web",
        client,
        routes,
        snapshot: {
          catalog: [CAPTURE],
          entries,
          drivenStates: new Map([[CAPTURE.id, new Set(["fixable"])]]),
        },
        observedRows: new Set(),
        observedParts: new Set(),
        timeout: 500,
      });
  };
  const open = (opens?: string): PageStep => ({
    name: "open",
    run: click("Fix it"),
    witness: "Repair",
    opens,
  });
  // Repair removes the opener, and with it the state it showed in.
  const repair: PageStep = { name: "repair", run: click("Repair"), witness: "Repair", states: {} };
  const close = (closes?: string, states?: Record<string, string[]>): PageStep => ({
    name: "close",
    run: click("Close"),
    witness: { row: CAPTURE.id },
    closes,
    states,
  });

  it("keeps the dialog valid on its proved origin once its opener goes, and clears what that step clears", async () => {
    expect(await renderCaptured()([open(ORIGIN), repair, close(ORIGIN)])).toEqual([]);
  });

  it("finds an unproved dialog, and a step carrying a state its page no longer shows", async () => {
    expect(await renderCaptured()([open(), close()])).toEqual([
      'fixtures › web › fix › open: overlay "fixture.capture:fix": no origin was proved for this captured overlay',
    ]);
    // Without clearing the state, the vanished opener is still required.
    const carried: PageStep = { ...repair, states: undefined };
    cleanup();
    expect(await renderCaptured()([open(ORIGIN), carried, close(ORIGIN)])).toContain(
      'fixtures › web › fix › repair: fixture.capture: part "fix" doesn\'t render',
    );
  });

  it("proves each opening afresh, and a reopen without its own proof fails", async () => {
    const fixable = { [CAPTURE.id]: ["fixable"] };
    expect(await renderCaptured()([open(ORIGIN), close(ORIGIN, fixable), open(), close()])).toEqual(
      [
        'fixtures › web › fix › open: overlay "fixture.capture:fix": no origin was proved for this captured overlay',
      ],
    );
    cleanup();
    expect(
      await renderCaptured()([open(ORIGIN), close(ORIGIN, fixable), open(ORIGIN), close(ORIGIN)]),
    ).toEqual([]);
  });

  it("refuses an opening that's already open, a close of none, one that shows nothing, or a dialog left open", async () => {
    await expect(renderCaptured()([open(ORIGIN), open(ORIGIN)])).rejects.toThrow(
      "fixtures › web › fix › open opens fixture.capture:fix, which is already open",
    );
    cleanup();
    await expect(renderCaptured()([close(ORIGIN)])).rejects.toThrow(
      "fixtures › web › fix › close closes fixture.capture:fix, which no step opened",
    );
    cleanup();
    const nothing: PageStep = {
      name: "nothing",
      run: async () => {},
      witness: { row: CAPTURE.id },
      opens: ORIGIN,
    };
    await expect(renderCaptured()([nothing])).rejects.toThrow(
      "fixtures › web › fix › nothing opens fixture.capture:fix, but no such overlay shows",
    );
    cleanup();
    await expect(renderCaptured()([open(ORIGIN)])).rejects.toThrow(
      "fixtures › web › fix leaves fixture.capture:fix open",
    );
  });

  it("requires each request a step sends to be answered while it runs", async () => {
    const cancel = { method: "DELETE" as const, url: "/v1/logins/1", body: {} };
    // Closing sends the cancel the step declares.
    const closeCancelling: PageStep = {
      ...close(ORIGIN),
      run: async () => {
        await click("Close")();
        await fetch("/v1/logins/1", { method: "DELETE" });
      },
      sends: ["DELETE /v1/logins/1"],
    };
    expect(await renderCaptured([cancel])([open(ORIGIN), closeCancelling])).toEqual([]);
    cleanup();
    // A close that sends nothing, or a request no route answers, never sent it.
    await expect(
      renderCaptured([cancel])([open(ORIGIN), { ...close(ORIGIN), sends: closeCancelling.sends }]),
    ).rejects.toThrow(
      "fixtures › web › fix › close never sent DELETE /v1/logins/1; it sent nothing",
    );
    cleanup();
    await expect(renderCaptured()([open(ORIGIN), closeCancelling])).rejects.toThrow(
      "never sent DELETE /v1/logins/1",
    );
  });

  it("proves an opening from its opener before the step runs, so a gone opener can't open it", async () => {
    // Repaired, the row no longer shows Fix it, so nothing can prove a new opening.
    const reopen: PageStep = {
      name: "reopen",
      run: async () => {},
      witness: { row: CAPTURE.id },
      opens: ORIGIN,
    };
    await expect(
      renderCaptured()([open(ORIGIN), repair, close(ORIGIN, {}), reopen]),
    ).rejects.toThrow(
      "overlay origin fixture.capture:fix: its opener isn't rendered and owned in its row",
    );
  });
});

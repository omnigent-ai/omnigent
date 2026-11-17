// How the real-page audit walks a page that changes under it: waiting for what a step shows,
// letting the page settle around requests a step holds on purpose, auditing each resulting page
// as a snapshot of its own, and noting every row and part that rendered along the way.

import { hashKey, type MutationKey, type QueryClient, type QueryKey } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import { expect } from "vitest";

import type { SettingsEntry } from "@/pages/settings/search/catalog/types";

import { auditSettingsDomFindings, renderedSettingsParts } from "./settingsDomAudit";
import type { SettingsRoute, SettingsRouteTable } from "./settingsPageRoutes";

/** What shows a state is ready: text, a row, a labelled control, or an option. */
export type Witness = string | { row: string } | { label: string } | { option: string };

const rowSelector = (id: string) => `[data-settings-item="${CSS.escape(id)}"]`;

/** Waits for the witness to be on the page. */
export async function witnessed(witness: Witness, timeout = 5_000) {
  if (typeof witness === "string") {
    await screen.findAllByText(witness, { exact: false }, { timeout });
    return;
  }
  if ("label" in witness) {
    await screen.findByLabelText(witness.label, {}, { timeout });
    return;
  }
  if ("option" in witness) {
    await screen.findByRole("option", { name: witness.option }, { timeout });
    return;
  }
  await waitFor(() => expect(document.querySelector(rowSelector(witness.row))).not.toBeNull(), {
    timeout,
  });
}

/** Throws unless the witness is on the page right now. */
export function witnessedNow(witness: Witness) {
  if (typeof witness === "string") screen.getAllByText(witness, { exact: false });
  else if ("row" in witness) {
    if (!document.querySelector(rowSelector(witness.row))) throw new Error("no row");
  } else if ("label" in witness) screen.getByLabelText(witness.label);
  else screen.getByRole("option", { name: witness.option });
}

/**
 * A request a step leaves waiting on purpose, as `METHOD /path?query`; what the page shows
 * while it waits, such as "Saving…"; and the query or mutation, by key, that's waiting on it.
 * A request made outside React Query names no operation.
 */
export interface HeldRequest {
  request: string;
  witness: Witness;
  operation?: { queryKey: QueryKey } | { mutationKey: MutationKey };
}

/** The keys of the queries fetching and the mutations pending now; a keyless mutation is `?`. */
function runningOperations(client: QueryClient): { queries: string[]; mutations: string[] } {
  return {
    queries: client
      .getQueryCache()
      .findAll({ fetchStatus: "fetching" })
      .map((query) => query.queryHash),
    mutations: client
      .getMutationCache()
      .findAll({ status: "pending" })
      .map((mutation) =>
        mutation.options.mutationKey === undefined ? "?" : hashKey(mutation.options.mutationKey),
      ),
  };
}

function heldOperations(held: readonly HeldRequest[]): {
  queries: Set<string>;
  mutations: Set<string>;
} {
  const queries = new Set<string>();
  const mutations = new Set<string>();
  for (const { operation } of held) {
    if (operation && "queryKey" in operation) queries.add(hashKey(operation.queryKey));
    if (operation && "mutationKey" in operation) mutations.add(hashKey(operation.mutationKey));
  }
  return { queries, mutations };
}

/**
 * Waits until nothing runs but the declared held requests and their own operations: no other
 * request waits on the route table, and every query fetching and mutation pending is one a held
 * request names, by key. A keyless mutation is never one of them.
 */
export async function waitUntilSettled({
  client,
  routes,
  held = [],
  timeout = 5_000,
}: {
  client: QueryClient;
  routes: SettingsRouteTable;
  held?: readonly HeldRequest[];
  timeout?: number;
}) {
  const allowed = new Set(held.map((each) => each.request));
  const operations = heldOperations(held);
  await waitFor(
    () => {
      expect(routes.inFlight().filter((request) => !allowed.has(request))).toEqual([]);
      const running = runningOperations(client);
      expect(running.queries.filter((query) => !operations.queries.has(query))).toEqual([]);
      expect(running.mutations.filter((mutation) => !operations.mutations.has(mutation))).toEqual(
        [],
      );
    },
    { timeout },
  );
}

/**
 * Why the declared held requests aren't all still waiting, with their operations running and
 * their witnesses showing, at this moment; or undefined when they are.
 */
function heldProblem(
  client: QueryClient,
  routes: SettingsRouteTable,
  held: readonly HeldRequest[],
): string | undefined {
  const inFlight = routes.inFlight();
  const running = runningOperations(client);
  for (const each of held) {
    if (!inFlight.includes(each.request)) return `${each.request} isn't waiting any more`;
    const { operation } = each;
    if (
      operation &&
      "queryKey" in operation &&
      !running.queries.includes(hashKey(operation.queryKey))
    ) {
      return `${each.request}'s query ${hashKey(operation.queryKey)} isn't fetching`;
    }
    if (
      operation &&
      "mutationKey" in operation &&
      !running.mutations.includes(hashKey(operation.mutationKey))
    ) {
      return `${each.request}'s mutation ${hashKey(operation.mutationKey)} isn't pending`;
    }
    try {
      witnessedNow(each.witness);
    } catch {
      return `${JSON.stringify(each.witness)} isn't showing while ${each.request} waits`;
    }
  }
  return undefined;
}

/** Notes every row, and every part of one, that's on the page now. */
export function observeRendered(
  entries: ReadonlyMap<string, SettingsEntry>,
  rows: Set<string>,
  parts: Set<string>,
) {
  for (const row of document.querySelectorAll("[data-settings-item]")) {
    rows.add(row.getAttribute("data-settings-item") ?? "");
  }
  for (const part of renderedSettingsParts([document.body], entries)) parts.add(part);
}

/** The whole page as it is now, and what it should show. */
export interface PageSnapshot {
  /** `section › scenario › …`, prefixed to each problem. */
  where: string;
  /** The section's own entries. */
  catalog: readonly SettingsEntry[];
  entries: ReadonlyMap<string, SettingsEntry>;
  /** Rows this page can't show, with why. */
  hides?: Readonly<Record<string, string>>;
  drivenStates?: ReadonlyMap<string, ReadonlySet<string>>;
  /** The captured overlays open now, each proved from its opener before it opened. */
  origins?: readonly string[];
}

/**
 * What's wrong with the whole page now, as a snapshot of its own: a row it hides that renders
 * anyway, and every finding of the DOM audit, unowned controls included.
 */
export function wholePageProblems({
  where,
  catalog,
  entries,
  hides = {},
  drivenStates,
  origins,
}: PageSnapshot): string[] {
  const problems: string[] = [];
  for (const [id, reason] of Object.entries(hides)) {
    if (document.querySelector(rowSelector(id))) {
      problems.push(`${where}: ${id} renders, though it's hidden here (${reason})`);
    }
  }
  const expected = catalog.filter((entry) => !(entry.id in hides));
  for (const finding of auditSettingsDomFindings({
    roots: [document.body],
    entries,
    expected,
    drivenStates,
    origins,
  })) {
    problems.push(`${where}: ${finding.message}`);
  }
  return problems;
}

/** One thing done to the page that changes it, such as following a link to another view. */
export interface PageStep {
  name: string;
  run: () => Promise<void>;
  /** Shows the page it leads to is ready. */
  witness: Witness;
  /**
   * The states entries are in on the page it leads to, by entry id. When given, they replace the
   * previous page's states whole, so `{}` clears them; otherwise they carry over.
   */
  states?: Readonly<Record<string, readonly string[]>>;
  /**
   * Rows the page it leads to can't show, with why. When given, they replace the previous page's
   * whole; otherwise they carry over.
   */
  hides?: Readonly<Record<string, string>>;
  /** Requests it leaves waiting on purpose, each with what shows while it waits. */
  held?: readonly HeldRequest[];
  /**
   * The captured overlay this step opens, by its opener, `entry:part`: proved rendered and
   * owned before the step runs, it stands in for that opener until a step closes it.
   */
  opens?: string;
  /** The captured overlay this step closes, which must be open; its proof ends with it. */
  closes?: string;
  /** How the server answers from this step on, set just before it runs, as a host changes. */
  routes?: readonly SettingsRoute[];
  /** Requests the step must make, `METHOD /path?query`, each answered by the time it settles. */
  sends?: readonly string[];
}

/** Steps through pages that replace each other, each audited as a whole page of its own. */
export interface PageAction {
  name: string;
  steps: readonly PageStep[];
}

/**
 * Runs each step in turn. After each one: its held requests are in flight and showing their
 * witnesses, its own witness shows, and everything else settles. Then, as one snapshot, the held
 * requests are checked again and the whole page is audited, with the hides and states that step
 * leaves. Every row and part seen is noted.
 */
export async function runPageAction({
  action,
  where,
  client,
  routes,
  snapshot,
  observedRows,
  observedParts,
  timeout = 5_000,
}: {
  action: PageAction;
  where: string;
  client: QueryClient;
  routes: SettingsRouteTable;
  /** The page as the scenario left it; a step may replace its hides and states. */
  snapshot: Omit<PageSnapshot, "where">;
  observedRows: Set<string>;
  observedParts: Set<string>;
  timeout?: number;
}): Promise<string[]> {
  const problems: string[] = [];
  let hides = snapshot.hides ?? {};
  let drivenStates: ReadonlyMap<string, ReadonlySet<string>> = snapshot.drivenStates ?? new Map();
  // The captured overlays this action has open, each proved for its own opening.
  let origins: readonly string[] = [];
  const overlayShown = (origin: string) =>
    document.querySelector(`[data-settings-overlay-owner="${CSS.escape(origin)}"]`) !== null;
  for (const step of action.steps) {
    const stepWhere = `${where} › ${action.name} › ${step.name}`;
    if (step.opens !== undefined) {
      if (origins.includes(step.opens)) {
        throw new Error(`${stepWhere} opens ${step.opens}, which is already open`);
      }
      // Proved as the opener is now, before the step opens what it opens.
      provenOverlayOrigin(step.opens);
    }
    if (step.closes !== undefined && !origins.includes(step.closes)) {
      throw new Error(`${stepWhere} closes ${step.closes}, which no step opened`);
    }
    for (const route of step.routes ?? []) routes.set(route);
    const answeredBefore = routes.answered().length;
    // oxlint-disable-next-line no-await-in-loop -- each step after the one before
    await step.run();
    for (const held of step.held ?? []) {
      // oxlint-disable-next-line no-await-in-loop -- each held request in turn
      await waitFor(() => expect(routes.inFlight()).toContain(held.request), { timeout }).catch(
        (error: unknown) => {
          throw new Error(`${stepWhere} holds ${held.request}, but the page never asked for it`, {
            cause: error,
          });
        },
      );
      // oxlint-disable-next-line no-await-in-loop -- and what shows while it waits
      await witnessed(held.witness, timeout).catch((error: unknown) => {
        throw new Error(
          `${stepWhere} never showed ${JSON.stringify(held.witness)} while ${held.request} waited`,
          {
            cause: error,
          },
        );
      });
    }
    // oxlint-disable-next-line no-await-in-loop -- its own witness
    await witnessed(step.witness, timeout).catch((error: unknown) => {
      throw new Error(`${stepWhere} never showed ${JSON.stringify(step.witness)}`, {
        cause: error,
      });
    });
    // oxlint-disable-next-line no-await-in-loop -- then everything but what it holds
    await waitUntilSettled({ client, routes, held: step.held, timeout }).catch((error: unknown) => {
      throw new Error(`${stepWhere} never settled; in flight: ${routes.inFlight().join(", ")}`, {
        cause: error,
      });
    });
    const answered = routes.answered().slice(answeredBefore);
    for (const request of step.sends ?? []) {
      if (!answered.includes(request)) {
        throw new Error(
          `${stepWhere} never sent ${request}; it sent ${answered.join(", ") || "nothing"}`,
        );
      }
    }
    if (step.opens !== undefined) {
      if (!overlayShown(step.opens)) {
        throw new Error(`${stepWhere} opens ${step.opens}, but no such overlay shows`);
      }
      origins = [...origins, step.opens];
    }
    if (step.closes !== undefined) {
      if (overlayShown(step.closes)) {
        throw new Error(`${stepWhere} closes ${step.closes}, but it still shows`);
      }
      origins = origins.filter((origin) => origin !== step.closes);
    }
    if (step.hides) hides = step.hides;
    if (step.states) {
      drivenStates = new Map(
        Object.entries(step.states).map(([id, driven]) => [id, new Set(driven)]),
      );
    }
    // The held state and the audit are one snapshot: nothing may finish between them.
    const held = heldProblem(client, routes, step.held ?? []);
    if (held) throw new Error(`${stepWhere} lost its held state before the audit: ${held}`);
    problems.push(
      ...wholePageProblems({
        where: stepWhere,
        catalog: snapshot.catalog,
        entries: snapshot.entries,
        hides,
        drivenStates,
        origins,
      }),
    );
    observeRendered(snapshot.entries, observedRows, observedParts);
  }
  // A dialog an action opens, it closes, so neither it nor its proof reaches the next.
  if (origins.length > 0) {
    throw new Error(`${where} › ${action.name} leaves ${origins.join(", ")} open`);
  }
  return problems;
}

/** What can open an overlay: a button, a link, or an element in one of their roles. */
const OVERLAY_OPENER =
  'button, a[href], [role="button"], [role="link"], [role="menuitem"], [role="tab"]';

/**
 * Proves a captured overlay's origin before its action opens it: its opener, `entry:part`,
 * rendered and owned inside its entry's row. The overlay is then audited against that origin
 * while it stays open, even once the opener is gone; the proof ends when it closes.
 */
export function provenOverlayOrigin(origin: string): string {
  const separator = origin.indexOf(":");
  const id = separator === -1 ? origin : origin.slice(0, separator);
  // A control, owned as that part, whose nearest row is its own entry's and that no overlay holds.
  const opener = [
    ...document.querySelectorAll(`[data-settings-control="${CSS.escape(origin)}"]`),
  ].find(
    (element) =>
      element.matches(OVERLAY_OPENER) &&
      element.closest("[data-settings-item]")?.getAttribute("data-settings-item") === id &&
      element.closest("[data-settings-overlay-owner]") === null,
  );
  if (!opener) {
    throw new Error(`overlay origin ${origin}: its opener isn't rendered and owned in its row`);
  }
  return origin;
}

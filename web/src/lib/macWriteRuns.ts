// One click authorizing a bulk run's writes on this Mac. The click asks the shell for a grant
// per account the run writes as, naming each change. A send carries a grant only while its own
// run sends it (`within`), and only for exactly one of the changes that run named, as the same
// account and workspace user. Without a shell that can, writes go as they always have.

import { randomUUID } from "@/lib/randomUUID";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import {
  type DesktopCodeHost,
  authorizeDesktopCodeHostRun,
  endDesktopCodeHostRun,
  hasRunGrantBridge,
} from "@/lib/nativeBridge";

/** One change a run will make, as the route it goes on. */
export interface MacWriteRunEntry {
  route: {
    target: Pick<CodeHostPullRequest, "host" | "repository" | "number">;
    actor: { login: string; generation: number };
    reader: { host: string; login: string } | null;
  };
  operation: string;
  params: Record<string, unknown>;
}

/** What a send knows about itself, to find the grant its own run minted for it. */
export interface MacWriteRunLookup {
  owner: string;
  authGeneration: number;
  route: MacWriteRunEntry["route"];
  operation: string;
}

/** The run a click started. */
export interface MacWriteRun {
  /** Whether the shell was asked for any grant; a run on an older shell asks for none. */
  readonly capable: boolean;
  /** Settles once the shell has answered every grant asked for; never rejects. */
  readonly ready: Promise<void>;
  /** Run `send` as this run's: a write it makes synchronously may carry this run's grant. */
  within: <Result>(send: () => Result) => Result;
  /** The grant this run holds for exactly this change, or null. */
  grantFor: (lookup: MacWriteRunLookup) => string | null;
  /** End every grant the run asked for, at once; later calls do nothing. */
  end: () => void;
}

/** An operation sent once for a whole batch: its grant names the batch, not one PR. */
const BATCH_OPERATIONS = new Set(["pull_request_auto_merge_enable_many"]);

interface RunGrant {
  requestId: string;
  identity: string;
  keys: ReadonlySet<string>;
  grantId: string | null;
}

/** The run whose send is being made right now, if any; set only within that run's own call. */
let sendingRun: MacWriteRun | null = null;

const lower = (text: string) => text.toLowerCase();

/** Who a route writes as: host, login, sign-in and the named login, if any. */
function identityOf(route: MacWriteRunEntry["route"]): string {
  return JSON.stringify([
    lower(route.target.host),
    lower(route.actor.login),
    route.actor.generation,
    route.reader ? lower(route.reader.login) : null,
  ]);
}

function changeKey(operation: string, target: MacWriteRunEntry["route"]["target"]): string {
  if (BATCH_OPERATIONS.has(operation)) return `${operation}\nbatch`;
  return [operation, lower(target.host), lower(target.repository), target.number].join("\n");
}

function newRequestId(): string {
  return randomUUID();
}

/**
 * Ask for the grants a run's `entries` need; call it from the click, before anything is
 * awaited. `writes` is the shell's GitHub write capability. Entries on a shell that can't
 * authorize a run are left as they are: each still needs its own click.
 */
export function startMacWriteRun({
  owner,
  authGeneration,
  writes,
  entries,
}: {
  owner: string;
  authGeneration: number;
  writes: DesktopCodeHost["writes"] | null | undefined;
  entries: readonly MacWriteRunEntry[];
}): MacWriteRun {
  const groups = new Map<string, MacWriteRunEntry[]>();
  for (const entry of entries) {
    const identity = identityOf(entry.route);
    groups.set(identity, [...(groups.get(identity) ?? []), entry]);
  }
  const grants: RunGrant[] = [];
  const answers: Promise<void>[] = [];
  let ended = false;
  for (const [identity, group] of groups) {
    const { route } = group[0]!;
    if (!hasRunGrantBridge(writes, route.reader !== null)) continue;
    const grant: RunGrant = {
      requestId: newRequestId(),
      identity,
      keys: new Set(group.map((entry) => changeKey(entry.operation, entry.route.target))),
      grantId: null,
    };
    grants.push(grant);
    const request = {
      requestId: grant.requestId,
      provider: "github" as const,
      host: route.target.host,
      actor: { login: route.actor.login, generation: route.actor.generation },
      entries: group.map(({ operation, params }) => ({ operation, params })),
    };
    answers.push(
      authorizeDesktopCodeHostRun(
        route.reader ? { ...request, macLogin: route.reader.login } : request,
      ).then((answer) => {
        if (answer.ok && !ended) grant.grantId = answer.grantId;
      }),
    );
  }
  const run: MacWriteRun = {
    capable: grants.length > 0,
    ready: Promise.all(answers).then(() => undefined),
    within: (send) => {
      const outer = sendingRun;
      sendingRun = run;
      try {
        return send();
      } finally {
        sendingRun = outer;
      }
    },
    grantFor: (lookup) => {
      if (ended || lookup.owner !== owner || lookup.authGeneration !== authGeneration) return null;
      const identity = identityOf(lookup.route);
      const key = changeKey(lookup.operation, lookup.route.target);
      const grant = grants.find(
        (each) => each.grantId && each.identity === identity && each.keys.has(key),
      );
      return grant?.grantId ?? null;
    },
    end: () => {
      if (ended) return;
      ended = true;
      for (const grant of grants) {
        grant.grantId = null;
        // Also ends a grant whose mint hasn't answered yet: the shell then stores none.
        endDesktopCodeHostRun(grant.requestId);
      }
    },
  };
  return run;
}

/**
 * The grant the run sending right now minted for exactly this change, or null: a send outside
 * its run's own call, or one its run didn't name, needs its own click.
 */
export function macWriteRunGrant(lookup: MacWriteRunLookup): string | null {
  return sendingRun?.grantFor(lookup) ?? null;
}

/** Forget the run being sent, for tests. */
export function resetMacWriteRunsForTests(): void {
  sendingRun = null;
}

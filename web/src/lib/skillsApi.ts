// Typed client for the cross-harness Skill Registry endpoints.
//
// The backend (a sibling track, confirmed shipped) implements the read-only
// catalog behind the routes below; this module is the browser-facing seam.
// Field names are converted from the server's snake_case wire shape to
// camelCase at the API boundary so page/hook code never depends on raw wire
// keys — same convention as `codexGoalApi.ts`.
//
//   GET  /v1/skills?session_id=<id>&include_other_tools=<bool>       → { object, data[], include_other_tools, hidden_count }
//   GET  /v1/skills/{id}?session_id=<id>&include_other_tools=<bool>  → summary + { content, provenance, selected_winner, conflict_candidates, delivery }
//   GET  /v1/skills/trust                                            → { value, include_other_tools }
//   PUT  /v1/skills/trust  { value }                                 → { value, include_other_tools }
//
// The catalog is SESSION-CONTEXTUAL: bundle/workspace/provider skills live on
// the bound runner, so both catalog + detail require the active session id.
// The caller resolves the current bound session (see `useActiveSkillSession`)
// and passes it here; with no bound session the page shows an empty state and
// never calls these routes.
//
// Contract semantics (from the shared plan + backend integration notes):
//  - The primary UX is harness-neutral. `origin` is one of the three USER
//    concepts — built_in / workspace / personal. Harness/vendor provenance
//    (provider, path, source kind, coords, digest, conflicts, delivery) is
//    diagnostic only and lives under a single Advanced section.
//  - `enabled` / `available` are surfaced READ-ONLY: the backend reports both
//    as `true` and invented no per-skill mutation, so the UI renders
//    availability as status, never a persisted toggle.
//  - The include-other-tools switch maps to the trust setting. The server's
//    internal values are `current` / `all-host`; the PUT body is `{ value }`.
//    The frontend works in the normalized boolean `includeOtherTools`.
//
// Every call talks to the real backend. There is no runtime fixture fallback:
// a 404/501 (endpoint missing), a network failure, an auth rejection, or a
// runner error propagates as an `ApiError`/`TypeError` so the page shows its
// real error state rather than inventing skills that don't exist on the host.
// Fixtures live only in the unit tests and the Playwright route interception.

import { authenticatedFetch } from "./identity";
import { ApiError } from "./sessionsApi";

/** The three user-facing origin groups. Never a harness/vendor name. */
export type SkillOrigin = "built_in" | "workspace" | "personal";

/** Discovery provider — provenance only, shown under Advanced details. */
export type SkillDiscoveryProvider = "omnigent" | "claude" | "codex" | "cursor" | string;

/** Internal server trust values. The UI works in a boolean; this is the wire form. */
export type SkillTrustValue = "current" | "all-host";

// ── Wire shapes (server snake_case) ──────────────────────────────────────────

interface SkillSummaryWire {
  id: string;
  name: string;
  description: string;
  origin: SkillOrigin;
  enabled: boolean;
  available: boolean;
  has_conflict: boolean;
  updated_at?: number | null;
}

interface SkillCatalogWire {
  object: "list";
  data: SkillSummaryWire[];
  /** Effective include-other-tools setting the server resolved for this call. */
  include_other_tools: boolean;
  /** Count of skills hidden because include-other-tools is off. */
  hidden_count: number;
}

interface SkillProvenanceWire {
  provider: SkillDiscoveryProvider;
  original_path: string;
  source_kind: string;
  source_coords: string;
  digest: string;
}

interface SkillDetailWire extends SkillSummaryWire {
  /** Raw SKILL.md instruction body (frontmatter + markdown). */
  content: string;
  provenance: SkillProvenanceWire;
  /** Canonical coords of the winner Omnigent resolved by precedence. */
  selected_winner: string;
  /** Canonical coords of shadowed same-name candidates (diagnostics). */
  conflict_candidates: string[];
  delivery: { mode: string };
}

interface SkillTrustWire {
  value: SkillTrustValue;
  include_other_tools: boolean;
}

// ── Browser-facing shapes (camelCase) ────────────────────────────────────────

/** A catalog row — the shape the master list renders. */
export interface SkillSummary {
  id: string;
  name: string;
  description: string;
  origin: SkillOrigin;
  /** Whether the skill is active in-session. Rendered read-only. */
  enabled: boolean;
  /** Whether the skill is available (discovered + trusted). Rendered read-only. */
  available: boolean;
  /** True when another source defines the same name (shown under Advanced). */
  hasConflict: boolean;
  updatedAt: number | null;
}

/** The catalog list plus its resolution envelope. */
export interface SkillCatalog {
  skills: SkillSummary[];
  /** The include-other-tools setting the server applied for this response. */
  includeOtherTools: boolean;
  /** How many skills are hidden because include-other-tools is off. */
  hiddenCount: number;
}

/** One entry in a name-conflict resolution stack (Advanced details only). */
export interface SkillConflictCandidate {
  /** Canonical coordinates identifying the source, e.g. `personal:codex:foo`. */
  coords: string;
  /** True for the winner Omnigent resolved by precedence. */
  selected: boolean;
}

/** Diagnostic provenance — the ONLY place harness/vendor facts surface. */
export interface SkillAdvanced {
  discoveryProvider: SkillDiscoveryProvider;
  sourceKind: string;
  /** Human delivery summary, e.g. "Automatic". */
  delivery: string;
  originPath: string;
  canonicalId: string;
  digest: string;
  /** Winner + shadowed candidates by coords; empty when there's no conflict. */
  conflicts: SkillConflictCandidate[];
}

/** Full detail for the selected skill. */
export interface SkillDetail extends SkillSummary {
  /** Raw SKILL.md instruction body (frontmatter + markdown). */
  instructions: string;
  /**
   * Human overview paragraph. The backend catalog doesn't carry one, so this
   * is `null` for real responses; the page falls back to `description`.
   */
  overview: string | null;
  advanced: SkillAdvanced;
}

// ── Origin display helpers (client-derived) ──────────────────────────────────

/** Short user-facing label for an origin group. */
export function originLabel(origin: SkillOrigin): string {
  switch (origin) {
    case "built_in":
      return "Built in";
    case "workspace":
      return "Workspace";
    case "personal":
      return "Personal";
  }
}

/** One-line explainer for an origin group, shown in the detail's Origin row. */
export function originExplainer(origin: SkillOrigin): string {
  switch (origin) {
    case "built_in":
      return "Ships with Omnigent.";
    case "workspace":
      return "Defined in this project.";
    case "personal":
      return "From your personal skill library.";
  }
}

// ── Wire → browser projection ────────────────────────────────────────────────

function toSummary(w: SkillSummaryWire): SkillSummary {
  return {
    id: w.id,
    name: w.name,
    description: w.description,
    origin: w.origin,
    enabled: w.enabled,
    available: w.available,
    hasConflict: w.has_conflict,
    updatedAt: w.updated_at ?? null,
  };
}

function toDetail(w: SkillDetailWire): SkillDetail {
  const conflicts: SkillConflictCandidate[] = w.conflict_candidates.length
    ? [
        { coords: w.selected_winner, selected: true },
        ...w.conflict_candidates.map((coords) => ({ coords, selected: false })),
      ]
    : [];
  return {
    ...toSummary(w),
    instructions: w.content,
    overview: null,
    advanced: {
      discoveryProvider: w.provenance.provider,
      sourceKind: w.provenance.source_kind,
      // Backend reports a structured `{ mode: "automatic" }`; render a friendly
      // sentence so the Advanced row reads as prose.
      delivery: w.delivery?.mode === "automatic" ? "Automatic" : (w.delivery?.mode ?? "Automatic"),
      originPath: w.provenance.original_path,
      canonicalId: w.provenance.source_coords,
      digest: w.provenance.digest,
      conflicts,
    },
  };
}

async function readJsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    // Reuse the AP error-body convention: prefer `error.message`/`error.code`.
    let message = `${res.status} ${res.statusText}`;
    let code: string | null = null;
    try {
      const body = (await res.json()) as { error?: { code?: string; message?: string } };
      if (body.error?.message) message = body.error.message;
      if (body.error?.code) code = body.error.code;
    } catch {
      // Non-JSON / empty body — keep the status-line fallback.
    }
    throw new ApiError(message, res.status, code);
  }
  return (await res.json()) as T;
}

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Fetch the harness-neutral skill catalog for a bound session. The catalog is
 * session-contextual (bundle/workspace/provider skills resolve on that
 * session's runner), so `sessionId` is required. `includeOtherTools` maps to
 * the `include_other_tools` query flag (the trust widening). Any request
 * failure — endpoint missing, network, auth, runner — propagates to the caller.
 */
export async function getSkillCatalog(
  sessionId: string,
  includeOtherTools: boolean,
): Promise<SkillCatalog> {
  const params = new URLSearchParams({
    session_id: sessionId,
    include_other_tools: includeOtherTools ? "true" : "false",
  });
  const res = await authenticatedFetch(`/v1/skills?${params.toString()}`);
  const wire = await readJsonOrThrow<SkillCatalogWire>(res);
  return {
    skills: wire.data.map(toSummary),
    includeOtherTools: wire.include_other_tools,
    hiddenCount: wire.hidden_count,
  };
}

/**
 * Fetch one skill's full detail (instructions + provenance) in the same
 * session + trust context as the list it was selected from — the backend
 * resolves the same winner only when `session_id` + `include_other_tools`
 * match the catalog call. Any request failure propagates to the caller.
 */
export async function getSkillDetail(
  id: string,
  sessionId: string,
  includeOtherTools: boolean,
): Promise<SkillDetail> {
  const params = new URLSearchParams({
    session_id: sessionId,
    include_other_tools: includeOtherTools ? "true" : "false",
  });
  const res = await authenticatedFetch(`/v1/skills/${encodeURIComponent(id)}?${params.toString()}`);
  const wire = await readJsonOrThrow<SkillDetailWire>(res);
  return toDetail(wire);
}

/** Read the persisted include-other-tools trust setting. */
export async function getSkillTrust(): Promise<boolean> {
  const res = await authenticatedFetch(`/v1/skills/trust`);
  const wire = await readJsonOrThrow<SkillTrustWire>(res);
  return wire.include_other_tools;
}

/** Persist the include-other-tools trust setting; returns the applied value. */
export async function setSkillTrust(includeOtherTools: boolean): Promise<boolean> {
  const value: SkillTrustValue = includeOtherTools ? "all-host" : "current";
  const res = await authenticatedFetch(`/v1/skills/trust`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
  const wire = await readJsonOrThrow<SkillTrustWire>(res);
  return wire.include_other_tools;
}

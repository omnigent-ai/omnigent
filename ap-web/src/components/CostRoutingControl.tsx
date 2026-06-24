import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import type { Session } from "@/lib/types";

/** Per-session cost-control switch value; `null` = unset (presents as off). */
export type CostControlMode = "on" | "off" | null;

/** Agent whose sessions carry the cost advisor (orchestrator only). */
export const COST_ROUTING_AGENT_NAME = "polly";

/**
 * Whether a session should surface the cost-routing control.
 *
 * The advisor governs only the orchestrator's own brain, so the gate
 * must exclude sub-agent (child) sessions: workers spawned via
 * `sys_session_send` inherit the parent bundle's `agentName`
 * (`"polly"`), so an agent-name check alone wrongly matches them.
 * `parentSessionId` — set on every child snapshot, `null` for
 * top-level sessions — is the discriminator.
 *
 * @param session The session snapshot (or any subset carrying agent
 *   identity + parent linkage); `null`/`undefined` while loading or
 *   on the landing page.
 * @returns `true` only for a top-level (non-child) polly session.
 */
export function isCostRoutingSession(
  session: Pick<Session, "agentName" | "parentSessionId"> | null | undefined,
): boolean {
  return session?.agentName === COST_ROUTING_AGENT_NAME && session.parentSessionId == null;
}

/** Conversation label the cost advisor persists its latest plan under. */
export const COST_CONTROL_PLAN_LABEL = "cost_control.plan";

/**
 * The advisor's latest per-turn verdict, parsed from the v3
 * `cost_control.plan` conversation label.
 *
 * @param tier Price tier the advisor judged for the turn.
 * @param model Model id the tier resolved to, e.g.
 *   `"databricks-claude-haiku-4-5"`.
 * @param applied `true` when routing was applied; `false` in
 *   advise/shadow mode.
 * @param rationale Advisor's explanation; `null` when absent.
 * @param turnAnchor ISO timestamp of the turn that produced the
 *   verdict, e.g. `"2026-06-10T12:00:00+00:00"`; `null` when absent.
 */
export interface CostRoutingVerdict {
  tier: "cheap" | "medium" | "expensive";
  model: string;
  applied: boolean;
  rationale: string | null;
  turnAnchor: string | null;
}

/**
 * Parse the advisor's latest verdict from a session's labels.
 *
 * Every malformed or non-v3 shape (legacy v2 included) collapses to
 * `null` so the control degrades to a plain mode toggle.
 *
 * @param labels Session snapshot labels, e.g.
 *   `{"cost_control.plan": "{\"version\": 3, ...}"}`; `undefined`
 *   when the snapshot hasn't loaded.
 * @returns The parsed verdict, or `null` when no valid v3 plan exists.
 */
export function parseCostRoutingVerdict(
  labels: Record<string, string> | undefined,
): CostRoutingVerdict | null {
  const raw = labels?.[COST_CONTROL_PLAN_LABEL];
  if (!raw) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof payload !== "object" || payload === null) return null;
  const plan = payload as Record<string, unknown>;
  if (plan.version !== 3) return null;
  const tier = plan.tier;
  if (tier !== "cheap" && tier !== "medium" && tier !== "expensive") return null;
  if (typeof plan.model !== "string" || plan.model.length === 0) return null;
  if (typeof plan.applied !== "boolean") return null;
  return {
    tier,
    model: plan.model,
    applied: plan.applied,
    rationale: typeof plan.rationale === "string" ? plan.rationale : null,
    turnAnchor: typeof plan.turn_anchor === "string" ? plan.turn_anchor : null,
  };
}

// The tier-defining token of Claude model ids ("databricks-claude-haiku-4-5" → "haiku").
const MODEL_FAMILY_HINTS = ["haiku", "sonnet", "opus"] as const;

/**
 * Friendly short name for a model id, for the tooltip's verdict line.
 *
 * Lossy is fine — the tooltip is a glance surface, not an audit log.
 *
 * @param model Model id, e.g. `"databricks-claude-haiku-4-5"`.
 * @returns The short display name, e.g. `"haiku"`.
 */
export function shortModelName(model: string): string {
  const lower = model.toLowerCase();
  for (const family of MODEL_FAMILY_HINTS) {
    if (lower.includes(family)) return family;
  }
  return lower.startsWith("databricks-") ? model.slice("databricks-".length) : model;
}

// Lucide "waypoints" geometry — a routing topology: four nodes joined by
// connectors. Split so the on state fills the nodes and stages the
// connector traces in separately.
const WAYPOINT_NODES = [
  { cx: 12, cy: 4.5 },
  { cx: 4.5, cy: 12 },
  { cx: 19.5, cy: 12 },
  { cx: 12, cy: 19.5 },
] as const;
const WAYPOINT_TRACE_PATHS = ["m10.2 6.3-3.9 3.9", "M7 12h10", "m15.7 17.7-3.9-3.9"] as const;

/** Muted outline waypoints — the resting (off) face of the toggle. */
function SparkleOutline() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="size-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {WAYPOINT_NODES.map((n) => (
        <circle key={`${n.cx}-${n.cy}`} cx={n.cx} cy={n.cy} r={2.5} />
      ))}
    </svg>
  );
}

/**
 * Lit waypoints — solid-filled nodes with connector traces that stage in
 * just after the nodes land (see the `.imc-spark` delay in index.css).
 */
function SparkleLit() {
  return (
    <svg viewBox="0 0 24 24" className="size-4" fill="none" aria-hidden="true">
      {WAYPOINT_NODES.map((n) => (
        <circle
          key={`${n.cx}-${n.cy}`}
          cx={n.cx}
          cy={n.cy}
          r={2.5}
          fill="currentColor"
          stroke="currentColor"
          strokeWidth={0.75}
        />
      ))}
      {WAYPOINT_TRACE_PATHS.map((d) => (
        <path
          key={d}
          className="imc-spark"
          d={d}
          stroke="currentColor"
          strokeWidth={1.5}
          strokeLinecap="round"
        />
      ))}
    </svg>
  );
}

/**
 * Intelligent model router — a binary sparkle toggle in the composer.
 *
 * Click flips on ↔ off (an unset `null` presents — and flips — as off;
 * the control never emits `null`). Hovering reveals a two-line tooltip:
 * the control's name, plus the advisor's latest pick when the control is
 * on and a verdict exists.
 *
 * Visuals: two stacked sparkle layers cross-fade with a slight spring on
 * toggle, a soft halo backs the lit glyph, and a fresh verdict landing
 * mid-session replays a one-shot ring ping (CSS only — see the `imc-*`
 * block in index.css; the global reduced-motion gate covers all of it).
 *
 * Controlled: the chat composer wires it to the chatStore, the
 * new-session dialog to local state it sends at create time.
 *
 * @param value Current switch state; `null` = unset (presents as off).
 * @param onChange Toggle callback, called with `"on"` or `"off"`.
 * @param disabled Disables the trigger (read-only sessions).
 * @param verdict Latest parsed advisor verdict, or `null`/omitted when
 *   none exists (pre-session, non-advisor servers, malformed label).
 */
export function IntelligentModelControl({
  value,
  onChange,
  disabled = false,
  verdict = null,
}: {
  value: CostControlMode;
  onChange: (mode: CostControlMode) => void;
  disabled?: boolean;
  verdict?: CostRoutingVerdict | null;
}) {
  const { t } = useTranslation("common");
  const isOn = value === "on";

  // Fresh-verdict ping: bumping the key remounts the ring span, replaying
  // its one-shot CSS animation — no JS timers to leak. `undefined` marks
  // the pre-mount state so a verdict already present at first paint (e.g.
  // a reload) stays quiet; only verdicts that ARRIVE while mounted ping.
  const verdictSignature =
    verdict === null
      ? null
      : `${verdict.model}|${verdict.tier}|${verdict.applied}|${verdict.turnAnchor}`;
  const [pingKey, setPingKey] = useState(0);
  const lastSignature = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const previous = lastSignature.current;
    lastSignature.current = verdictSignature;
    if (previous === undefined) return;
    if (verdictSignature !== null && verdictSignature !== previous) {
      setPingKey((k) => k + 1);
    }
  }, [verdictSignature]);

  return (
    // Local provider so the control works outside the app shell (tests, dialogs).
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            disabled={disabled}
            aria-label={t("intelligentModelRouter")}
            aria-pressed={isOn}
            data-testid="cost-toggle-trigger"
            data-mode={isOn ? "on" : "off"}
            className="imc-toggle relative size-9 text-muted-foreground md:size-8"
            onClick={() => onChange(isOn ? "off" : "on")}
          >
            <span className="imc-halo" aria-hidden="true" />
            {isOn && pingKey > 0 && (
              <span
                key={pingKey}
                className="imc-ping"
                data-testid="imc-verdict-ping"
                aria-hidden="true"
              />
            )}
            <span className="imc-layer imc-layer-off" aria-hidden="true">
              <SparkleOutline />
            </span>
            <span className="imc-layer imc-layer-on" aria-hidden="true">
              <SparkleLit />
            </span>
          </Button>
        </TooltipTrigger>
        <TooltipContent
          side="top"
          sideOffset={6}
          className="flex-col items-start gap-0.5 px-3 py-2"
        >
          <span className="font-medium" data-testid="imc-tooltip-title">
            {t("intelligentModelRouter")}
          </span>
          {isOn && verdict !== null && (
            <span className="text-muted-foreground" data-testid="imc-verdict-line">
              {verdict.applied ? t("modelRouterPicked") : t("modelRouterWouldPick")}{" "}
              <span className="font-medium text-popover-foreground">
                {shortModelName(verdict.model)}
              </span>
              <span className="text-muted-foreground/80">{` · ${verdict.tier}`}</span>
            </span>
          )}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

import type { ComponentType, SVGProps } from "react";

import { cn } from "@/lib/utils";

/**
 * A brand/role glyph component — the shape every existing harness→icon
 * resolver already returns (e.g. ``ClaudeIcon``, ``BotIcon``). Used as the
 * ``harness`` fallback payload so a caller keeps its own iconKind mapping.
 */
export type AgentGlyph = ComponentType<SVGProps<SVGSVGElement>>;

/**
 * Image suffixes that mark an ``icon`` string as a path rather than an emoji.
 * Mirrors ``_ICON_IMAGE_SUFFIXES`` in ``omnigent/spec/validator.py`` so the
 * client classifies a spec's icon exactly as the server does.
 */
const ICON_IMAGE_SUFFIXES = [".svg", ".png", ".jpg", ".jpeg", ".webp"] as const;

/**
 * Whether a spec ``icon`` string should be treated as a path (served via the
 * icon endpoint) rather than an emoji grapheme.
 *
 * Mirrors ``omnigent.spec.validator.icon_looks_path_like``: path-like when it
 * contains a separator (``/`` or ``\``) or ends with a known image suffix;
 * everything else is an emoji.
 *
 * @param value - The raw icon string from the agent spec.
 * @returns ``true`` when the value denotes an image path.
 */
export function iconLooksPathLike(value: string): boolean {
  const lower = value.toLowerCase();
  return (
    value.includes("/") ||
    value.includes("\\") ||
    ICON_IMAGE_SUFFIXES.some((suffix) => lower.endsWith(suffix))
  );
}

/**
 * The read-only endpoint that streams a path-valued agent icon's bytes
 * (``GET /v1/agents/{agent_id}/icon``, added server-side alongside the payload
 * ``icon`` field). Relative like every other ``/v1`` request so it resolves
 * against the app origin; an ``<img>`` can't carry auth headers, and the
 * endpoint is read-only, so no ``authenticatedFetch`` wrapper is needed.
 *
 * @param agentId - The agent whose icon file to serve.
 * @returns The relative icon URL.
 */
export function agentIconUrl(agentId: string): string {
  return `/v1/agents/${encodeURIComponent(agentId)}/icon`;
}

/** The declared-icon-bearing fields the resolver reads off an agent. */
export interface DeclaredIconAgent {
  /** Raw spec ``icon``: an emoji grapheme, a relative image path, or null. */
  icon?: string | null;
  /** Agent id, used to build the icon endpoint URL for a path-valued icon. */
  id?: string | null;
}

/**
 * The resolved icon for an agent. ``emoji`` renders the grapheme, ``url``
 * renders an ``<img>`` at the icon endpoint, and ``harness`` carries the
 * caller's existing iconKind/harness fallback (a glyph component) so the
 * precedence lives in one place while each surface keeps its own fallback.
 */
export type AgentIconResolution<F> =
  | { kind: "emoji"; value: string }
  | { kind: "url"; value: string }
  | { kind: "harness"; value: F };

/**
 * Resolve which icon to render for an agent, declared-icon first.
 *
 * Precedence: a declared emoji grapheme wins, then a declared image path
 * (served via {@link agentIconUrl}), then the caller's existing
 * harness/iconKind fallback. Keeping this order here means both the agent
 * catalog card and the Agents-rail row honour a spec's ``icon`` identically —
 * a custom icon on one surface but not the other would be a defect.
 *
 * @param agent - The agent's declared-icon fields.
 * @param harnessFallback - Thunk returning the surface's existing icon when no
 *   declared icon applies (an image path with no id also degrades to this).
 * @returns The resolved icon.
 */
export function resolveAgentIcon<F>(
  agent: DeclaredIconAgent,
  harnessFallback: () => F,
): AgentIconResolution<F> {
  const icon = agent.icon?.trim();
  if (icon) {
    if (iconLooksPathLike(icon)) {
      const id = agent.id?.trim();
      if (id) return { kind: "url", value: agentIconUrl(id) };
      // Path-like but no id to address the endpoint — fall through.
    } else {
      return { kind: "emoji", value: icon };
    }
  }
  return { kind: "harness", value: harnessFallback() };
}

/**
 * Render a resolved agent icon. Emoji graphemes render in a sized box so they
 * line up with sibling glyphs; a path icon renders as an ``<img>`` at the icon
 * endpoint; the harness fallback renders the caller's glyph component. Always
 * decorative (``aria-hidden``) — the agent name beside it is the label.
 *
 * @param resolution - The output of {@link resolveAgentIcon}.
 * @param className - Sizing/color classes forwarded to the rendered icon.
 */
export function AgentIcon({
  resolution,
  className,
}: {
  resolution: AgentIconResolution<AgentGlyph>;
  className?: string;
}) {
  if (resolution.kind === "emoji") {
    return (
      <span
        aria-hidden="true"
        className={cn("inline-flex items-center justify-center leading-none", className)}
      >
        {resolution.value}
      </span>
    );
  }
  if (resolution.kind === "url") {
    return (
      <img
        src={resolution.value}
        alt=""
        aria-hidden="true"
        className={cn("object-contain", className)}
      />
    );
  }
  const Glyph = resolution.value;
  return <Glyph className={className} aria-hidden="true" />;
}

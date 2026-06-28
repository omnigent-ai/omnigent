/**
 * Registry mapping a built-in group's `iconKey` to its bundled SVG component.
 *
 * Built-in entity groups (Jira, GitHub) ship their logos as inline-SVG
 * components; the backend sends a stable `iconKey` ("jira"/"github") which the
 * picker resolves here. User-created groups upload an image instead and are
 * rendered from their `iconUrl`.
 */

import type { ComponentType, SVGProps } from "react";
import { GitHubIcon } from "./GitHubIcon";
import { JiraIcon } from "./JiraIcon";

const ICON_REGISTRY: Record<string, ComponentType<SVGProps<SVGSVGElement>>> = {
  jira: JiraIcon,
  github: GitHubIcon,
};

/** Resolve a built-in icon component by key, or `undefined` if unknown. */
export function getIconComponent(
  key: string | null | undefined,
): ComponentType<SVGProps<SVGSVGElement>> | undefined {
  return key ? ICON_REGISTRY[key] : undefined;
}

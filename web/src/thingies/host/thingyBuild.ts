// Building a new thingy from a description: its session gets the description with instructions to
// build and publish it.

import { composeThingySessionPrompt } from "./thingySession";
import type { Thingy } from "../api/thingyApi";

const LEADING_REQUEST =
  /^\s*(please\s+)?(build|create|make|design|i\s+want|i\s+need|give\s+me)(\s+me)?\s+((a|an|the)\s+)?/i;
const MAX_DERIVED_NAME = 60;

/** A short name from the first sentence of a description; the session can rename it later. */
export function thingyNameFrom(description: string): string {
  const sentence = description.trim().split(/\n|[.!?:;,](?:\s|$)/)[0] ?? "";
  const words = sentence
    .replace(LEADING_REQUEST, "")
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 6)
    .join(" ");
  const name = words ? words.charAt(0).toUpperCase() + words.slice(1) : "";
  return (name || "New thingy").slice(0, MAX_DERIVED_NAME);
}

/** Instructions for a thingy session to build a new, unpublished thingy from the viewer's description. */
export function thingyBuildInstructions(): string {
  return [
    "This thingy is new and has nothing published yet. Build it from my description below:",
    "- Write one self-contained HTML page that uses the Omnigent SDK (window.omnigent.connect(); the sys_thingy_revision_create tool describes it). Declare its collections, actions, rules and guide, upload it with sys_thingy_revision_create and publish it with sys_thingy_publish.",
    '- The page runs in a sandboxed frame that silently blocks forms, alert/confirm/prompt, localStorage and cookies, new windows and the network. Use type="button" buttons with click handlers and handle Enter with keydown instead of a form\'s submit, confirm inline instead of with confirm(), and keep state in memory or in records.',
    "- Seed clearly labeled synthetic sample data on first load so it works right away. Do not use real data unless I give it to you.",
    "- Data reads and writes often take a second or more. Show a spinner or a pending state on every action, from the click until the call returns (an added item shows as Adding… at once), and show failures with a retry.",
    "- For actions that run in a session, follow client.agent.watch and keep a spinner only while the status is pending, queued, running or needs_input. done, failed, cancelled, finished_unreported and interrupted mean it ended, even when the session never reported, so stop the spinner and offer a retry after interrupted.",
    "- Put data-omnigent-ref and data-omnigent-label on each record's elements, so a selection names the exact record.",
    '- Others may see it read-only. Put data-omnigent-requires="write" on every control that changes records and data-omnigent-requires="action" on every control that requests an action; the SDK disables them for viewers without that access and shows why.',
    "- Once you know what it is, give it a short name with sys_thingy_rename.",
    "- Then tell me what you built and what I could ask for next.",
    "",
    "My description:",
    "",
  ].join("\n");
}

/** What a new thingy's session reads before the viewer's description. */
export function composeThingyBuildPrompt(app: Pick<Thingy, "id" | "name">): string {
  return `${composeThingySessionPrompt(app)}\n\n${thingyBuildInstructions()}`;
}

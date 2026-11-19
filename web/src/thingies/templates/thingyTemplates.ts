// Starting points for a new thingy, each a working example of the SDK's patterns. A template's
// revision is uploaded and published as the thingy's first version; it seeds synthetic sample
// data on first load, so the thingy works before anyone connects real data.

import { DATA_STORY_REVISION } from "./dataStoryTemplate";
import type { CreateThingyRevisionRequest } from "../api/thingyApi";
import { REFERENCE_KANBAN_REVISION } from "../reference-kanban/referenceKanbanApp";

export type ThingyTemplateId = "kanban" | "data-story" | "blank";

export interface ThingyTemplate {
  id: ThingyTemplateId;
  name: string;
  description: string;
  /** What the template shows off, for its preview. */
  shows: string[];
  /** First requests offered in its thingy session. */
  suggestions: string[];
  /** Null for a blank thingy, which its session builds from scratch. */
  revision: CreateThingyRevisionRequest | null;
}

// Shown in the New thingy dialog and the template preview.
const messages = {
  kanbanName: "Kanban board",
  kanbanDescription:
    "Cards in columns your team moves, with a WIP limit and an agent that works on a card when you ask.",
  kanbanShowsCollections:
    "Collections with schemas and rules: a WIP limit, and one run record per card",
  kanbanShowsAction: "A declared action, Run card, with live progress from the session doing it",
  kanbanShowsSelection:
    "Cards that name themselves, so Reply and Ask in side chat point at the exact card",
  kanbanShowsGuide: "A guide that tells agents how the board works",
  kanbanSuggestColumns: "Rename the columns to To do, In review and Shipped",
  kanbanSuggestLimit: "Change the WIP limit to 3",
  kanbanSuggestConnect: "Connect it to my team's issue table",
  storyName: "Data story",
  storyDescription:
    "A scrolling, interactive report with annotated charts that an agent refreshes from a SQL table.",
  storyShowsHeadline: "A headline metric with its trend",
  storyShowsCharts: "Hand-drawn charts with annotations the agent writes",
  storyShowsInteraction: "Filters, brushing and drill-down",
  storyShowsRefresh: "A Refresh action with live progress, and Ask in side chat on any point",
  storySuggestConnect: "Connect it to my table",
  storySuggestExplain: "Explain the biggest change this week",
  storySuggestBreakdown: "Add a breakdown by region",
  blankName: "Blank",
  blankDescription: "An empty thingy. Describe what it should do in its session.",
  blankShows: "Nothing yet: its session builds it with you",
} as const;

export const THINGY_TEMPLATES: readonly ThingyTemplate[] = [
  {
    id: "kanban",
    name: messages.kanbanName,
    description: messages.kanbanDescription,
    shows: [
      messages.kanbanShowsCollections,
      messages.kanbanShowsAction,
      messages.kanbanShowsSelection,
      messages.kanbanShowsGuide,
    ],
    suggestions: [
      messages.kanbanSuggestColumns,
      messages.kanbanSuggestLimit,
      messages.kanbanSuggestConnect,
    ],
    revision: REFERENCE_KANBAN_REVISION,
  },
  {
    id: "data-story",
    name: messages.storyName,
    description: messages.storyDescription,
    shows: [
      messages.storyShowsHeadline,
      messages.storyShowsCharts,
      messages.storyShowsInteraction,
      messages.storyShowsRefresh,
    ],
    suggestions: [
      messages.storySuggestConnect,
      messages.storySuggestExplain,
      messages.storySuggestBreakdown,
    ],
    revision: DATA_STORY_REVISION,
  },
  {
    id: "blank",
    name: messages.blankName,
    description: messages.blankDescription,
    shows: [messages.blankShows],
    suggestions: [],
    revision: null,
  },
];

export function thingyTemplate(id: string | null | undefined): ThingyTemplate | undefined {
  return THINGY_TEMPLATES.find((template) => template.id === id);
}

// Thingies this tab just created from a template, so their session pane offers its first requests.
const startedFromTemplate = new Map<string, ThingyTemplateId>();

export function rememberThingyTemplate(appId: string, templateId: ThingyTemplateId): void {
  startedFromTemplate.set(appId, templateId);
}

export function templateForThingy(appId: string | undefined): ThingyTemplate | undefined {
  return appId === undefined ? undefined : thingyTemplate(startedFromTemplate.get(appId));
}

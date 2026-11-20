// Starting points for a new thingy, each a working example of the SDK's patterns. A template's
// revision is uploaded and published as the thingy's first version. The data story seeds
// synthetic sample data on first load; the work radar starts empty until its session scans the
// viewer's own sessions and designs the page for them.

import { DATA_STORY_REVISION } from "./dataStoryTemplate";
import type { CreateThingyRevisionRequest } from "../api/thingyApi";
import { WORK_RADAR_REVISION } from "./workRadarTemplate";

export type ThingyTemplateId = "work-radar" | "data-story" | "blank";

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
  radarName: "Work radar",
  radarDescription: "Sees what your recent sessions add up to, and what to do next.",
  radarShowsSummary:
    "What your recent sessions add up to, grouped by goal, with what is blocked or waiting on you",
  radarShowsDesign:
    "A page your thingy session designs for your work, such as a rollout as stages or reviews as lanes",
  radarShowsSteps:
    "Next steps such as ramping a flag or checking a metric, each a click away, with their progress",
  radarShowsBack: "Back to the previous look whenever a new design misses",
  radarSuggestScan: "Scan my sessions from the last 3 days",
  radarSuggestLanes: "Show my pull request reviews as lanes",
  radarSuggestWindow: "Scan 7 days by default and leave out trivial sessions",
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
    id: "work-radar",
    name: messages.radarName,
    description: messages.radarDescription,
    shows: [
      messages.radarShowsSummary,
      messages.radarShowsDesign,
      messages.radarShowsSteps,
      messages.radarShowsBack,
    ],
    suggestions: [
      messages.radarSuggestScan,
      messages.radarSuggestLanes,
      messages.radarSuggestWindow,
    ],
    revision: WORK_RADAR_REVISION,
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

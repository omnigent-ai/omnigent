// Starting points for a new gadget, each a working example of the SDK's patterns. A template's
// revision is published as the gadget's first version.

import { DATA_STORY_REVISION } from "./dataStoryTemplate";
import type { CreateGadgetRevisionRequest } from "../api/gadgetApi";
import { WHERE_WAS_I_REVISION } from "./whereWasITemplate";

export type GadgetTemplateId = "where-was-i" | "data-story" | "blank";

export interface GadgetTemplate {
  id: GadgetTemplateId;
  name: string;
  description: string;
  /** What the template shows off, for its preview. */
  shows: string[];
  /** First requests offered in its gadget session. */
  suggestions: string[];
  /** Null for a blank gadget, which its session builds from scratch. */
  revision: CreateGadgetRevisionRequest | null;
}

// Shown in the New gadget dialog and the template preview.
const messages = {
  radarName: "Where was I?",
  radarDescription: "Sees what your recent sessions add up to, and what to do next.",
  radarShowsSummary:
    "What your recent sessions add up to, grouped by goal, with what is blocked or waiting on you",
  radarShowsDesign:
    "A page your gadget session designs for your work, such as a rollout as stages or reviews as lanes",
  radarShowsSteps:
    "Only the step that is due in the workflows you repeat, such as a metric check after a ramp or the telemetry you meant to add; it learns from what you click",
  radarShowsBack: "Back to the previous look whenever a new design misses",
  radarSuggestScan: "Scan my sessions from the last 3 days",
  radarSuggestLanes: "Show my pull request reviews as lanes",
  radarSuggestWindow: "Scan 7 days by default and leave out trivial sessions",
  storyName: "Data story",
  storyDescription:
    "Finds the stories in your recent data work, then builds the one you pick from real data.",
  storyShowsIdeas:
    "Story ideas from your recent sessions: the tables you queried and the metrics you checked",
  storyShowsBuild:
    "The story you pick, built from real data: a headline number, annotated charts, the takeaways",
  storyShowsDesign: "A page your gadget session designs for that story, so no two look alike",
  storyShowsRefresh: "Refresh data, Back to the previous look, and where every number comes from",
  storySuggestFind: "Find stories in my sessions from the last 7 days",
  storySuggestOwn: "Build a story about my team's weekly active users",
  storySuggestWindow: "Look back 30 days for story ideas",
  blankName: "Blank",
  blankDescription: "An empty gadget. Describe what it should do in its session.",
  blankShows: "Nothing yet: its session builds it with you",
} as const;

export const GADGET_TEMPLATES: readonly GadgetTemplate[] = [
  {
    id: "where-was-i",
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
    revision: WHERE_WAS_I_REVISION,
  },
  {
    id: "data-story",
    name: messages.storyName,
    description: messages.storyDescription,
    shows: [
      messages.storyShowsIdeas,
      messages.storyShowsBuild,
      messages.storyShowsDesign,
      messages.storyShowsRefresh,
    ],
    suggestions: [messages.storySuggestFind, messages.storySuggestOwn, messages.storySuggestWindow],
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

export function gadgetTemplate(id: string | null | undefined): GadgetTemplate | undefined {
  return GADGET_TEMPLATES.find((template) => template.id === id);
}

// Gadgets this tab just created from a template, so their session pane offers its first requests.
const startedFromTemplate = new Map<string, GadgetTemplateId>();

export function rememberGadgetTemplate(appId: string, templateId: GadgetTemplateId): void {
  startedFromTemplate.set(appId, templateId);
}

export function templateForGadget(appId: string | undefined): GadgetTemplate | undefined {
  return appId === undefined ? undefined : gadgetTemplate(startedFromTemplate.get(appId));
}

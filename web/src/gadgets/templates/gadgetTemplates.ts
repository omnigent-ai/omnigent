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
  radarDescription: "Tracks every change you have in flight, from commit to impact report.",
  radarShowsSummary:
    "Each change from your recent sessions on its track: before the PR, in review, rolling out, or waiting on metrics",
  radarShowsDesign:
    "What waits on what, the PRs and flags involved, and the sessions working on each change",
  radarShowsSteps:
    "Only the next step you are sure to want, such as ramping a flag after two quiet days; it learns how each project ships from what you click",
  radarShowsBack:
    "A page your gadget session designs for your work, with Back to the previous look",
  radarSuggestScan: "Scan my sessions from the last 3 days",
  radarSuggestLanes: "Show my changes as a board by stage",
  radarSuggestWindow: "We don't use flags in the web app: we canary, then roll out",
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

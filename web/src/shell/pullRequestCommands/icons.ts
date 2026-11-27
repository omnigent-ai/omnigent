// Each pull request action's icon, shared by its menus and ⌘K.

import {
  ExternalLinkIcon,
  GitBranchIcon,
  GitPullRequestIcon,
  HashIcon,
  LinkIcon,
  type LucideIcon,
  PanelRightIcon,
  SquarePlusIcon,
  PlusIcon,
  RefreshCwIcon,
  TypeIcon,
  XIcon,
} from "lucide-react";

import type { PullRequestCommandId } from "./model";

export const PULL_REQUEST_COMMAND_ICONS: Record<PullRequestCommandId, LucideIcon> = {
  refresh: RefreshCwIcon,
  link: PlusIcon,
  open: GitPullRequestIcon,
  "open-in-new-tab": SquarePlusIcon,
  "open-in-workspace": PanelRightIcon,
  "open-on-provider": ExternalLinkIcon,
  "open-in-review-site": ExternalLinkIcon,
  "copy-link": LinkIcon,
  "copy-review-site-link": LinkIcon,
  "copy-number": HashIcon,
  "copy-title": TypeIcon,
  "copy-branch": GitBranchIcon,
  remove: XIcon,
};

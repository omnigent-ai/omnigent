// Per-device order of a pull request's conversation: Activity's Sort choice. Newest first by
// default.

import { createStoredChoicePreference } from "@/lib/storedChoicePreference";

import { COMMENTS_SORT_IDS, type CommentsSort } from "./pullRequestCommentsModel";

export const COMMENTS_ORDER_STORAGE_KEY = "omnigent:pull-request-comments-order";

export const commentsOrderPreference = createStoredChoicePreference<CommentsSort>(
  COMMENTS_ORDER_STORAGE_KEY,
  COMMENTS_SORT_IDS,
  "newest",
);

/** Live order, including a choice made in another window. */
export const useCommentsOrder = commentsOrderPreference.use;

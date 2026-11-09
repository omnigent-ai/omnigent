// Worker entry for `searchPatternInWorker`. Matches with the same `findMatches` the
// substring path uses, and replies with corpus indices rather than cloned targets.

import { findMatches } from "./pullRequestReview";
import type { PatternSearchReply, PatternSearchRequest } from "./pullRequestPatternSearch";

self.addEventListener("message", (event: MessageEvent<PatternSearchRequest>) => {
  const { corpus, query, options } = event.data;
  const indexOf = new Map(corpus.map((target, index) => [target, index]));
  const { matches, truncated, invalid } = findMatches(corpus, query, options);
  const reply: PatternSearchReply = {
    matches: matches.map((match) => ({
      index: indexOf.get(match.target) ?? -1,
      start: match.start,
      end: match.end,
    })),
    truncated,
    invalid,
  };
  self.postMessage(reply);
});

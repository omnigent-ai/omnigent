// Worker entry for `searchPatternInWorker`. Matches with the same `findMatches` the
// substring path uses, and replies with corpus indices rather than cloned targets.

import { findMatches } from "./pullRequestReview";
import type {
  PatternSearchMessage,
  PatternSearchReply,
  PatternSearchRequest,
} from "./pullRequestPatternSearch";

self.addEventListener("message", (event: MessageEvent<PatternSearchRequest>) => {
  const { corpus, query, options } = event.data;
  // The corpus has arrived, so the caller's match budget starts with the next message.
  const started: PatternSearchMessage = { started: true };
  self.postMessage(started);
  const indexOf = new Map(corpus.map((target, index) => [target, index]));
  const { matches, truncated, invalid } = findMatches(corpus, query, options);
  const reply: PatternSearchReply = {
    matches: matches.flatMap((match) => {
      const index = indexOf.get(match.target);
      return index === undefined ? [] : [{ index, start: match.start, end: match.end }];
    }),
    truncated,
    invalid,
  };
  self.postMessage(reply);
});

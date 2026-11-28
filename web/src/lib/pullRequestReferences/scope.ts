// What a view that shows PR references shares with them: what a `#123` or a link written in it
// names, and the view's cap on lookups. A view without one leaves
// references as written.

import { createContext, useContext } from "react";

import type { PageLookupBudget } from "./lookupQueue";
import { mentionHref } from "./mentions";
import {
  type PullRequestReference,
  type PullRequestReferenceContext,
  referenceFromLinkOnAnyHost,
  referenceFromShorthand,
  referenceFromUrl,
} from "./reference";
import type { ReferenceLookupRecord, SessionReferenceLookups } from "./sessionLookups";

/** A reference as the view reads it. */
export interface ScopedPullRequestReference {
  reference: PullRequestReference;
}

export interface PullRequestReferenceScopeValue {
  /** The PR a written `#123` names here; null leaves it as written. */
  shorthand: (text: string) => ScopedPullRequestReference | null;
  /** The PR a link names here; null leaves it a plain link. */
  link: (href: string | null | undefined) => ScopedPullRequestReference | null;
  /** Where a written `@login` or `@org/team` links here; absent or null leaves it as written. */
  mention?: (login: string) => string | null;
  budget: PageLookupBudget;
  /** The view's own record of what it looked up, where a later read of one is never sent. */
  record?: ReferenceLookupRecord;
  /** The session whose host looks up a PR this Mac doesn't read; absent for none. */
  hostSession?: string;
}

function scoped(reference: PullRequestReference | null): ScopedPullRequestReference | null {
  return reference ? { reference } : null;
}

/** A PR's view: `#123` is in its repository, and only links on its own host are read. */
export function pullRequestPageReferenceScope(
  context: PullRequestReferenceContext,
  budget: PageLookupBudget,
  hostSession?: string,
): PullRequestReferenceScopeValue {
  return {
    ...(hostSession ? { hostSession } : {}),
    shorthand: (text) => scoped(referenceFromShorthand(text, context)),
    link: (href) => scoped(referenceFromUrl(href, context)),
    // GitHub links a mention to its profile on the PR's own host, an Enterprise one too.
    mention: (login) => (context.provider === "github" ? mentionHref(context.host, login) : null),
    budget,
  };
}

/**
 * A session's chat: `#123` is in the session's repository when its host is known, and a link is
 * read on its own host. Both read as this Mac's Automatic account, as a chat names none, or
 * through `hostSession`'s host where this Mac doesn't read them.
 */
export function sessionReferenceScope(
  session: PullRequestReferenceContext | null,
  { budget, record }: SessionReferenceLookups,
  hostSession?: string,
): PullRequestReferenceScopeValue {
  return {
    ...(hostSession ? { hostSession } : {}),
    shorthand: (text) => (session ? scoped(referenceFromShorthand(text, session)) : null),
    link: (href) => scoped(referenceFromLinkOnAnyHost(href, session)),
    budget,
    record,
  };
}

export const PullRequestReferenceScopeContext =
  createContext<PullRequestReferenceScopeValue | null>(null);

/** The view's reference scope, or null where references stay as written. */
export function usePullRequestReferenceScope(): PullRequestReferenceScopeValue | null {
  return useContext(PullRequestReferenceScopeContext);
}

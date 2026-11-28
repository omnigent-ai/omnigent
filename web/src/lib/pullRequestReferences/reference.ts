// Which pull request a reference names, read against the page it's written on: `#123` is the
// page's repository's, `repo#123` its owner's, and a link names its own. A reference never
// guesses beyond that: on a page with no PR of its own, `#123` names nothing.

import { type CodeHostPullRequest, parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { parseSessionPullRequestUrl } from "@/lib/sessionPullRequestUrl";

import { detectPullRequestShorthand } from "./detect";

/** The page a reference is written on: its own PR's code host and repository. */
export interface PullRequestReferenceContext {
  provider: CodeHostPullRequest["provider"];
  /** Lowercase, as `parseCodeHostPullRequestUrl` gives it. */
  host: string;
  /** `owner/repo`. */
  repository: string;
}

/** A referenced PR and how the reference shows it. */
export interface PullRequestReference {
  target: CodeHostPullRequest;
  /** `#123` in the page's own repository, `owner/repo#123` elsewhere. */
  label: string;
}

/** The context of the page showing `pullRequestUrl`, or null when it isn't a PR link. */
export function referenceContextFor(
  pullRequestUrl: string | null | undefined,
): PullRequestReferenceContext | null {
  const page = parseSessionPullRequestUrl(pullRequestUrl);
  if (!page) return null;
  return { provider: page.provider, host: page.host, repository: page.repository };
}

/** The PR a written `#123`, `repo#123` or `owner/repo#123` names on this page; null otherwise. */
export function referenceFromShorthand(
  text: string,
  context: PullRequestReferenceContext,
): PullRequestReference | null {
  const [found] = detectPullRequestShorthand(text);
  if (!found || found.start !== 0 || found.end !== text.length) return null;
  const [pageOwner] = context.repository.split("/");
  const repository = found.repo ? `${found.owner ?? pageOwner}/${found.repo}` : context.repository;
  const target = parseCodeHostPullRequestUrl(
    `https://${context.host}/${repository}/pull/${found.number}`,
    githubHostsOf(context),
  );
  return target ? { target, label: labelFor(target, context) } : null;
}

/**
 * The PR a link names on the page's own code host; null for any other link, a PR on another
 * host included, which keeps its text and is never looked up as this page's reader.
 */
export function referenceFromUrl(
  href: string | null | undefined,
  context: PullRequestReferenceContext,
): PullRequestReference | null {
  const target = parseCodeHostPullRequestUrl(pullRequestPageOf(href), githubHostsOf(context));
  if (!target || target.provider !== context.provider || target.host !== context.host) return null;
  return { target, label: labelFor(target, context) };
}

/**
 * The PR a link names on whichever code host it's on, as a chat reads its links: it has no page
 * of its own, so each link is read on its own host. `session` only shortens the label.
 */
export function referenceFromLinkOnAnyHost(
  href: string | null | undefined,
  session: PullRequestReferenceContext | null,
): PullRequestReference | null {
  const page = pullRequestPageOf(href);
  const target = parseCodeHostPullRequestUrl(page, session ? githubHostsOf(session) : []);
  if (!target) return null;
  return { target, label: session ? labelFor(target, session) : fullLabel(target) };
}

/** A PR the session's own PR info names: its branch's PR or one linked to it. */
export interface SessionPullRequest {
  url: string;
  host?: string | null;
  provider?: string | null;
}

/**
 * What `#123` means in a session's chat: its repository, on the one code host its own PRs put
 * it on. With no such PR, or PRs in a repository of that name on more than one host, nothing.
 */
export function sessionReferenceContext(
  repository: string | null | undefined,
  pullRequests: readonly SessionPullRequest[],
): PullRequestReferenceContext | null {
  const wanted = repository?.trim().toLowerCase();
  if (!wanted) return null;
  const hosts = new Map<string, CodeHostPullRequest>();
  for (const pr of pullRequests) {
    const githubHosts = pr.host && (!pr.provider || pr.provider === "github") ? [pr.host] : [];
    const target = parseCodeHostPullRequestUrl(pr.url, githubHosts);
    if (target?.repository.toLowerCase() === wanted) {
      hosts.set(`${target.provider} ${target.host}`, target);
    }
  }
  if (hosts.size !== 1) return null;
  const [only] = hosts.values();
  return { provider: only!.provider, host: only!.host, repository: only!.repository };
}

/** A link to one of a PR's tabs or commits, such as `…/pull/12/files`, as the PR's own link. */
function pullRequestPageOf(href: string | null | undefined): string | null | undefined {
  return href?.replace(/^(https:\/\/[^/?#]+\/[^/?#]+\/[^/?#]+\/pull\/\d+)\/[^?#]*/, "$1");
}

function labelFor(target: CodeHostPullRequest, context: PullRequestReferenceContext): string {
  const here =
    target.provider === context.provider &&
    target.host === context.host &&
    target.repository.toLowerCase() === context.repository.toLowerCase();
  return here ? `#${target.number}` : fullLabel(target);
}

function fullLabel(target: CodeHostPullRequest): string {
  return `${target.repository}#${target.number}`;
}

/** The page's host, as a GitHub host, when the page is a GitHub PR, e.g. on an Enterprise host. */
function githubHostsOf(context: PullRequestReferenceContext): string[] {
  return context.provider === "github" ? [context.host] : [];
}

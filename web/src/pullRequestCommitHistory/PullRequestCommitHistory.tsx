// The new Commits page: a history header with its count and any limit, a search, and the
// commits as cards on a timeline. A card opens its commit's changes; ⌘-click or ⌘Enter opens
// them in a new tab, as the classic list does.

import { useCallback, useId, useMemo, useRef, useState } from "react";
import {
  CheckIcon,
  ChevronRightIcon,
  CopyIcon,
  EllipsisIcon,
  GitCommitHorizontalIcon,
  SearchIcon,
} from "lucide-react";
import { useRovingFocus } from "@/hooks/useRovingFocus";
import type { PendingFocus } from "@/hooks/usePendingFocus";
import { copyText } from "@/lib/clipboard";
import {
  commitAuthor,
  commitCiLabel,
  commitCiTone,
  shortSha,
  type PullRequestCommit,
  type PullRequestCommits,
} from "@/lib/pullRequestCommits";
import { absoluteTime, longRelativeTime } from "@/lib/relativeTime";
import { initials } from "@/pullRequestList/pullRequestListPageModel";
import { CheckGlyph } from "@/shell/PullRequestListRows";
import {
  type PullRequestOpenIntent,
  openByIntent,
  openGestureProps,
} from "@/shell/pullRequestOpenIntent";
import type { CommitFocusTarget } from "@/shell/pullRequestCommits/useCommitCommands";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";
import { settingCommand } from "@/store/paletteSettings";
import {
  commitHistoryHeading,
  committedTime,
  matchesCommitSearch,
} from "./pullRequestCommitHistoryModel";
import { useCopyFeedback } from "./useCopyFeedback";
import "./pullRequestCommitHistory.css";

const NO_COMMANDS: PaletteCommand[] = [];

export function PullRequestCommitHistory({
  list,
  active,
  owner,
  onSelectCommit,
  onOpenCommitInNewTab,
  initialSha,
  focusRequest,
  onFocusRequestSettled,
  providerLabel,
  commitsUrl,
}: {
  list: PullRequestCommits;
  /** False while the page is hidden, so its ⌘K rows leave with it. */
  active: boolean;
  /** Names this page's ⌘K rows apart from another pull request's. */
  owner: string;
  onSelectCommit: (sha: string) => void;
  onOpenCommitInNewTab?: (sha: string, options: { background: boolean }) => void;
  /** The commit that holds the tab stop at first, such as the one just left. */
  initialSha: string | undefined;
  focusRequest: PendingFocus<CommitFocusTarget> | null;
  onFocusRequestSettled: () => void;
  /** The PR's host, as the reader knows it, such as "GitHub Enterprise". */
  providerLabel: string;
  /** The PR's commits page on its host, which lists every commit. */
  commitsUrl: string | null;
}) {
  const commits = list.commits;
  const heading = commitHistoryHeading(list);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const searchButton = useRef<HTMLButtonElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const searchId = useId();
  const visible = useMemo(
    () => (query.trim() ? commits.filter((commit) => matchesCommitSearch(commit, query)) : commits),
    [commits, query],
  );
  const initialIndex = Math.max(
    0,
    visible.findIndex((commit) => commit.sha === initialSha),
  );
  const { itemProps } = useRovingFocus(visible.length, {
    initialIndex,
    focusRequest,
    onFocusRequestSettled,
  });
  // The cards whose description is open, as the "…" beside each title opens it.
  const [openBodies, setOpenBodies] = useState<ReadonlySet<string>>(() => new Set());
  const toggleBody = useCallback(
    (sha: string) =>
      setOpenBodies((current) => {
        const next = new Set(current);
        if (!next.delete(sha)) next.add(sha);
        return next;
      }),
    [],
  );
  const withBodies = useMemo(() => commits.filter((commit) => commit.body), [commits]);
  const allBodiesOpen =
    withBodies.length > 0 && withBodies.every((commit) => openBodies.has(commit.sha));
  const { stateOf, copy } = useCopyFeedback<string>();
  const copySha = useCallback((sha: string) => copy(() => copyText(sha), sha), [copy]);
  const openSearch = useCallback(() => {
    setSearchOpen(true);
    // The field mounts with this render; focus it once it has.
    requestAnimationFrame(() => searchInput.current?.focus());
  }, []);
  const closeSearch = () => {
    setQuery("");
    setSearchOpen(false);
    searchButton.current?.focus({ preventScroll: true });
  };
  const open = (commit: PullRequestCommit, intent: PullRequestOpenIntent) =>
    openByIntent(
      intent,
      () => onSelectCommit(commit.sha),
      onOpenCommitInNewTab && ((options) => onOpenCommitInNewTab(commit.sha, options)),
    );

  // The page's own actions in ⌘K: finding a commit, and copying any commit's SHA.
  const commands = useMemo<PaletteCommand[]>(
    () =>
      commits.length === 0
        ? []
        : [
            {
              id: "commits.find",
              label: "Find commits",
              icon: SearchIcon,
              keywords: ["search", "commits", "history", "sha"],
              movesFocus: true,
              run: openSearch,
            },
            ...(withBodies.length === 0
              ? []
              : [
                  // The card's own "…", for one commit from the keyboard.
                  settingCommand({
                    id: "commits.toggle-description",
                    owner,
                    label: "Show or hide a commit's description",
                    icon: EllipsisIcon,
                    keywords: ["commits", "message", "description", "body", "expand", "collapse"],
                    current: "",
                    choices: withBodies.map((commit) => ({
                      value: commit.sha,
                      label: `${openBodies.has(commit.sha) ? "Hide" : "Show"} ${shortSha(commit.sha)} ${commit.subject || "(no message)"}`,
                    })),
                    choose: toggleBody,
                  }),
                  {
                    id: "commits.toggle-descriptions",
                    label: allBodiesOpen ? "Hide commit descriptions" : "Show commit descriptions",
                    icon: EllipsisIcon,
                    keywords: ["commits", "message", "description", "body", "expand", "collapse"],
                    run: () =>
                      setOpenBodies(
                        allBodiesOpen ? new Set() : new Set(withBodies.map((commit) => commit.sha)),
                      ),
                  },
                ]),
            settingCommand({
              id: "commits.copy-sha",
              owner,
              label: "Copy a commit's SHA",
              icon: CopyIcon,
              keywords: ["commits", "sha", "hash", "clipboard"],
              current: "",
              choices: commits.map((commit) => ({
                value: commit.sha,
                label: `${shortSha(commit.sha)} ${commit.subject || "(no message)"}`,
              })),
              choose: copySha,
            }),
          ],
    [allBodiesOpen, commits, copySha, openBodies, openSearch, owner, toggleBody, withBodies],
  );
  const paletteInstance = useId();
  usePaletteCommands(
    `pull-request-commits:${owner}:${paletteInstance}`,
    "Commits",
    active ? commands : NO_COMMANDS,
  );

  if (commits.length === 0) {
    return (
      <div className="commit-history">
        <div className="commit-empty">
          <GitCommitHorizontalIcon aria-hidden />
          <strong>No commits</strong>
          <span>This pull request has no commits yet.</span>
        </div>
      </div>
    );
  }
  return (
    <div className="commit-history">
      {/* The header and the search stick as one, so a wrapped header never covers the field. */}
      <div className="commit-history-top">
        <header className="commit-list-heading">
          <div>
            <span className="commit-list-eyebrow">History</span>
            <h2>{heading.title}</h2>
          </div>
          <div className="commit-list-heading-actions">
            {heading.note && (
              <span>
                {heading.note}
                {commitsUrl && (
                  <>
                    {" · "}
                    <a href={commitsUrl} target="_blank" rel="noreferrer">
                      See all on {providerLabel}
                    </a>
                  </>
                )}
              </span>
            )}
            <button
              ref={searchButton}
              type="button"
              className="commit-search-button"
              aria-label="Find commits"
              aria-expanded={searchOpen}
              aria-controls={searchOpen ? searchId : undefined}
              onClick={() => (searchOpen ? closeSearch() : openSearch())}
            >
              <SearchIcon aria-hidden />
            </button>
          </div>
        </header>
        {searchOpen && (
          <div className="commit-search" id={searchId}>
            <SearchIcon aria-hidden />
            <input
              ref={searchInput}
              type="search"
              placeholder="Find commits"
              aria-label="Find commits"
              value={query}
              spellCheck={false}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key !== "Escape") return;
                event.preventDefault();
                event.stopPropagation();
                closeSearch();
              }}
            />
            {query && (
              <span role="status">
                {visible.length} of {commits.length}
              </span>
            )}
          </div>
        )}
      </div>
      <ol className="commit-list" aria-label="Commits">
        {visible.map((commit, index) => (
          <CommitCard
            key={commit.sha}
            commit={commit}
            roving={itemProps(index)}
            onOpen={(intent) => open(commit, intent)}
            copyState={stateOf(commit.sha)}
            onCopy={() => copySha(commit.sha)}
            bodyOpen={openBodies.has(commit.sha)}
            onToggleBody={() => toggleBody(commit.sha)}
          />
        ))}
      </ol>
      {visible.length === 0 && (
        <div className="commit-search-empty" role="status">
          <strong>No Matching Commits</strong>
          <span>Try a title, author, or commit SHA.</span>
        </div>
      )}
    </div>
  );
}

function CommitCard({
  commit,
  roving,
  onOpen,
  copyState,
  onCopy,
  bodyOpen,
  onToggleBody,
}: {
  commit: PullRequestCommit;
  /** The list's arrow-key focus: this card's tab stop, keys and element. */
  roving: ReturnType<ReturnType<typeof useRovingFocus>["itemProps"]>;
  onOpen: (intent: PullRequestOpenIntent) => void;
  copyState: "idle" | "copied" | "failed";
  onCopy: () => void;
  /** The description shows under the card; only a commit with one has the "…". */
  bodyOpen: boolean;
  onToggleBody: () => void;
}) {
  const bodyId = useId();
  const author = commitAuthor(commit);
  const time = committedTime(commit);
  const tone = commitCiTone(commit.ci_state);
  const ciLabel = `CI: ${commitCiLabel(tone)}`;
  const subject = commit.subject || "(no message)";
  const copyLabel =
    copyState === "copied"
      ? "Commit SHA copied"
      : copyState === "failed"
        ? "Couldn’t copy the commit SHA. Try again."
        : `Copy commit SHA ${commit.sha}`;
  // A click on the SHA or the chevron opens the commit too, as the card's main button does.
  const { onClick, onMouseDown, onAuxClick } = openGestureProps(onOpen);
  const openFromMouse = { onClick, onMouseDown, onAuxClick };
  return (
    <li>
      <span className="commit-rail" aria-hidden>
        <span className="commit-rail-line before" />
        <span className="commit-node" />
        <span className="commit-rail-line after" />
      </span>
      <div className={commit.body ? "commit-row has-body" : "commit-row"}>
        <button
          type="button"
          className="commit-row-main"
          {...roving}
          {...openGestureProps(onOpen, roving.onKeyDown)}
          aria-label={`${subject}, ${author ?? "Unknown author"}, ${shortSha(commit.sha)}`}
        >
          <span className="commit-avatar" aria-hidden>
            {initials(author ?? "?")}
          </span>
          <span className="commit-copy">
            <strong>{subject}</strong>
            <span>
              <b>{author ?? "Unknown author"}</b>
              {time !== null && (
                <>
                  {" committed "}
                  <time dateTime={new Date(time).toISOString()} title={absoluteTime(time)}>
                    {longRelativeTime(time)}
                  </time>
                </>
              )}
            </span>
          </span>
        </button>
        {commit.body && (
          <button
            type="button"
            className="commit-body-toggle"
            aria-label={bodyOpen ? "Hide description" : "Show description"}
            title={bodyOpen ? "Hide description" : "Show description"}
            aria-expanded={bodyOpen}
            aria-controls={bodyOpen ? bodyId : undefined}
            // The card is the list's one tab stop; ⌘K toggles any commit's description.
            tabIndex={-1}
            onClick={onToggleBody}
          >
            <EllipsisIcon aria-hidden />
          </button>
        )}
        <span className={`commit-ci ${tone}`} role="img" aria-label={ciLabel} title={ciLabel}>
          {tone === "none" ? <CheckGlyph tone="skipped" /> : <CheckGlyph tone={tone} />}
        </span>
        <span className="commit-row-sha" aria-hidden {...openFromMouse}>
          <code>{shortSha(commit.sha)}</code>
        </span>
        <button
          type="button"
          className={`commit-sha-copy ${copyState}`}
          aria-label={copyLabel}
          title={copyLabel}
          // The card is the list's one tab stop; ⌘K copies any commit's SHA from the keyboard.
          tabIndex={-1}
          onClick={onCopy}
        >
          {copyState === "copied" ? <CheckIcon aria-hidden /> : <CopyIcon aria-hidden />}
        </button>
        <span className="commit-row-open" aria-hidden {...openFromMouse}>
          <ChevronRightIcon />
        </span>
        {commit.body && bodyOpen && (
          <p id={bodyId} className="commit-row-body" data-testid="commit-row-body">
            {commit.body}
          </p>
        )}
      </div>
    </li>
  );
}

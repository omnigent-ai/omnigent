// The label commands a PR offers: Edit Labels…, which opens its picker, and on the open PR an
// Add Label row in ⌘K for each label this user added recently in its repository.

import { TagIcon } from "lucide-react";
import { useId, useMemo, useRef } from "react";

import type { PullRequestLabel } from "@/newPullRequestPage/hooks/usePullRequests";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { usePullRequestEditsServed } from "@/newPullRequestPage/hooks/pullRequestEditOffers";
import { useRecentLabels } from "@/lib/recentLabels";
import type { RepositoryLabel } from "@/lib/repositoryLabelCache";
import { requestPullRequestControl } from "@/pullRequestPage/controlRequests";
import { useControlPlace } from "@/pullRequestPage/pullRequestDestination";
import {
  AVAILABLE,
  type CommandAvailability,
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommands,
} from "@/store/paletteCommands";

import {
  LABEL_CHANGE_WAITING_REASON,
  labelKey,
  usePullRequestLabelChanges,
} from "./pullRequestLabelChanges";

export const EDIT_LABELS_ACTION_ID = "edit-labels";
export const EDIT_LABELS_LABEL = "Edit Labels…";
export const LABEL_COMMAND_ICON = TagIcon;
export const LABEL_COMMAND_KEYWORDS: readonly string[] = [
  "label",
  "labels",
  "tag",
  "pull request",
  "pr",
];

/** Edit Labels…, naming the PR where ⌘K lists more than one. */
export function editLabelsPaletteLabel(name: string | null): string {
  return name ? `Edit Labels on ${name}…` : EDIT_LABELS_LABEL;
}

/** Edit Labels… waits, as the picker's own trigger does, while a label change is still going. */
export function editLabelsAvailability(waiting: boolean): CommandAvailability {
  return waiting ? unavailableBecause(LABEL_CHANGE_WAITING_REASON) : AVAILABLE;
}

export interface AddLabelCommand {
  id: string;
  label: RepositoryLabel;
  paletteLabel: string;
  keywords: string[];
  availability: CommandAvailability;
}

/**
 * One Add Label row for each recent label the PR doesn't have once its unanswered changes land,
 * most recent first.
 */
export function addRecentLabelCommands({
  recent,
  applied,
  waiting,
}: {
  recent: readonly RepositoryLabel[];
  /** The PR's labels once every unanswered change lands, by {@link labelKey}. */
  applied: ReadonlySet<string>;
  /** A label change from this Mac is still going; another can't start yet. */
  waiting: boolean;
}): AddLabelCommand[] {
  return recent
    .filter((label) => !applied.has(labelKey(label)))
    .map((label) => ({
      id: `add-label:${labelKey(label)}`,
      label,
      paletteLabel: `Add Label: ${label.name}`,
      keywords: [
        ...LABEL_COMMAND_KEYWORDS,
        "add",
        "recent",
        label.name,
        ...(label.description ? [label.description] : []),
      ],
      availability: waiting ? unavailableBecause(LABEL_CHANGE_WAITING_REASON) : AVAILABLE,
    }));
}

const NO_COMMANDS: PaletteCommand[] = [];

/**
 * ⌘K's label rows for the open PR at `prUrl`, sent as `conversationId`'s: Edit Labels…, which
 * `openPicker` opens, and an Add Label row for each recent label. Its Labels section shows how
 * each change went. `prUrl` null offers none; labels not yet read offer no Add Label rows.
 */
export function usePullRequestLabelPaletteCommands({
  conversationId,
  prUrl,
  labels,
  openPicker,
}: {
  conversationId: string;
  prUrl: string | null;
  labels: readonly PullRequestLabel[] | undefined;
  openPicker: () => void;
}): PaletteCommand[] {
  const { applied, waiting, send } = usePullRequestLabelChanges(conversationId, prUrl, labels);
  const scope = useMemo(() => {
    const target = prUrl ? parseCodeHostPullRequestUrl(prUrl) : null;
    return target ? { host: target.host, repository: target.repository } : null;
  }, [prUrl]);
  const recent = useRecentLabels(scope);
  // The rows keep their identity across renders; running one reads the latest send and picker.
  const latest = useRef({ send, openPicker });
  latest.current = { send, openPicker };
  const known = labels !== undefined;
  return useMemo(() => {
    if (!prUrl) return NO_COMMANDS;
    const commands: PaletteCommand[] = [
      {
        id: `${EDIT_LABELS_ACTION_ID}:${prUrl}`,
        label: EDIT_LABELS_LABEL,
        icon: LABEL_COMMAND_ICON,
        keywords: [...LABEL_COMMAND_KEYWORDS],
        availability: editLabelsAvailability(waiting),
        movesFocus: true,
        run: () => latest.current.openPicker(),
      },
    ];
    if (known) {
      for (const command of addRecentLabelCommands({ recent, applied, waiting })) {
        commands.push({
          id: `${command.id}:${prUrl}`,
          label: command.paletteLabel,
          icon: LABEL_COMMAND_ICON,
          keywords: command.keywords,
          availability: command.availability,
          run: () => latest.current.send({ prUrl, add: [command.label], remove: [] }),
        });
      }
    }
    return commands;
  }, [prUrl, known, recent, applied, waiting]);
}

/**
 * The open PR's label rows in ⌘K while its view is shown and its labels can be edited here. Edit
 * Labels… opens the Labels picker as a list row's does, bringing its Summary tab forward.
 */
export function PullRequestLabelPaletteCommands({
  conversationId,
  prUrl,
  labels,
  editable,
  active,
}: {
  conversationId: string;
  prUrl: string | null;
  labels: readonly PullRequestLabel[] | undefined;
  editable: boolean;
  active: boolean;
}): null {
  const place = useControlPlace();
  // A host that can't list or change labels gets no label rows, which could only fail.
  const served = usePullRequestEditsServed(conversationId).labels;
  const offered = active && editable && served ? prUrl : null;
  const commands = usePullRequestLabelPaletteCommands({
    conversationId,
    prUrl: offered,
    labels,
    openPicker: () => {
      if (offered) requestPullRequestControl(offered, "labels", place, null);
    },
  });
  const instance = useId();
  usePaletteCommands(`pull-request-labels:${instance}`, "Pull request", commands);
  return null;
}

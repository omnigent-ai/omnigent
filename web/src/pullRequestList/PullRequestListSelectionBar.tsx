// The bar a list shows while rows are selected: how many, the selection's actions, More, and the
// way out. It fits the list's width (see `selectionBarFit.ts`): actions shorten, then move into
// More from the end, so nothing clips, and each keeps its full label in its tooltip and name.

import { type ReactNode, type RefObject, useId, useMemo, useRef, useState } from "react";
import { EllipsisIcon, XIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useMenuCloseHandOff } from "@/hooks/useMenuCloseHandOff";
import type { ListSelectionAction } from "./listSelectionActions";
import { RemovalBusyFace } from "./RemovalBusyFace";
import { STOP_REMOVING_LABEL } from "./removalProgress";
import {
  SelectionBarSizers as SizerRow,
  SizerText,
} from "@/components/selectionBar/SelectionBarSizers";
import { useSelectionBarFit } from "@/components/selectionBar/selectionBarFit";
import { type HeldMenuItem, useHeldMenuItems } from "@/components/selectionBar/useHeldMenuItems";
import { DescribedMenuItem } from "@/pullRequestPage/describedMenuItems";

export interface PullRequestListSelectionBarProps {
  selectedCount: number;
  /** How many Select All would select; null while every row shown is selected. */
  selectAllCount: number | null;
  /** The list hasn't loaded every row its search found: Select All takes the loaded ones. */
  loadedOnly?: boolean;
  onSelectAll: () => void;
  /** The selection's own buttons, in the bar's order; the last folds into More first. */
  actions: readonly ListSelectionAction[];
  /** What More always lists, a group at a time, after any action that folded. */
  moreGroups: readonly (readonly ListSelectionAction[])[];
  /**
   * Remove from this session, where the list manages session links. `share` is set when the
   * selection holds rows that removal leaves alone, so it acts on `acting` of its `of` rows.
   */
  remove: {
    reason: string | undefined;
    keys: string;
    /**
     * Where it removes them from, e.g. "this session and its agents", and in title case;
     * `detail` names every session it unlinks them from and every one still linking them.
     */
    place?: { text: string; title: string; detail?: string | null };
    share?: { acting: number; of: number };
    /** Its label while the selection is being removed, such as "Removing 3 of 8". */
    busy?: string;
    /** Stops the removal under way, where it can be stopped now. */
    stop?: () => void;
    run: () => void;
  } | null;
  onClear: () => void;
}

export function PullRequestListSelectionBar({
  selectedCount,
  selectAllCount,
  loadedOnly = false,
  onSelectAll,
  actions,
  moreGroups,
  remove,
  onClear,
}: PullRequestListSelectionBarProps) {
  const bar = useRef<HTMLDivElement>(null);
  const more = useRef<HTMLButtonElement>(null);
  const removeReasonId = useId();
  const stoppable = !!remove?.busy && !!remove.stop;
  const staticMore = moreGroups.some((group) => group.length > 0);
  const [moreOpen, setMoreOpen] = useState(false);
  const actionIds = useMemo(() => actions.map((action) => action.id), [actions]);
  const fit = useSelectionBarFit({ bar, more, staticMore, moreOpen, actionIds });
  // By id: an action that joins or leaves the selection moves no other one in or out of More.
  const shown = actions.filter((action) => !fit.folded.has(action.id));
  const foldedActions = actions.filter((action) => fit.folded.has(action.id));
  const selectAllLabel = `Select All ${selectAllCount}${loadedOnly ? " Loaded" : ""}`;
  return (
    <div
      ref={bar}
      className="pr-list-selection-bar"
      role="toolbar"
      aria-label="Selected pull requests"
    >
      <span className="pr-list-selection-count">
        <strong>{selectedCount}</strong>
        {fit.count === "full" ? (
          " selected"
        ) : (
          <span className="pr-list-visually-hidden"> selected</span>
        )}
      </span>
      {selectAllCount !== null && (
        <button
          type="button"
          aria-label={fit.selectAll === "short" ? selectAllLabel : undefined}
          title={fit.selectAll === "short" ? selectAllLabel : undefined}
          onClick={onSelectAll}
        >
          {fit.selectAll === "short" ? `All ${selectAllCount}` : selectAllLabel}
        </button>
      )}
      <span className="pr-list-selection-divider" data-bar-part="fixed" aria-hidden />
      {shown.map((action) => (
        <SelectionActionButton key={action.id} action={action} compact={fit.compact} />
      ))}
      <SelectionMoreMenu
        trigger={more}
        actions={actions}
        folded={foldedActions}
        groups={moreGroups}
        onOpenChange={setMoreOpen}
        onEmptyClose={() => bar.current?.querySelector<HTMLElement>("[data-bar-clear]")?.focus()}
      />
      {remove && (
        <button
          type="button"
          className="pr-list-selection-remove"
          data-bar-part="fixed"
          // It stays focusable where it can't remove, so its reason can be read from the keyboard.
          aria-label={stoppable ? STOP_REMOVING_LABEL : undefined}
          aria-disabled={remove.reason && !stoppable ? true : undefined}
          aria-describedby={remove.reason ? removeReasonId : undefined}
          aria-busy={remove.busy ? true : undefined}
          data-stoppable={stoppable ? true : undefined}
          title={
            stoppable
              ? STOP_REMOVING_LABEL
              : (remove.reason ??
                [
                  remove.share
                    ? `Remove from ${remove.place?.title ?? "This Session"} (${remove.share.acting} of ${remove.share.of})`
                    : `Remove from ${remove.place?.text ?? "this session"} (${remove.keys})`,
                  remove.place?.detail,
                ]
                  .filter(Boolean)
                  .join("\n"))
          }
          onClick={stoppable ? remove.stop : remove.reason ? undefined : remove.run}
        >
          {remove.busy ? <RemovalBusyFace label={remove.busy} stoppable={stoppable} /> : "Remove"}
          {!remove.busy && remove.share && (
            <span className="pr-list-selection-share">
              ({remove.share.acting} of {remove.share.of})
            </span>
          )}
          <kbd aria-hidden>{remove.keys}</kbd>
        </button>
      )}
      {remove?.reason && (
        // Beside the button, so it describes Remove without joining its name.
        <span id={removeReasonId} className="sr-only">
          {remove.reason}
        </span>
      )}
      <button
        type="button"
        className="pr-list-selection-clear"
        data-bar-part="fixed"
        data-bar-clear=""
        aria-label="Clear Selection"
        title="Clear Selection (Esc)"
        onClick={onClear}
      >
        <XIcon aria-hidden />
      </button>
      <SelectionBarSizers
        selectedCount={selectedCount}
        selectAllCount={selectAllCount}
        actions={actions}
      />
    </div>
  );
}

/** How many of the selection a shortened action acts on, where it isn't all of them. */
const shareOf = ({ fits }: ListSelectionAction) => (fits && fits.acting < fits.of ? fits : null);

/** What a compact action button shows: its short label, and the share it acts on as a badge. */
function CompactLabel({ action }: { action: ListSelectionAction }): ReactNode {
  if (!action.shortLabel) return action.label;
  const share = shareOf(action);
  return (
    <>
      {action.shortLabel}
      {share && (
        <span className="pr-list-selection-share">
          {share.acting}/{share.of}
        </span>
      )}
    </>
  );
}

/**
 * One of the selection's actions in its bar. Where it can't act now it stays focusable and
 * hoverable, so its reason can be read, and a click does nothing.
 */
function SelectionActionButton({
  action,
  compact,
}: {
  action: ListSelectionAction;
  compact: boolean;
}) {
  const Icon = action.icon;
  const reason = action.unavailableReason;
  const shortened = compact && !!action.shortLabel;
  return (
    <button
      type="button"
      className="pr-list-selection-action"
      data-bar-action={action.id}
      aria-label={shortened ? action.label : undefined}
      aria-disabled={reason ? true : undefined}
      title={reason ?? (shortened ? action.label : undefined)}
      onClick={reason ? undefined : action.run}
    >
      <Icon aria-hidden />
      {shortened ? <CompactLabel action={action} /> : action.label}
    </button>
  );
}

/**
 * The selection bar's More menu: the actions that folded, in the bar's order, then what it
 * always offers, a group at a time. While it's open, nothing it lists leaves it, so the item
 * with focus stays; each still acts as the selection's current definition says.
 */
function SelectionMoreMenu({
  trigger,
  actions,
  folded,
  groups,
  onOpenChange,
  onEmptyClose,
}: {
  trigger: RefObject<HTMLButtonElement | null>;
  actions: readonly ListSelectionAction[];
  folded: readonly ListSelectionAction[];
  groups: readonly (readonly ListSelectionAction[])[];
  onOpenChange: (open: boolean) => void;
  onEmptyClose: () => void;
}) {
  const menu = useMenuCloseHandOff();
  const held = useHeldMenuItems(actions, folded);
  const foldedGroup = held.items;
  const staticGroups = groups.filter((group) => group.length > 0);
  const open = held.open;
  if (!open && folded.length === 0 && staticGroups.length === 0) return null;
  const shownGroups: readonly (readonly (ListSelectionAction | HeldMenuItem)[])[] = [
    foldedGroup,
    ...staticGroups,
  ].filter((group) => group.length > 0);
  return (
    <DropdownMenu
      onOpenChange={(opening) => {
        held.onOpenChange(opening);
        onOpenChange(opening);
      }}
    >
      <DropdownMenuTrigger asChild>
        <button ref={trigger} type="button" aria-label="More Actions" title="More Actions">
          <EllipsisIcon aria-hidden />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        side="top"
        align="end"
        onCloseAutoFocus={(event) => {
          menu.onCloseAutoFocus(event);
          // With nothing left to list, the trigger goes as the menu closes: focus stays in the bar.
          if (!event.defaultPrevented && folded.length === 0 && staticGroups.length === 0) {
            event.preventDefault();
            onEmptyClose();
          }
        }}
      >
        {shownGroups.flatMap((group, index) => [
          ...(index > 0 ? [<DropdownMenuSeparator key={`before-${group[0]!.id}`} />] : []),
          ...group.map((item) => {
            const Icon = item.icon;
            if (!("run" in item)) {
              return (
                <DropdownMenuItem key={item.id} disabled>
                  <Icon aria-hidden />
                  {item.label}
                </DropdownMenuItem>
              );
            }
            const action = item;
            return (
              <DescribedMenuItem
                key={action.id}
                disabled={!!action.unavailableReason}
                description={action.unavailableReason}
                onSelect={() =>
                  // At once, so an open stays within the click; a dialog once the menu hands focus back.
                  action.movesFocus
                    ? menu.runAfterClose(action.run, { afterFocusReturns: true })
                    : action.run()
                }
              >
                <Icon aria-hidden />
                {action.label}
              </DescribedMenuItem>
            );
          }),
        ])}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** A compact action's sizer: its short label and badge, as `CompactLabel` draws them. */
function CompactSizer({ action }: { action: ListSelectionAction }) {
  if (!action.shortLabel) return <SizerText text={action.label} />;
  const share = shareOf(action);
  return (
    <>
      <SizerText text={action.shortLabel} />
      {share && (
        <span className="pr-list-selection-share">
          <SizerText text={`${share.acting}/${share.of}`} />
        </span>
      )}
    </>
  );
}

/**
 * Copies of the bar's parts at each form, for the fit to measure: never interactive, out of the
 * tab order and the accessibility tree, with no text of their own.
 */
function SelectionBarSizers({
  selectedCount,
  selectAllCount,
  actions,
}: {
  selectedCount: number;
  selectAllCount: number | null;
  actions: readonly ListSelectionAction[];
}) {
  return (
    <SizerRow>
      <span className="pr-list-selection-count" data-bar-size="count-full">
        <SizerText text={String(selectedCount)} className="pr-list-sizer-strong" />
        <SizerText text=" selected" />
      </span>
      <span className="pr-list-selection-count" data-bar-size="count-number">
        <SizerText text={String(selectedCount)} className="pr-list-sizer-strong" />
      </span>
      {selectAllCount !== null && (
        <>
          <span className="pr-list-selection-control" data-bar-size="select-all-full">
            <SizerText text={`Select All ${selectAllCount}`} />
          </span>
          <span className="pr-list-selection-control" data-bar-size="select-all-short">
            <SizerText text={`All ${selectAllCount}`} />
          </span>
        </>
      )}
      <span className="pr-list-selection-control" data-bar-size="more">
        <EllipsisIcon aria-hidden />
      </span>
      {actions.map((action) => {
        const Icon = action.icon;
        return (
          <span key={action.id} data-bar-action-size={action.id}>
            <span className="pr-list-selection-control pr-list-selection-action" data-form="full">
              <Icon aria-hidden />
              <SizerText text={action.label} />
            </span>
            <span
              className="pr-list-selection-control pr-list-selection-action"
              data-form="compact"
            >
              <Icon aria-hidden />
              <CompactSizer action={action} />
            </span>
          </span>
        );
      })}
    </SizerRow>
  );
}

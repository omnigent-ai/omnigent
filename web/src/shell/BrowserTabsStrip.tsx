import { GlobeIcon, PencilIcon, RotateCcwIcon, XIcon } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent, type Ref } from "react";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { SuppressBrowserView } from "@/hooks/useSuppressBrowserView";
import {
  BROWSER_TAB_NAME_MAX_CHARS,
  browserTabLabel,
  browserTabNameSeed,
} from "@/lib/browserTabLabel";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { cn } from "@/lib/utils";

export interface BrowserTabsStripProps {
  tabs: readonly string[];
  /** Null unless the Browser rail mode is active. */
  activeTabId: string | null;
  names: Readonly<Record<string, string>>;
  /** Absent until the tab loads a page. */
  urls: Readonly<Record<string, string>>;
  activeTabRef: Ref<HTMLDivElement>;
  onSelect: (tabId: string) => void;
  onClose: (tabId: string) => void;
  /** Save a manual name; an empty string clears it. */
  onRename: (tabId: string, name: string) => void;
}

/** Browser soft tabs in the Workspace rail, named after their page or a manual name. */
export function BrowserTabsStrip({
  tabs,
  activeTabId,
  names,
  urls,
  activeTabRef,
  onSelect,
  onClose,
  onRename,
}: BrowserTabsStripProps) {
  return (
    <>
      {tabs.map((tabId) => (
        <BrowserTab
          key={tabId}
          active={activeTabId === tabId}
          manualName={names[tabId] ?? null}
          url={urls[tabId] ?? null}
          activeTabRef={activeTabRef}
          onSelect={() => onSelect(tabId)}
          onClose={() => onClose(tabId)}
          onRename={(name) => onRename(tabId, name)}
        />
      ))}
    </>
  );
}

interface BrowserTabProps {
  active: boolean;
  manualName: string | null;
  url: string | null;
  activeTabRef: Ref<HTMLDivElement>;
  onSelect: () => void;
  onClose: () => void;
  onRename: (name: string) => void;
}

function BrowserTab({
  active,
  manualName,
  url,
  activeTabRef,
  onSelect,
  onClose,
  onRename,
}: BrowserTabProps) {
  // The field's start value while renaming, else null.
  const [editSeed, setEditSeed] = useState<string | null>(null);
  const editing = editSeed !== null;
  // Rename starts once the menu has closed; the closing menu would steal focus.
  const renameFromMenuRef = useRef(false);
  const tabButtonRef = useRef<HTMLButtonElement>(null);
  // Enter or Escape hands focus back to the tab; a blur leaves it where it went.
  const refocusTabRef = useRef(false);
  useEffect(() => {
    if (editing || !refocusTabRef.current) return;
    refocusTabRef.current = false;
    tabButtonRef.current?.focus();
  }, [editing]);

  const finishEditing = (byKeyboard: boolean) => {
    refocusTabRef.current = byKeyboard;
    setEditSeed(null);
  };
  const label = browserTabLabel(manualName, url);
  const startEditing = () => setEditSeed(browserTabNameSeed(label));

  const commitRename = (value: string, byKeyboard: boolean) => {
    const seed = editSeed ?? "";
    finishEditing(byKeyboard);
    const trimmed = value.trim();
    // Saving the untouched field must not pin a URL label, even after a navigation.
    if (trimmed === seed.trim() || trimmed === (manualName ?? "")) return;
    onRename(trimmed);
  };

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild disabled={editing}>
        <div
          ref={active ? activeTabRef : null}
          title={url ?? label}
          className={cn(
            "flex h-[24px] shrink-0 items-center gap-[6px] rounded-md px-2 text-ui font-medium leading-5 transition-colors",
            active
              ? "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground"
              : "text-muted-foreground hover:bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] hover:text-foreground",
          )}
        >
          {editing ? (
            <span className="flex items-center gap-1">
              <GlobeIcon className="size-4 shrink-0" />
              <BrowserTabNameInput
                initialValue={editSeed}
                onCommit={commitRename}
                onCancel={() => finishEditing(true)}
              />
            </span>
          ) : (
            <button
              ref={tabButtonRef}
              type="button"
              role="tab"
              aria-selected={active}
              className="flex min-w-0 items-center gap-1"
              onAuxClick={(event) => {
                if (event.button === 1) {
                  event.preventDefault();
                  onClose();
                }
              }}
              onClick={onSelect}
              onDoubleClick={(event) => {
                event.preventDefault();
                startEditing();
              }}
              onKeyDown={(event) => {
                if (event.key === "F2") {
                  event.preventDefault();
                  startEditing();
                }
              }}
            >
              <GlobeIcon className="size-4 shrink-0" />
              <span className="max-w-56 truncate">{label}</span>
            </button>
          )}
          <button
            type="button"
            aria-label={`Close ${label}`}
            className="flex size-4 shrink-0 items-center justify-center rounded hover:bg-muted"
            onClick={onClose}
          >
            <XIcon className="size-3" />
          </button>
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent
        className="min-w-36"
        onCloseAutoFocus={(event) => {
          if (!renameFromMenuRef.current) return;
          renameFromMenuRef.current = false;
          event.preventDefault();
          startEditing();
        }}
      >
        <SuppressBrowserView />
        <ContextMenuItem
          onSelect={() => {
            renameFromMenuRef.current = true;
          }}
        >
          <PencilIcon />
          Rename
        </ContextMenuItem>
        {manualName !== null && (
          <ContextMenuItem onSelect={() => onRename("")}>
            <RotateCcwIcon />
            Reset name
          </ContextMenuItem>
        )}
      </ContextMenuContent>
    </ContextMenu>
  );
}

/** Inline tab-name field: Enter or blur saves, Escape cancels. */
function BrowserTabNameInput({
  initialValue,
  onCommit,
  onCancel,
}: {
  initialValue: string;
  onCommit: (value: string, byKeyboard: boolean) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initialValue);
  const inputRef = useRef<HTMLInputElement>(null);
  const settledRef = useRef(false);
  const composingRef = useRef(false);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const settle = (commit: boolean, byKeyboard: boolean) => {
    if (settledRef.current) return;
    settledRef.current = true;
    if (commit) onCommit(value, byKeyboard);
    else onCancel();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    // The field owns every key, so rail shortcuts such as 1-9 must not act on typing.
    event.stopPropagation();
    if (isImeCompositionKeyEvent(event, composingRef.current)) return;
    if (event.key !== "Enter" && event.key !== "Escape") return;
    event.preventDefault();
    settle(event.key === "Enter", true);
  };

  return (
    <input
      ref={inputRef}
      type="text"
      aria-label="Browser tab name"
      maxLength={BROWSER_TAB_NAME_MAX_CHARS}
      value={value}
      onChange={(event) => setValue(event.target.value)}
      onCompositionStart={() => {
        composingRef.current = true;
      }}
      onCompositionEnd={() => {
        composingRef.current = false;
      }}
      onKeyDown={handleKeyDown}
      onBlur={() => settle(true, false)}
      className="w-40 min-w-0 rounded bg-transparent px-0.5 text-ui font-medium outline-1 outline-ring"
    />
  );
}

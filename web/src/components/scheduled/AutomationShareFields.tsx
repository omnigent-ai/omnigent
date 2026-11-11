/**
 * Editor for who an automation's runs are shared with: one row per user or
 * group with its permission level, plus a row to add another. Controlled; the
 * caller saves the list with the automation.
 */

import { type FormEvent, useState } from "react";
import { Trash2Icon, UserPlusIcon } from "lucide-react";
import { AddUserField } from "@/components/AddUserField";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { LEVEL_EDIT, LEVEL_READ } from "@/lib/permissionsApi";
import type { ScheduledTaskShare } from "@/lib/scheduledTasksApi";

/** Mirrors the server's per-automation limit. */
export const MAX_AUTOMATION_SHARES = 10;

const LEVEL_LABELS: Record<number, string> = { [LEVEL_READ]: "Read", [LEVEL_EDIT]: "Edit" };

/** Whether two share lists grant the same levels to the same users, in any order. */
export function sameShares(
  first: readonly ScheduledTaskShare[],
  second: readonly ScheduledTaskShare[],
): boolean {
  const key = (shares: readonly ScheduledTaskShare[]) =>
    shares
      .map((share) => `${share.level}:${share.userId}`)
      .sort()
      .join("\n");
  return key(first) === key(second);
}

/**
 * Whether the server lets automations share runs. Off while the capability
 * probe loads, and on servers that predate the feature or disable sharing.
 */
export function useAutomationSharingAvailable(): boolean {
  const info = useServerInfo();
  if (info === "loading") return false;
  return info.scheduled_task_sharing === true && info.sharing_mode !== "off";
}

interface AutomationShareFieldsProps {
  /** Prefixes the field ids, so two editors on a page keep distinct labels. */
  idPrefix: string;
  value: ScheduledTaskShare[];
  onChange: (next: ScheduledTaskShare[]) => void;
  /** The owner already has every run, so they are refused as a grantee. */
  ownerId?: string | null;
  disabled?: boolean;
}

export function AutomationShareFields({
  idPrefix,
  value,
  onChange,
  ownerId,
  disabled = false,
}: AutomationShareFieldsProps) {
  const info = useServerInfo();
  const canShare = useAutomationSharingAvailable();
  const sharingMode = info === "loading" ? "on" : info.sharing_mode;
  const readOnly = sharingMode === "read_only" || sharingMode === "restricted_read_only";
  const [newUserId, setNewUserId] = useState("");
  const [newLevel, setNewLevel] = useState(String(LEVEL_READ));
  const [error, setError] = useState<string | null>(null);
  const full = value.length >= MAX_AUTOMATION_SHARES;

  function levelOptions(current: number) {
    const levels = readOnly ? [LEVEL_READ] : [LEVEL_READ, LEVEL_EDIT];
    // Keep a saved level the policy no longer offers visible, but not selectable.
    return levels.includes(current) ? levels : [...levels, current];
  }

  function handleAdd(event: FormEvent) {
    event.preventDefault();
    const userId = newUserId.trim();
    if (!userId) return;
    if (userId === ownerId) {
      setError("You already own every run.");
      return;
    }
    if (value.some((share) => share.userId === userId)) {
      setError(`Runs are already shared with ${userId}.`);
      return;
    }
    setError(null);
    onChange([...value, { userId, level: Number(newLevel) }]);
    setNewUserId("");
  }

  if (!canShare) {
    // The server refuses any saved list here, so the only change it accepts is none.
    return (
      <div className="flex min-w-0 flex-col gap-2" data-testid={`${idPrefix}-fields`}>
        <p className="text-sm text-muted-foreground">
          {value.length === 0
            ? "Runs won't be shared after you save."
            : "This server no longer shares automation runs, so runs skip these people and groups."}
        </p>
        {value.length > 0 && (
          <>
            <ul className="flex min-w-0 flex-col gap-1" data-testid={`${idPrefix}-list`}>
              {value.map((share) => (
                <li key={share.userId} className="flex min-w-0 items-center gap-2 text-ui">
                  <span className="min-w-0 flex-1 truncate">{share.userId}</span>
                  <span className="text-muted-foreground">
                    {LEVEL_LABELS[share.level] ?? `Level ${share.level}`}
                  </span>
                </li>
              ))}
            </ul>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="self-start"
              disabled={disabled}
              onClick={() => onChange([])}
              componentId="tasks.scheduled.share_clear"
            >
              Stop sharing runs
            </Button>
          </>
        )}
      </div>
    );
  }

  const editShared = value.some((share) => share.level >= LEVEL_EDIT);
  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid={`${idPrefix}-fields`}>
      <p className="text-sm text-muted-foreground">
        Each run&apos;s session is shared with these people or groups.
      </p>
      {value.length > 0 && (
        <ul className="flex min-w-0 flex-col gap-1" data-testid={`${idPrefix}-list`}>
          {value.map((share) => (
            <li key={share.userId} className="flex min-w-0 items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-ui">{share.userId}</span>
              <Select
                value={String(share.level)}
                onValueChange={(level) =>
                  onChange(
                    value.map((entry) =>
                      entry.userId === share.userId ? { ...entry, level: Number(level) } : entry,
                    ),
                  )
                }
                disabled={disabled}
                componentId="tasks.scheduled.share_level"
                valueHasNoPii
              >
                <SelectTrigger className="h-8 w-24" aria-label={`Permission for ${share.userId}`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper" align="start">
                  {levelOptions(share.level).map((level) => (
                    <SelectItem
                      key={level}
                      value={String(level)}
                      disabled={readOnly && level !== LEVEL_READ}
                    >
                      {LEVEL_LABELS[level] ?? `Level ${level}`}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={`Stop sharing runs with ${share.userId}`}
                disabled={disabled}
                onClick={() => onChange(value.filter((entry) => entry.userId !== share.userId))}
                componentId="tasks.scheduled.share_remove"
              >
                <Trash2Icon className="size-3.5" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={handleAdd} className="flex items-end gap-2">
        <div className="min-w-0 flex-1">
          <label htmlFor={`${idPrefix}-user`} className="text-sm font-medium text-muted-foreground">
            User or group
          </label>
          <AddUserField
            id={`${idPrefix}-user`}
            value={newUserId}
            onChange={(next) => {
              setNewUserId(next);
              setError(null);
            }}
            disabled={disabled || full}
            placeholder="alice@example.com or a group"
          />
        </div>
        <Select
          value={readOnly ? String(LEVEL_READ) : newLevel}
          onValueChange={setNewLevel}
          disabled={disabled || full}
          componentId="tasks.scheduled.share_new_level"
          valueHasNoPii
        >
          <SelectTrigger className="h-8 w-24" aria-label="Permission for new share">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" align="start">
            <SelectItem value={String(LEVEL_READ)}>Read</SelectItem>
            {!readOnly && <SelectItem value={String(LEVEL_EDIT)}>Edit</SelectItem>}
          </SelectContent>
        </Select>
        <Button
          type="submit"
          size="sm"
          variant="outline"
          disabled={disabled || full || !newUserId.trim()}
          componentId="tasks.scheduled.share_add"
        >
          <UserPlusIcon className="mr-1 size-3.5" />
          Add
        </Button>
      </form>
      {full && (
        <p className="text-sm text-muted-foreground">
          Runs can be shared with up to {MAX_AUTOMATION_SHARES} people or groups.
        </p>
      )}
      {editShared && (
        <p className="text-sm text-muted-foreground" data-testid={`${idPrefix}-edit-note`}>
          People with edit access can send messages to each run, and a run shared with edit access
          can&apos;t start or message other sessions.
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

// Key/value editor for user labels ("tags"), shared by the session info
// popover (each change PATCHes the session) and the project settings dialog
// (changes edit a draft saved with the dialog). Internal labels are hidden.
//
// It renders no <form>: the project dialog already wraps it in one, so Enter
// is handled on the inputs instead of by implicit form submission.

import { useImperativeHandle, useState, type KeyboardEvent, type Ref } from "react";
import { CheckIcon, PencilIcon, PlusIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { labelValidationError, userLabelEntries } from "@/lib/sessionLabels";

/** What {@link LabelEditorHandle.takePendingLabel} found in the add or edit row. */
export type PendingLabel =
  { status: "none" } | { status: "valid"; key: string; value: string } | { status: "invalid" };

/** Lets a parent that saves in bulk collect a label typed but not yet committed. */
export interface LabelEditorHandle {
  /**
   * Take the label in the add row or the edit in progress: a valid one is
   * cleared from its row for the caller to save, and an invalid one shows its
   * inline message.
   */
  takePendingLabel: () => PendingLabel;
}

interface LabelEditorProps {
  /** Every label on the target; internal keys are filtered out for display. */
  labels: Record<string, string> | null | undefined;
  /** Show labels without add / edit / remove controls. */
  readOnly?: boolean;
  /** Disable the controls, e.g. while a parent is loading or saving. */
  disabled?: boolean;
  /** Set one label. A rejected promise keeps the input and shows its message. */
  onSet: (key: string, value: string) => Promise<unknown> | void;
  /** Remove one label. A rejected promise shows its message. */
  onRemove: (key: string) => Promise<unknown> | void;
  /** Text shown when there are no user labels. */
  emptyText?: string;
  /** Prefix for the `data-testid`s inside the editor. */
  testIdPrefix?: string;
  /** Handle for collecting the add row's pending label. */
  handleRef?: Ref<LabelEditorHandle>;
  /** Short note shown after a label's value, e.g. where it came from. */
  rowNote?: (key: string, value: string) => string | null;
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** Run `handler` on Enter without submitting an enclosing form. */
function onEnter(handler: () => void) {
  return (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
    event.preventDefault();
    handler();
  };
}

function LabelRowNote({ note }: { note: string | null | undefined }) {
  if (!note) return null;
  return <span className="shrink-0 font-sans text-xs text-muted-foreground">{note}</span>;
}

export function LabelEditor({
  labels,
  readOnly = false,
  disabled = false,
  onSet,
  onRemove,
  emptyText = "No labels",
  testIdPrefix = "label-editor",
  handleRef,
  rowNote,
}: LabelEditorProps) {
  const entries = userLabelEntries(labels);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [adding, setAdding] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const controlsDisabled = disabled || pending;

  async function run(action: () => Promise<unknown> | void, onDone: () => void) {
    setPending(true);
    setError(null);
    try {
      await action();
      onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setPending(false);
    }
  }

  function startEdit(key: string, value: string) {
    setAdding(false);
    setError(null);
    setEditingKey(key);
    setEditValue(value);
  }

  function saveEdit() {
    if (editingKey === null) return;
    const value = editValue.trim();
    const invalid = labelValidationError(editingKey, value);
    if (invalid) {
      setError(invalid);
      return;
    }
    void run(
      () => onSet(editingKey, value),
      () => setEditingKey(null),
    );
  }

  function resetAdd() {
    setAdding(false);
    setNewKey("");
    setNewValue("");
  }

  /** Validate the add row, showing its message when invalid. */
  function validateNew(): { key: string; value: string } | null {
    const key = newKey.trim();
    const value = newValue.trim();
    const invalid =
      labelValidationError(key, value) ??
      (entries.some(([existing]) => existing === key)
        ? `"${key}" already exists. Edit it instead.`
        : null);
    if (invalid) {
      setError(invalid);
      return null;
    }
    return { key, value };
  }

  function saveNew() {
    const label = validateNew();
    if (label) void run(() => onSet(label.key, label.value), resetAdd);
  }

  useImperativeHandle(handleRef, () => ({
    takePendingLabel: () => {
      if (editingKey !== null) {
        const value = editValue.trim();
        const invalid = labelValidationError(editingKey, value);
        if (invalid) {
          setError(invalid);
          return { status: "invalid" };
        }
        setError(null);
        setEditingKey(null);
        return { status: "valid", key: editingKey, value };
      }
      if (!adding || (!newKey.trim() && !newValue.trim())) return { status: "none" };
      const label = validateNew();
      if (!label) return { status: "invalid" };
      setError(null);
      resetAdd();
      return { status: "valid", ...label };
    },
  }));

  return (
    <div className="flex flex-col gap-1.5" data-testid={testIdPrefix}>
      {entries.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {entries.map(([key, value]) =>
            editingKey === key ? (
              <li key={key} className="flex min-w-0 items-center gap-1">
                <span className="max-w-[40%] shrink-0 truncate font-mono text-sm text-muted-foreground">
                  {key}
                </span>
                <Input
                  autoFocus
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  onKeyDown={onEnter(saveEdit)}
                  aria-label={`Value for label ${key}`}
                  className="h-7 font-mono text-sm"
                  disabled={controlsDisabled}
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Save label ${key}`}
                  onClick={saveEdit}
                  disabled={controlsDisabled}
                >
                  <CheckIcon className="size-3" />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Cancel editing"
                  onClick={() => {
                    setEditingKey(null);
                    setError(null);
                  }}
                  disabled={pending}
                >
                  <XIcon className="size-3" />
                </Button>
              </li>
            ) : (
              <li
                key={key}
                className="flex min-w-0 items-center gap-1 font-mono text-sm"
                data-testid={`${testIdPrefix}-row`}
              >
                <span className="max-w-[40%] shrink-0 truncate text-muted-foreground" title={key}>
                  {key}:
                </span>
                <span className="min-w-0 flex-1 truncate" title={value}>
                  {value}
                </span>
                <LabelRowNote note={rowNote?.(key, value)} />
                {!readOnly && (
                  <>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      aria-label={`Edit label ${key}`}
                      onClick={() => startEdit(key, value)}
                      disabled={controlsDisabled}
                    >
                      <PencilIcon className="size-3" />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      aria-label={`Remove label ${key}`}
                      onClick={() =>
                        void run(
                          () => onRemove(key),
                          () => {},
                        )
                      }
                      disabled={controlsDisabled}
                    >
                      <XIcon className="size-3 text-destructive" />
                    </Button>
                  </>
                )}
              </li>
            ),
          )}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">{emptyText}</p>
      )}
      {!readOnly &&
        (adding ? (
          <div className="flex flex-col gap-1">
            <div className="flex min-w-0 items-center gap-1">
              <Input
                autoFocus
                value={newKey}
                onChange={(e) => setNewKey(e.target.value)}
                onKeyDown={onEnter(saveNew)}
                placeholder="key"
                aria-label="New label key"
                className="h-7 font-mono text-sm"
                disabled={controlsDisabled}
              />
              <Input
                value={newValue}
                onChange={(e) => setNewValue(e.target.value)}
                onKeyDown={onEnter(saveNew)}
                placeholder="value"
                aria-label="New label value"
                className="h-7 font-mono text-sm"
                disabled={controlsDisabled}
              />
            </div>
            <div className="flex justify-end gap-1">
              <Button
                type="button"
                variant="ghost"
                size="xs"
                onClick={() => {
                  resetAdd();
                  setError(null);
                }}
                disabled={pending}
              >
                Cancel
              </Button>
              <Button
                type="button"
                size="xs"
                onClick={saveNew}
                loading={pending}
                disabled={controlsDisabled}
              >
                Add
              </Button>
            </div>
          </div>
        ) : (
          <Button
            type="button"
            variant="ghost"
            size="xs"
            className="self-start text-muted-foreground"
            onClick={() => {
              setEditingKey(null);
              setError(null);
              setAdding(true);
            }}
            disabled={controlsDisabled}
          >
            <PlusIcon className="size-3" />
            Add label
          </Button>
        ))}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

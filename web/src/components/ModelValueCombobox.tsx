/**
 * Single-input multi-select combobox for policy array params (e.g.
 * ``expensive_models``).
 *
 * Users can type a free-form value or pick from a themed dropdown below the
 * input. Every option stays listed with a checkmark on the ones already
 * selected; clicking toggles it. Pressing Enter adds the typed value.
 *
 * The dropdown renders in normal flow (not absolutely positioned) so it grows
 * the dialog and scrolls with it rather than floating over — and overlapping —
 * the buttons beneath. The enclosing dialog is the scroll container.
 */

import { useEffect, useRef, useState } from "react";
import { CheckIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export function ModelValueCombobox({
  options,
  selected,
  onToggle,
  placeholder = "Select or type a value…",
}: {
  options: string[];
  selected: string[];
  onToggle: (value: string) => void;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const wrapperRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const lower = query.toLowerCase();
  const filtered = lower ? options.filter((v) => v.toLowerCase().includes(lower)) : options;
  const selectedSet = new Set(selected);
  const showList = open && filtered.length > 0;

  function pick(value: string) {
    const v = value.trim();
    if (!v) return;
    onToggle(v);
    setQuery("");
    // Keep the dropdown open so more values can be picked in a row; the input
    // is already focused, so relying on onFocus to reopen wouldn't fire.
    setOpen(true);
    inputRef.current?.focus();
  }

  // Close when clicking outside the combobox.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      if (!wrapperRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  return (
    <div ref={wrapperRef} className="space-y-1">
      <input
        ref={inputRef}
        type="text"
        value={query}
        placeholder={placeholder}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            pick(query);
          } else if (e.key === "Escape") {
            setOpen(false);
          }
        }}
        className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring"
      />
      {showList && (
        <div className="max-h-40 overflow-y-auto rounded-lg border border-border bg-popover p-1 text-popover-foreground">
          {filtered.map((v) => {
            const isSelected = selectedSet.has(v);
            return (
              <button
                key={v}
                type="button"
                className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent hover:text-accent-foreground"
                // mousedown fires before the input's blur, so the value toggles
                // before the click-outside handler would close the dropdown.
                onMouseDown={(e) => {
                  e.preventDefault();
                  pick(v);
                }}
              >
                <CheckIcon className={cn("size-3.5 shrink-0", !isSelected && "opacity-0")} />
                <span className="flex-1 truncate">{v}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

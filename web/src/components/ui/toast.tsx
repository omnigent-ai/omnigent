// A minimal, dependency-free toast system.
//
// Self-contained in the same spirit as KeyboardShortcutsDialog: a module-level
// `showToast()` dispatches a window event, and a single `<Toaster />` (mounted
// once near the app shell) listens and renders a top-center stack. Toasts
// auto-dismiss after a timeout and carry a manual dismiss control.
//
// Content is an arbitrary ReactNode so callers can embed links/actions (e.g.
// a routing <Link>); it renders inside the Toaster, which sits within the
// Router, so links resolve normally.

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { XIcon } from "lucide-react";

const TOAST_EVENT = "omnigent:toast";
const DEFAULT_DURATION_MS = 6000;

interface ToastItem {
  id: number;
  content: ReactNode;
  duration: number;
}

let counter = 0;

/** Show a transient toast. `duration <= 0` keeps it until manually dismissed. */
export function showToast(content: ReactNode, opts?: { duration?: number }): void {
  if (typeof window === "undefined") return;
  counter += 1;
  const detail: ToastItem = {
    id: counter,
    content,
    duration: opts?.duration ?? DEFAULT_DURATION_MS,
  };
  window.dispatchEvent(new CustomEvent(TOAST_EVENT, { detail }));
}

/** Mount once near the app shell; renders the active toast stack. */
export function Toaster() {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  useEffect(() => {
    const onToast = (e: Event) => {
      const item = (e as CustomEvent<ToastItem>).detail;
      setToasts((prev) => [...prev, item]);
    };
    window.addEventListener(TOAST_EVENT, onToast);
    return () => window.removeEventListener(TOAST_EVENT, onToast);
  }, []);

  if (toasts.length === 0) return null;

  return (
    <div className="pointer-events-none fixed inset-x-0 top-4 z-[100] flex flex-col items-center gap-2 px-4">
      {toasts.map((t) => (
        <ToastRow key={t.id} item={t} onDismiss={dismiss} />
      ))}
    </div>
  );
}

function ToastRow({ item, onDismiss }: { item: ToastItem; onDismiss: (id: number) => void }) {
  useEffect(() => {
    if (item.duration <= 0) return;
    const timer = setTimeout(() => onDismiss(item.id), item.duration);
    return () => clearTimeout(timer);
  }, [item.id, item.duration, onDismiss]);

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="toast"
      className="pointer-events-auto flex max-w-full items-center gap-3 rounded-full border border-border bg-card px-4 py-2 text-sm shadow-lg"
    >
      <span className="min-w-0">{item.content}</span>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => onDismiss(item.id)}
        className="shrink-0 text-muted-foreground transition-colors hover:text-foreground"
      >
        <XIcon className="size-4" />
      </button>
    </div>
  );
}

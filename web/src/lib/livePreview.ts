// One live-preview transaction for a picker: the highlighted value is applied
// without persisting it, a sweep across many rows paints only the last one, and
// the transaction ends by committing a value or reverting to the committed one.

export interface LivePreviewOptions<Value> {
  /** The value committed when the transaction began; cancelling restores it. */
  committed: Value;
  /** Shows a value without persisting it. */
  apply: (value: Value) => void;
  /** Persists and shows the chosen value. */
  commit: (value: Value) => void;
  /** Runs `callback` once before the next paint; returns a handle for `cancelFrame`. */
  requestFrame?: (callback: () => void) => number;
  cancelFrame?: (handle: number) => void;
}

export interface LivePreview<Value> {
  /** Previews a value, or the committed one for null, on the next frame. */
  preview: (value: Value | null) => void;
  /** Shows the committed value right away until `resume`, such as while the window is in the background. */
  suspend: () => void;
  /** Shows the latest previewed value again after `suspend`. */
  resume: () => void;
  /** Ends the transaction keeping `value`. */
  commit: (value: Value) => void;
  /** Ends the transaction restoring the committed value. */
  cancel: () => void;
}

export function createLivePreview<Value>({
  committed,
  apply,
  commit,
  requestFrame = (callback) => window.requestAnimationFrame(callback),
  cancelFrame = (handle) => window.cancelAnimationFrame(handle),
}: LivePreviewOptions<Value>): LivePreview<Value> {
  let applied = committed;
  let requested = committed;
  let pendingFrame: number | null = null;
  let suspended = false;
  let ended = false;

  const dropPending = () => {
    if (pendingFrame !== null) cancelFrame(pendingFrame);
    pendingFrame = null;
  };
  const show = (value: Value) => {
    if (Object.is(value, applied)) return;
    applied = value;
    apply(value);
  };
  const showRequestedNextFrame = () => {
    dropPending();
    pendingFrame = requestFrame(() => {
      pendingFrame = null;
      if (!ended && !suspended) show(requested);
    });
  };

  return {
    preview(value) {
      if (ended) return;
      requested = value ?? committed;
      if (!suspended) showRequestedNextFrame();
    },
    suspend() {
      if (ended || suspended) return;
      suspended = true;
      dropPending();
      show(committed);
    },
    resume() {
      if (ended || !suspended) return;
      suspended = false;
      showRequestedNextFrame();
    },
    commit(value) {
      if (ended) return;
      ended = true;
      dropPending();
      applied = value;
      commit(value);
    },
    cancel() {
      if (ended) return;
      ended = true;
      dropPending();
      show(committed);
    },
  };
}

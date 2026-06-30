import { create } from "zustand";

interface TerminalActivityState {
  /** terminalId -> epoch ms of the last observed PTY-output pulse. */
  lastActive: Record<string, number>;
  /** Record a fresh activity pulse for a terminal. */
  pulse: (terminalId: string) => void;
}

/**
 * Cross-component store of per-terminal activity pulses.
 *
 * Fed by the runner-determined ``session.terminal.activity`` SSE event
 * (the runner's pane watcher) and by the mounted terminal's own PTY
 * output. Read by ``useTerminalStatuses`` to drive the "active" badge for
 * ANY terminal — selected or not — without a client PTY attach. This is
 * what replaced the per-terminal fan-out WebSocket attaches.
 */
export const useTerminalActivityStore = create<TerminalActivityState>((set) => ({
  lastActive: {},
  pulse: (terminalId) =>
    set((s) => ({ lastActive: { ...s.lastActive, [terminalId]: Date.now() } })),
}));

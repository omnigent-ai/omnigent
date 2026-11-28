// When the user last typed into each session terminal, for checks that a reading taken before
// that may be out of date, such as whether closing an idle-looking shell can skip asking.

const lastInput = new Map<string, number>();
const inputKey = (sessionId: string, terminalId: string) => JSON.stringify([sessionId, terminalId]);

/** Record that the user typed into a session terminal just now. */
export function noteTerminalInput(sessionId: string, terminalId: string): void {
  lastInput.set(inputKey(sessionId, terminalId), performance.now());
}

/** When the user last typed into it, on the `performance.now()` clock, or -Infinity. */
export function lastTerminalInputAt(sessionId: string, terminalId: string): number {
  return lastInput.get(inputKey(sessionId, terminalId)) ?? -Infinity;
}

/** Forget every record; for tests. */
export function resetTerminalInputTimes(): void {
  lastInput.clear();
}

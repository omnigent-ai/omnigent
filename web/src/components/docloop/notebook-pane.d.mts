export interface PaneOptions {
  sessionId: string;
  fetcher: (input: string, init?: RequestInit) => Promise<Response>;
  pollMs?: number;
}
export function mountNotebookPane(
  host: HTMLElement,
  options: PaneOptions,
): {
  refresh: () => Promise<void>;
  dispose: () => void;
};

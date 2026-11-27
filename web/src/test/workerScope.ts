import { runInNewContext } from "node:vm";

/** A worker bundle started where a worker runs: no document, window or DOMParser. */
export interface StartedWorker {
  /** Sends `data` as a message, then lets the worker answer. */
  send: (data: unknown) => Promise<unknown[]>;
}

/** Runs `code` as a worker would; throws as the worker would fail to start. */
export function startInWorkerScope(code: string): StartedWorker {
  const answers: unknown[] = [];
  const scope: {
    onmessage: ((event: { data: unknown }) => void) | null;
    postMessage: (message: unknown) => void;
  } = {
    onmessage: null,
    postMessage: (message) => answers.push(message),
  };
  // What a worker scope has that the diffs rely on.
  const { TextDecoder, TextEncoder, URL, queueMicrotask, setTimeout, clearTimeout } = globalThis;
  runInNewContext(code, {
    self: scope,
    console,
    TextDecoder,
    TextEncoder,
    URL,
    queueMicrotask,
    setTimeout,
    clearTimeout,
  });
  if (typeof scope.onmessage !== "function") throw new Error("The worker set no message handler.");
  return {
    send: async (data) => {
      answers.length = 0;
      scope.onmessage!({ data });
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
      return [...answers];
    },
  };
}

/** A Markdown diff whose entities and math reach the parts whose browser builds need the DOM. */
export const RICH_MARKDOWN_REQUEST = {
  id: 1,
  request: {
    before: "# Notes\n\nPrice &amp; tax\n\n$$\nx^2\n$$\n",
    after: "# Notes\n\nPrice &amp; fees\n\n$$\ny^3\n$$\n",
  },
};

/** What a worker's answer to {@link RICH_MARKDOWN_REQUEST} rendered: its blocks, and their text. */
export function richMarkdownAnswer(answers: unknown[]): {
  id: unknown;
  status: unknown;
  rendered: string;
  text: string;
} {
  const [answer] = answers as [
    { id: unknown; result?: { status: unknown; sections?: { text: string }[] } } | undefined,
  ];
  const sections = answer?.result?.sections ?? [];
  return {
    id: answer?.id,
    status: answer?.result?.status,
    rendered: JSON.stringify(sections),
    text: sections.map((section) => section.text).join("\n"),
  };
}

import { runInNewContext } from "node:vm";

import { afterEach, describe, expect, it } from "vitest";
import { waitFor } from "@testing-library/react";

import { DATA_STORY_HTML } from "./dataStoryTemplate";

interface SnapshotDoc {
  id: string;
  version: string;
  value: unknown;
}

// The real inline script runs against jsdom with the SDK faked at window.omnigent, the external
// boundary. Writes stay pending until the test settles them.
function startStory({ preview = false, publish = true }: { preview?: boolean; publish?: boolean }) {
  let listener: ((snapshot: { status: string; items: SnapshotDoc[] }) => void) | null = null;
  const creates: { value: unknown; resolve: (doc: SnapshotDoc) => void }[] = [];
  const snapshots = {
    watch: (next: typeof listener) => {
      listener = next;
      return { dispose: () => undefined, refresh: () => undefined };
    },
    create: (value: unknown) =>
      new Promise<SnapshotDoc>((resolve) => {
        creates.push({ value, resolve });
      }),
  };
  const client = {
    context: {
      viewer: { id: "1001", displayName: "viewer@example.com" },
      permissions: { writeData: true, publish },
      preview,
    },
    hasCapability: (name: string) => name === "data.write",
    ui: { colorScheme: "light" },
    data: { collection: () => snapshots },
    agent: {
      request: () => Promise.resolve({ actionId: "a1", delivery: "sent" }),
      watch: () => ({ refresh: () => undefined, dispose: () => undefined }),
    },
  };
  const bodyStart = DATA_STORY_HTML.indexOf("<body>") + "<body>".length;
  const scriptStart = DATA_STORY_HTML.lastIndexOf("<script>");
  const scriptEnd = DATA_STORY_HTML.lastIndexOf("</script>");
  document.body.innerHTML = DATA_STORY_HTML.slice(bodyStart, scriptStart);
  runInNewContext(DATA_STORY_HTML.slice(scriptStart + "<script>".length, scriptEnd), {
    window: {
      omnigent: { connect: () => Promise.resolve(client), isOmnigentError: () => false },
    },
    document,
    setTimeout,
    clearTimeout,
  });
  const emit = (items: SnapshotDoc[]) => listener?.({ status: "live", items });
  return { emit, creates, hasWatch: () => listener !== null };
}

const byId = (id: string) => document.getElementById(id) as HTMLElement;

afterEach(() => {
  document.body.replaceChildren();
});

describe("data story template", () => {
  it("shows its sample as soon as it is saved, without an empty state in between", async () => {
    const { emit, creates, hasWatch } = startStory({});
    await waitFor(() => expect(hasWatch()).toBe(true));

    emit([]);
    expect(creates).toHaveLength(1);
    expect(byId("empty").hidden).toBe(true);

    creates[0].resolve({ id: "sample-synthetic", version: "v1", value: creates[0].value });

    await waitFor(() => expect(byId("content").hidden).toBe(false));
    expect(byId("empty").hidden).toBe(true);
  });

  it("shows no access notice in a template preview", async () => {
    const { hasWatch } = startStory({ preview: true, publish: false });
    await waitFor(() => expect(hasWatch()).toBe(true));

    expect(byId("notice").hidden).toBe(true);
  });

  it("tells a viewer who cannot edit the thingy why Refresh is missing", async () => {
    const { hasWatch } = startStory({ publish: false });
    await waitFor(() => expect(hasWatch()).toBe(true));

    expect(byId("notice").textContent).toContain("needs Can edit thingy access");
  });
});

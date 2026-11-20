import { runInNewContext } from "node:vm";

import { afterEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";

import { screen, waitFor, within } from "@testing-library/react";

import { REFERENCE_KANBAN_HTML } from "./referenceKanbanApp";

interface ChecklistItem {
  text: string;
  done: boolean;
}

interface CardValue {
  title: string;
  owner: string;
  column: string;
  order: number;
  checklist: ChecklistItem[];
  notes?: string;
}

interface CardDoc {
  id: string;
  version: string;
  value: CardValue;
}

interface Snapshot {
  status: "loading" | "live";
  items: CardDoc[];
  nextCursor: string | null;
  updatedAt: number;
  error: Error | null;
}

interface WriteResult {
  id: string;
  version: string;
}

interface WriteOptions {
  ifVersion?: string;
  idempotencyKey?: string;
}

interface RecordedCall<Args extends unknown[], Result> {
  args: Args;
  resolve: (result: Result) => void;
  reject: (error: unknown) => void;
}

class FakeOmnigentError extends Error {
  readonly code: string;
  readonly operationId?: string;

  constructor(code: string, message: string, operationId?: string) {
    super(message);
    this.name = "OmnigentError";
    this.code = code;
    this.operationId = operationId;
  }
}

function record<Args extends unknown[], Result>(
  calls: RecordedCall<Args, Result>[],
  args: Args,
): Promise<Result> {
  return new Promise<Result>((resolve, reject) => {
    calls.push({ args, resolve, reject });
  });
}

/** The cards collection of the fake SDK: every write stays pending until the test settles it. */
class FakeCards {
  listener: ((snapshot: Snapshot) => void) | null = null;
  readonly creates: RecordedCall<[CardValue, WriteOptions], WriteResult>[] = [];
  readonly replaces: RecordedCall<[string, CardValue, WriteOptions], WriteResult>[] = [];
  readonly deletes: RecordedCall<[string, WriteOptions], WriteResult>[] = [];
  readonly gets: RecordedCall<[string], CardDoc | null>[] = [];

  watch(listener: (snapshot: Snapshot) => void): void {
    this.listener = listener;
  }

  create(value: CardValue, options: WriteOptions) {
    return record(this.creates, [value, options]);
  }

  replace(id: string, value: CardValue, options: WriteOptions) {
    return record(this.replaces, [id, value, options]);
  }

  delete(id: string, options: WriteOptions) {
    return record(this.deletes, [id, options]);
  }

  get(id: string) {
    return record(this.gets, [id]);
  }

  emit(items: CardDoc[]): void {
    this.listener?.({ status: "live", items, nextCursor: null, updatedAt: 0, error: null });
  }
}

interface RunDoc {
  id: string;
  version: string;
  updatedAt: string;
  value: { cardId: string; state: string; summary?: string; sessionId?: string };
}

/** The runs collection: sessions write it, so the board only watches it. */
class FakeRuns {
  listener:
    | ((snapshot: {
        status: "live";
        items: RunDoc[];
        nextCursor: null;
        updatedAt: number;
        error: null;
      }) => void)
    | null = null;

  watch(listener: NonNullable<FakeRuns["listener"]>): void {
    this.listener = listener;
  }

  emit(items: RunDoc[]): void {
    this.listener?.({ status: "live", items, nextCursor: null, updatedAt: 0, error: null });
  }
}

function nth<T>(items: T[], index: number): T {
  const item = items[index];
  if (!item) {
    throw new Error(`Expected recorded call ${index}`);
  }
  return item;
}

function cardDoc(
  id: string,
  value: Partial<CardValue> & { title: string },
  version = "v1",
): CardDoc {
  return { id, version, value: { owner: "", column: "todo", order: 1, checklist: [], ...value } };
}

// The script talks to `window` and `document`. The document is the real jsdom one, wrapped so the
// listeners the script adds can be removed after each test; the window is a stand-in that carries
// the SDK, which is the external boundary.
const trackedListeners: {
  type: string;
  listener: EventListenerOrEventListenerObject;
  options?: boolean | AddEventListenerOptions;
}[] = [];
const trackedTimeouts = new Set<ReturnType<typeof setTimeout>>();

afterEach(() => {
  trackedListeners.splice(0).forEach(({ type, listener, options }) => {
    document.removeEventListener(type, listener, options);
  });
  trackedTimeouts.forEach((id) => clearTimeout(id));
  trackedTimeouts.clear();
  document.body.replaceChildren();
  document.body.removeAttribute("data-theme");
});

const scriptDocument = new Proxy(document, {
  get(target, property) {
    if (property === "addEventListener") {
      return (
        type: string,
        listener: EventListenerOrEventListenerObject,
        options?: boolean | AddEventListenerOptions,
      ) => {
        trackedListeners.push({ type, listener, options });
        target.addEventListener(type, listener, options);
      };
    }
    const value: unknown = Reflect.get(target, property, target);
    return typeof value === "function" ? value.bind(target) : value;
  },
});

interface BoardOptions {
  writeData?: boolean;
  capability?: boolean;
  items?: CardDoc[];
  /** False for a host that predates the navigation methods. */
  navigation?: boolean;
  /** Whether the viewer can edit the thingy, which running actions needs. */
  publish?: boolean;
  /** A template preview, whose data lives in memory. */
  preview?: boolean;
}

async function startBoard({
  writeData = true,
  capability = true,
  items,
  navigation = true,
  publish = true,
  preview = false,
}: BoardOptions = {}) {
  const cards = new FakeCards();
  const runs = new FakeRuns();
  const nav = {
    newSession: vi.fn((_params: { prompt: string; display?: string }) =>
      Promise.resolve({ opened: true }),
    ),
    openSession: vi.fn((_sessionId: string, _options?: { display?: string }) =>
      Promise.resolve({ opened: true }),
    ),
  };
  const actions: { listener: ((snapshot: { status: string; items: unknown[] }) => void) | null } = {
    listener: null,
  };
  const agent = {
    request: vi.fn((_action: string, _params: { cardId: string; runId: string }) =>
      Promise.resolve({ actionId: "action-1", delivery: "sent" }),
    ),
    watch: vi.fn((listener: (snapshot: { status: string; items: unknown[] }) => void) => {
      actions.listener = listener;
      return { refresh: () => undefined, dispose: () => undefined };
    }),
  };
  // Stands in for the progress Omnigent derives from the sessions doing the work.
  const emitActions = (progress: unknown[]) =>
    actions.listener?.({ status: "live", items: progress });
  const client = {
    context: {
      viewer: { id: "1001", displayName: "viewer@example.com" },
      permissions: { writeData, publish },
      preview,
    },
    hasCapability: (name: string) => capability && name === "data.write",
    ui: { colorScheme: "light" },
    data: { collection: (name: string) => (name === "runs" ? runs : cards) },
    ...(navigation ? { navigation: nav, agent } : {}),
  };
  const scriptWindow = {
    omnigent: {
      connect: () => Promise.resolve(client),
      isOmnigentError: (error: unknown) => error instanceof FakeOmnigentError,
    },
    setTimeout: (callback: () => void, delay: number) => {
      const id = setTimeout(callback, delay);
      trackedTimeouts.add(id);
      return id;
    },
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => undefined,
    getSelection: () => "",
    innerHeight: 800,
    scrollBy: () => undefined,
  };

  // The uploaded document is one body followed by one inline script; jsdom does not run
  // scripts set through innerHTML, so the script runs below through the vm instead.
  const bodyStart = REFERENCE_KANBAN_HTML.indexOf("<body>") + "<body>".length;
  const scriptStart = REFERENCE_KANBAN_HTML.lastIndexOf("<script>");
  const scriptEnd = REFERENCE_KANBAN_HTML.lastIndexOf("</script>");
  if (bodyStart < "<body>".length || scriptStart < bodyStart || scriptEnd < scriptStart) {
    throw new Error("The reference app carries no inline script");
  }
  const script = REFERENCE_KANBAN_HTML.slice(scriptStart + "<script>".length, scriptEnd);
  document.body.innerHTML = REFERENCE_KANBAN_HTML.slice(bodyStart, scriptStart);
  runInNewContext(script, { window: scriptWindow, document: scriptDocument });
  await waitFor(() => expect(cards.listener).not.toBeNull());
  if (items) {
    cards.emit(items);
  }
  return { emitActions, cards, runs, nav, agent };
}

const columnList = (name: string) => screen.getByRole("list", { name: new RegExp(`^${name}`) });
const titlesIn = (list: HTMLElement) =>
  within(list)
    .queryAllByRole("heading", { level: 3 })
    .map((heading) => heading.textContent);
const cardNamed = (name: string) => screen.findByRole("listitem", { name });

async function openEditor(title: string) {
  await userEvent.click(await screen.findByRole("button", { name: title }));
  return screen.findByLabelText("Title");
}

const ERROR_ITEMS = [cardDoc("c1", { title: "Fix login" })];

async function saveRename(next: string) {
  const title = await openEditor("Fix login");
  await userEvent.clear(title);
  await userEvent.paste(next);
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
}

// The real inline app script runs against jsdom with the SDK faked at window.omnigent.
describe("reference Kanban app", () => {
  describe("card controls", () => {
    it("offers a title button and a grip without the old move buttons, column select or add-item input", async () => {
      await startBoard({ items: [cardDoc("c1", { title: "Fix login", notes: "- [ ] one" })] });

      expect(await screen.findByRole("button", { name: "Fix login" })).toBeVisible();
      expect(screen.getByRole("button", { name: "Move Fix login" })).toBeVisible();
      expect(screen.queryByRole("button", { name: /move (left|right)/i })).not.toBeInTheDocument();
      expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: /add item/i })).not.toBeInTheDocument();
    });
  });

  describe("keyboard move", () => {
    const items = [
      cardDoc("c1", { title: "Draft spec", column: "todo", order: 1 }),
      cardDoc("c2", { title: "Review spec", column: "doing", order: 1 }),
    ];

    it("shows the card in the destination column at once and saves its column and order conditionally", async () => {
      const { cards } = await startBoard({ items });
      const grip = await screen.findByRole("button", { name: "Move Draft spec" });
      grip.focus();

      await userEvent.keyboard("[Space]");
      await userEvent.keyboard("{ArrowRight}");
      await userEvent.keyboard("[Space]");

      expect(cards.replaces).toHaveLength(1);
      const [id, value, options] = nth(cards.replaces, 0).args;
      expect(id).toBe("c1");
      expect(value).toMatchObject({ title: "Draft spec", column: "doing", order: 0 });
      expect(options).toEqual({ ifVersion: "v1" });
      await waitFor(() =>
        expect(titlesIn(columnList("In progress"))).toEqual(["Draft spec", "Review spec"]),
      );
      expect(titlesIn(columnList("Backlog"))).toEqual([]);
      expect(within(await cardNamed("Draft spec")).getByText("Moving…")).toBeVisible();
      expect(screen.getByText("Saving 1 change…")).toBeVisible();

      nth(cards.replaces, 0).resolve({ id: "c1", version: "v2" });

      expect(await screen.findByText("All changes saved")).toBeVisible();
      expect(screen.queryByText("Moving…")).not.toBeInTheDocument();
    });

    it("leaves the card where it was when Escape cancels the move", async () => {
      const { cards } = await startBoard({ items });
      const grip = await screen.findByRole("button", { name: "Move Draft spec" });
      grip.focus();

      await userEvent.keyboard("[Space]");
      await userEvent.keyboard("{ArrowRight}");
      await userEvent.keyboard("{Escape}");

      await waitFor(() => expect(titlesIn(columnList("Backlog"))).toEqual(["Draft spec"]));
      expect(titlesIn(columnList("In progress"))).toEqual(["Review spec"]);
      expect(cards.replaces).toHaveLength(0);
    });
  });

  describe("Markdown notes", () => {
    it("builds notes from text nodes so raw HTML stays literal text", async () => {
      await startBoard({
        items: [
          cardDoc("c1", {
            title: "Notes card",
            notes: [
              "**bold** and _soft_ <img src=x onerror=alert(1)>",
              "",
              "# Plan",
              "",
              "- plain item",
            ].join("\n"),
          }),
        ],
      });

      expect(await screen.findByText("bold")).toHaveProperty("tagName", "STRONG");
      expect(screen.getByText("soft")).toHaveProperty("tagName", "EM");
      expect(screen.getByRole("heading", { name: "Plan", level: 4 })).toBeVisible();
      expect(screen.getByText("plain item")).toBeVisible();
      expect(screen.getByText(/<img src=x onerror=alert\(1\)>/)).toBeVisible();
      expect(screen.queryByRole("img")).not.toBeInTheDocument();
    });

    it("renders a checklist-only legacy card as task items with their saved state", async () => {
      await startBoard({
        items: [
          cardDoc("c1", {
            title: "Legacy card",
            checklist: [
              { text: "Write docs", done: true },
              { text: "Ship it", done: false },
            ],
          }),
        ],
      });

      expect(await screen.findByRole("checkbox", { name: "Write docs" })).toBeChecked();
      expect(screen.getByRole("checkbox", { name: "Ship it" })).not.toBeChecked();
    });
  });

  describe("editor", () => {
    const items = [cardDoc("c1", { title: "Fix login", owner: "Dana" })];
    const NOTES = "- [ ] one\n- [x] two";

    it.each(["Control", "Meta"])(
      "saves title, owner and notes on %s+Enter and mirrors tasks into the checklist",
      async (modifier) => {
        const { cards } = await startBoard({ items });
        const title = await openEditor("Fix login");
        await userEvent.clear(title);
        await userEvent.paste("Fix sign-in");
        await userEvent.click(screen.getByLabelText("Notes"));
        await userEvent.paste(NOTES);

        await userEvent.keyboard(`{${modifier}>}{Enter}{/${modifier}}`);

        expect(cards.replaces).toHaveLength(1);
        const [id, value, options] = nth(cards.replaces, 0).args;
        expect(id).toBe("c1");
        expect(value).toEqual({
          title: "Fix sign-in",
          owner: "Dana",
          column: "todo",
          order: 1,
          notes: NOTES,
          checklist: [
            { text: "one", done: false },
            { text: "two", done: true },
          ],
        });
        expect(options).toEqual({ ifVersion: "v1" });
        expect(await screen.findByRole("button", { name: "Fix sign-in" })).toBeVisible();
      },
    );

    it("saves from the Save button and shows the saving state until the write settles", async () => {
      const { cards } = await startBoard({ items });

      await saveRename("Fix sign-in");

      expect(cards.replaces).toHaveLength(1);
      expect(within(await cardNamed("Fix sign-in")).getByText("Saving…")).toBeVisible();
      expect(screen.getByText("Saving 1 change…")).toBeVisible();
    });

    it("closes without writing when Esc cancels the edit", async () => {
      const { cards } = await startBoard({ items });
      const title = await openEditor("Fix login");
      await userEvent.click(title);
      await userEvent.paste(" draft");

      await userEvent.keyboard("{Escape}");

      expect(await screen.findByRole("button", { name: "Fix login" })).toBeVisible();
      expect(screen.queryByLabelText("Title")).not.toBeInTheDocument();
      expect(cards.replaces).toHaveLength(0);
    });

    it("shows the deleting state after confirmation and removes the card once the delete settles", async () => {
      const { cards } = await startBoard({ items });
      await openEditor("Fix login");

      await userEvent.click(screen.getByRole("button", { name: "Delete card" }));
      await userEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

      expect(nth(cards.deletes, 0).args).toEqual(["c1", { ifVersion: "v1" }]);
      expect(within(await cardNamed("Fix login")).getByText("Deleting…")).toBeVisible();

      nth(cards.deletes, 0).resolve({ id: "c1", version: "v2" });

      await waitFor(() =>
        expect(screen.queryByRole("listitem", { name: "Fix login" })).not.toBeInTheDocument(),
      );
      expect(screen.getByText("All changes saved")).toBeVisible();
    });
  });

  describe("task toggle", () => {
    it("shows the task checked at once and replaces notes and checklist", async () => {
      const { cards } = await startBoard({
        items: [
          cardDoc("c1", {
            title: "Tasks",
            notes: "- [ ] one\n- [ ] two",
            checklist: [
              { text: "one", done: false },
              { text: "two", done: false },
            ],
          }),
        ],
      });

      await userEvent.click(await screen.findByRole("checkbox", { name: "two" }));

      expect(nth(cards.replaces, 0).args[1]).toMatchObject({
        notes: "- [ ] one\n- [x] two",
        checklist: [
          { text: "one", done: false },
          { text: "two", done: true },
        ],
      });
      expect(nth(cards.replaces, 0).args[2]).toEqual({ ifVersion: "v1" });
      await waitFor(() => expect(screen.getByRole("checkbox", { name: "two" })).toBeChecked());
      expect(screen.getByText("Saving 1 change…")).toBeVisible();
    });
  });

  describe("loading and adding", () => {
    it("shows skeleton placeholders before the first snapshot and an empty state after it", async () => {
      const { cards } = await startBoard();

      await waitFor(() =>
        expect(
          within(columnList("Backlog")).getAllByRole("listitem", { hidden: true }),
        ).toHaveLength(2),
      );
      expect(screen.getByRole("status")).toHaveTextContent("Loading");
      expect(screen.queryByText("No cards")).not.toBeInTheDocument();

      cards.emit([]);

      expect(await screen.findAllByText("No cards")).toHaveLength(4);
      expect(screen.getByRole("status")).toHaveTextContent("Live");
    });

    it("starts a template preview with its sample cards and no sign-in or saving text", async () => {
      const { cards } = await startBoard({ preview: true, publish: false });

      cards.emit([]);

      await waitFor(() => expect(cards.creates).toHaveLength(3));
      cards.creates.forEach((call, index) =>
        call.resolve({ id: `sample-${index}`, version: "v1" }),
      );
      await waitFor(() =>
        expect(
          screen.getByRole("heading", { name: /Sample: draft the onboarding checklist/ }),
        ).toBeVisible(),
      );
      expect(screen.getByRole("status")).toHaveTextContent("Preview");
      expect(screen.queryByText(/Signed in as/)).not.toBeInTheDocument();
      expect(screen.queryByText("All changes saved")).not.toBeInTheDocument();
    });

    it("shows the added card as adding before the server answers, then settles it", async () => {
      const { cards } = await startBoard({ items: [] });
      await userEvent.click(await screen.findByRole("button", { name: "Add a card to Backlog" }));

      await userEvent.click(
        screen.getByRole("textbox", { name: "Title of the new card in Backlog" }),
      );
      await userEvent.paste("Write notes");
      await userEvent.keyboard("{Enter}");

      expect(nth(cards.creates, 0).args).toEqual([
        { title: "Write notes", owner: "", column: "todo", order: 1, checklist: [], notes: "" },
        {},
      ]);
      expect(within(await cardNamed("Write notes")).getByText("Adding…")).toBeVisible();
      expect(screen.getByText("Saving 1 change…")).toBeVisible();

      nth(cards.creates, 0).resolve({ id: "c9", version: "v1" });

      expect(await screen.findByText("All changes saved")).toBeVisible();
      expect(screen.queryByText("Adding…")).not.toBeInTheDocument();
    });
  });

  describe("failed saves", () => {
    it("offers Keep my change on a conflict, rebasing on the latest version and saving again", async () => {
      const { cards } = await startBoard({ items: ERROR_ITEMS });
      await saveRename("Fix sign-in");
      nth(cards.replaces, 0).reject(new FakeOmnigentError("CONFLICT", "conflict"));

      // The rejected save re-renders the card after its microtask, so wait for the alert before reading it.
      await screen.findByRole("alert");
      const card = await cardNamed("Fix sign-in");
      expect(within(card).getByRole("alert")).toHaveTextContent("Someone else changed this card.");
      expect(screen.getByText("1 change not saved")).toBeVisible();

      await userEvent.click(within(card).getByRole("button", { name: "Keep my change" }));
      expect(nth(cards.gets, 0).args).toEqual(["c1"]);
      nth(cards.gets, 0).resolve(cardDoc("c1", { title: "Fix login" }, "v7"));

      await waitFor(() => expect(cards.replaces).toHaveLength(2));
      expect(nth(cards.replaces, 1).args[1]).toMatchObject({ title: "Fix sign-in" });
      expect(nth(cards.replaces, 1).args[2]).toEqual({ ifVersion: "v7" });
    });

    it("keeps the other person's changes to fields this tab did not change", async () => {
      const { cards } = await startBoard({ items: ERROR_ITEMS });
      await saveRename("Fix sign-in");
      nth(cards.replaces, 0).reject(new FakeOmnigentError("CONFLICT", "conflict"));

      await userEvent.click(await screen.findByRole("button", { name: "Keep my change" }));
      nth(cards.gets, 0).resolve(
        cardDoc("c1", { title: "Fix login", owner: "Sam", notes: "Their notes" }, "v7"),
      );

      await waitFor(() => expect(cards.replaces).toHaveLength(2));
      expect(nth(cards.replaces, 1).args[1]).toEqual({
        title: "Fix sign-in",
        owner: "Sam",
        notes: "Their notes",
        column: "todo",
        order: 1,
        checklist: [],
      });
      expect(nth(cards.replaces, 1).args[2]).toEqual({ ifVersion: "v7" });
    });

    it("shows the board rule a save broke and puts the card back when it is dismissed", async () => {
      const { cards } = await startBoard({ items: ERROR_ITEMS });
      await saveRename("Fix sign-in");
      nth(cards.replaces, 0).reject(
        new FakeOmnigentError(
          "CONSTRAINT_VIOLATION",
          "In progress holds at most 5 cards. Move one on first.",
        ),
      );

      await screen.findByRole("alert");
      const card = await cardNamed("Fix sign-in");
      expect(within(card).getByRole("alert")).toHaveTextContent(
        "In progress holds at most 5 cards. Move one on first.",
      );
      expect(within(card).queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();

      await userEvent.click(within(card).getByRole("button", { name: "Dismiss" }));

      expect(await cardNamed("Fix login")).toBeVisible();
      expect(cards.replaces).toHaveLength(1);
    });

    it("restores the server copy when Use latest is chosen after a conflict", async () => {
      const { cards } = await startBoard({ items: ERROR_ITEMS });
      await saveRename("Fix sign-in");
      nth(cards.replaces, 0).reject(new FakeOmnigentError("CONFLICT", "conflict"));

      await userEvent.click(await screen.findByRole("button", { name: "Use latest" }));

      expect(await cardNamed("Fix login")).toBeVisible();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(cards.replaces).toHaveLength(1);
    });

    it("retries an unknown outcome with the same value, version and idempotency key", async () => {
      const { cards } = await startBoard({ items: ERROR_ITEMS });
      await saveRename("Fix sign-in");
      nth(cards.replaces, 0).reject(
        new FakeOmnigentError("DEADLINE_EXCEEDED", "Timed out", "op-9"),
      );

      // The rejected save re-renders the card after its microtask, so wait for the alert before reading it.
      await screen.findByRole("alert");
      const card = await cardNamed("Fix sign-in");
      expect(within(card).getByRole("alert")).toHaveTextContent(
        "This change might not be saved. Check your connection and retry.",
      );
      await userEvent.click(within(card).getByRole("button", { name: "Retry" }));

      expect(cards.replaces).toHaveLength(2);
      expect(nth(cards.replaces, 1).args[1]).toEqual(nth(cards.replaces, 0).args[1]);
      expect(nth(cards.replaces, 0).args[2]).toEqual({ ifVersion: "v1" });
      expect(nth(cards.replaces, 1).args[2]).toEqual({ ifVersion: "v1", idempotencyKey: "op-9" });
    });

    it("restores the server copy when an unknown outcome is discarded", async () => {
      const { cards } = await startBoard({ items: ERROR_ITEMS });
      await saveRename("Fix sign-in");
      nth(cards.replaces, 0).reject(
        new FakeOmnigentError("DEADLINE_EXCEEDED", "Timed out", "op-9"),
      );

      await userEvent.click(await screen.findByRole("button", { name: "Discard" }));

      expect(await cardNamed("Fix login")).toBeVisible();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("offers only Dismiss when the card was deleted by someone else", async () => {
      const { cards } = await startBoard({ items: ERROR_ITEMS });
      await saveRename("Fix sign-in");
      nth(cards.replaces, 0).reject(new FakeOmnigentError("NOT_FOUND", "gone"));

      // The rejected save re-renders the card after its microtask, so wait for the alert before reading it.
      await screen.findByRole("alert");
      const card = await cardNamed("Fix sign-in");
      expect(within(card).getByRole("alert")).toHaveTextContent(
        "This card was deleted by someone else.",
      );
      expect(within(card).getByRole("button", { name: "Dismiss" })).toBeVisible();
      expect(within(card).queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    });
  });

  describe("read-only viewers", () => {
    it.each([
      { reason: "the viewer lacks write permission", writeData: false, capability: true },
      { reason: "the app lacks the write capability", writeData: true, capability: false },
    ])("offers no grips, composers or editors when $reason", async ({ writeData, capability }) => {
      await startBoard({
        writeData,
        capability,
        items: [cardDoc("c1", { title: "Fix login", notes: "- [ ] one" })],
      });

      expect(await screen.findByText("Fix login")).toBeVisible();
      expect(screen.getByText(/You can view this board/)).toBeVisible();
      expect(screen.queryByRole("button", { name: "Move Fix login" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Fix login" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Add a card to/ })).not.toBeInTheDocument();
      expect(screen.getByRole("checkbox", { name: "one" })).toBeDisabled();
    });
  });
});

function runDoc(id: string, value: RunDoc["value"], updatedAt = "2026-11-15T00:00:00Z"): RunDoc {
  return { id, version: `${id}-v`, updatedAt, value };
}

function cardItem(title: string) {
  return screen.getByRole("listitem", { name: title });
}

// The real inline script runs against a fake SDK; the rendered board is the observable.
describe("reference Kanban runs in a session", () => {
  it("hands a card to the thingy session, waits for it, then shows what the session reports", async () => {
    const { runs, nav, agent } = await startBoard({
      items: [
        cardDoc("c1", {
          title: "Fix login",
          column: "doing",
          owner: "Ana",
          notes: "Check **SSO** first",
        }),
      ],
    });

    await userEvent.click(
      within(cardItem("Fix login")).getByRole("button", { name: "Run in a session" }),
    );

    expect(agent.request).toHaveBeenCalledTimes(1);
    const [action, params] = nth(agent.request.mock.calls, 0);
    expect(action).toBe("run_card");
    expect(params.cardId).toBe("c1");
    expect(params.runId).toMatch(/^run-[a-z0-9-]+$/);
    expect(nav.newSession).not.toHaveBeenCalled();
    expect(
      await within(cardItem("Fix login")).findByText("Sent to your thingy session."),
    ).toBeVisible();

    runs.emit([
      runDoc(params.runId, {
        cardId: "c1",
        state: "running",
        summary: "Reading the **logs**",
        sessionId: "s-1",
      }),
    ]);

    // Each update redraws the card, so read it again once the run shows.
    await waitFor(() =>
      expect(within(cardItem("Fix login")).getByText("Session running")).toBeVisible(),
    );
    const card = cardItem("Fix login");
    expect(within(card).getByText("logs", { selector: "strong" })).toBeVisible();
    expect(within(card).queryByText("Sent to your thingy session.")).not.toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Run again in a session" })).toBeVisible();
    // The thingy header's session toggle opens the session; the card does not repeat it.
    expect(within(card).queryByRole("button", { name: "Open session" })).not.toBeInTheDocument();
  });

  it("shows the progress Omnigent derives, not only what the session reports", async () => {
    const { emitActions } = await startBoard({
      items: [cardDoc("c1", { title: "Fix login", column: "doing" })],
    });
    await userEvent.click(
      within(cardItem("Fix login")).getByRole("button", { name: "Run in a session" }),
    );
    const tracked = {
      id: "action-1",
      action: "run_card",
      params: { cardId: "c1" },
      reason: null,
      summary: null,
      sessionIds: ["s-1", "s-2"],
    };

    emitActions([{ ...tracked, status: "running" }]);
    await waitFor(() =>
      expect(
        within(cardItem("Fix login")).getByText("Running in your thingy session"),
      ).toBeVisible(),
    );
    expect(within(cardItem("Fix login")).getByRole("button", { name: "Running…" })).toBeDisabled();

    emitActions([{ ...tracked, status: "needs_input" }]);
    await waitFor(() =>
      expect(
        within(cardItem("Fix login")).getByText("Waiting for your input in the session"),
      ).toBeVisible(),
    );

    // The session stopped without reporting: the card says so and can run again.
    emitActions([{ ...tracked, status: "interrupted", reason: "runner_offline" }]);
    await waitFor(() =>
      expect(
        within(cardItem("Fix login")).getByText("Stopped before it finished. Run it again."),
      ).toBeVisible(),
    );
    expect(
      within(cardItem("Fix login")).getByRole("button", { name: "Run again in a session" }),
    ).toBeEnabled();
  });

  it("asks the viewer to start their thingy session when they have none yet", async () => {
    const { agent } = await startBoard({ items: [cardDoc("c1", { title: "Fix login" })] });
    agent.request.mockReturnValueOnce(
      Promise.resolve({ actionId: "action-1", delivery: "starting" }),
    );

    await userEvent.click(
      within(cardItem("Fix login")).getByRole("button", { name: "Run in a session" }),
    );

    expect(
      await within(cardItem("Fix login")).findByText(
        "Create your thingy session to run this card.",
      ),
    ).toBeVisible();

    // Left without starting it, the run can open New session again.
    await userEvent.click(
      within(cardItem("Fix login")).getByRole("button", { name: "Open New session again" }),
    );
    expect(agent.request).toHaveBeenCalledTimes(2);
  });

  it("shows why the thingy session did not get the card", async () => {
    const { agent } = await startBoard({ items: [cardDoc("c1", { title: "Fix login" })] });
    agent.request.mockImplementationOnce(() =>
      Promise.reject(
        new FakeOmnigentError(
          "PERMISSION_DENIED",
          "Omnigent runs an action only right after a click in the app.",
        ),
      ),
    );

    await userEvent.click(
      within(cardItem("Fix login")).getByRole("button", { name: "Run in a session" }),
    );

    expect(await within(cardItem("Fix login")).findByRole("alert")).toHaveTextContent(
      "Omnigent runs an action only right after a click in the app.",
    );
  });

  it("offers to run a card only to viewers who can edit the thingy", async () => {
    await startBoard({ publish: false, items: [cardDoc("c1", { title: "Fix login" })] });

    expect(await screen.findByRole("listitem", { name: "Fix login" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Run in a session" })).not.toBeInTheDocument();
  });

  it("shows only the latest run of each card", async () => {
    const { runs } = await startBoard({
      items: [
        cardDoc("c1", { title: "Fix login" }),
        cardDoc("c2", { title: "Write docs", order: 2 }),
      ],
    });

    runs.emit([
      runDoc(
        "run-new",
        { cardId: "c1", state: "failed", summary: "Tests failed" },
        "2026-11-15T02:00:00Z",
      ),
      runDoc(
        "run-old",
        { cardId: "c1", state: "done", summary: "Old result" },
        "2026-11-15T01:00:00Z",
      ),
      runDoc("run-docs", { cardId: "c2", state: "needs_input", summary: "Which audience?" }),
    ]);

    await waitFor(() =>
      expect(within(cardItem("Fix login")).getByText("Session failed")).toBeVisible(),
    );
    const login = cardItem("Fix login");
    expect(within(login).getByText("Tests failed")).toBeVisible();
    expect(within(login).queryByText("Old result")).not.toBeInTheDocument();
    expect(within(cardItem("Write docs")).getByText("Session needs input")).toBeVisible();
  });

  it("shows a run to a read-only viewer without offering to start one", async () => {
    const { runs } = await startBoard({
      writeData: false,
      items: [cardDoc("c1", { title: "Fix login" })],
    });

    runs.emit([
      runDoc("run-1", { cardId: "c1", state: "done", summary: "Fixed", sessionId: "s-1" }),
    ]);

    await waitFor(() =>
      expect(within(cardItem("Fix login")).getByText("Session done")).toBeVisible(),
    );
    const card = cardItem("Fix login");
    expect(
      within(card).queryByRole("button", { name: /Run .*in a session/ }),
    ).not.toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Open session" })).not.toBeInTheDocument();
  });

  it("offers no session actions in a host without navigation", async () => {
    const { runs } = await startBoard({
      navigation: false,
      items: [cardDoc("c1", { title: "Fix login" })],
    });

    expect(await screen.findByRole("listitem", { name: "Fix login" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Run in a session" })).not.toBeInTheDocument();
    expect(runs.listener).toBeNull();
  });
});

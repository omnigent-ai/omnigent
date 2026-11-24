import { expect, it, vi } from "vitest";
import { createContentComponent, type ReactRenderer } from "@tiptap/react";

function renderer(label: string): ReactRenderer {
  const view: Pick<ReactRenderer, "reactElement" | "element"> = {
    reactElement: <span>{label}</span>,
    element: document.createElement("div"),
  };
  return view as ReactRenderer;
}

it.each(["getSnapshot", "getServerSnapshot"] as const)(
  "keeps published %s values immutable across batched writes",
  (read) => {
    const store = createContentComponent();
    const empty = Object.freeze(store[read]());
    store.setRenderer("first", renderer("original"));
    store.setRenderer("second", renderer("removed"));
    const before = Object.freeze(store[read]());
    expect(store.getSnapshot()).toBe(before);
    expect(store.getServerSnapshot()).toBe(before);

    store.setRenderer("first", renderer("updated"));
    store.setRenderer("third", renderer("added"));
    store.removeRenderer("second");
    const after = store[read]();
    expect(Object.keys(empty)).toEqual([]);
    expect(Object.keys(before)).toEqual(["first", "second"]);
    expect(before.first.children).toEqual(<span>original</span>);
    expect(Object.keys(after)).toEqual(["first", "third"]);
    expect(after.first.children).toEqual(<span>updated</span>);
    expect(store.getSnapshot()).toBe(after);
    expect(store.getServerSnapshot()).toBe(after);
  },
);

it("makes writes readable immediately while preserving batched notifications", async () => {
  const store = createContentComponent();
  const notify = vi.fn(() => store.getSnapshot());
  const unsubscribe = store.subscribe(notify);
  store.setRenderer("first", renderer("discarded"));
  store.setRenderer("second", renderer("retained"));
  store.removeRenderer("first");
  const snapshot = store.getSnapshot();
  expect(Object.keys(snapshot)).toEqual(["second"]);
  expect(notify).not.toHaveBeenCalled();
  await Promise.resolve();
  expect(notify).toHaveBeenCalledTimes(1);
  expect(notify.mock.results[0].value).toBe(snapshot);

  store.removeRenderer("second");
  unsubscribe();
  await Promise.resolve();
  expect(notify).toHaveBeenCalledTimes(1);
  expect(Object.keys(store.getSnapshot())).toEqual([]);
  expect(Object.keys(snapshot)).toEqual(["second"]);
});

it("retains renderer IDs that also name object prototype properties", () => {
  const store = createContentComponent();
  store.setRenderer("__proto__", renderer("first"));
  store.setRenderer("constructor", renderer("second"));
  const snapshot = store.getSnapshot();
  expect(Object.keys(snapshot)).toEqual(["__proto__", "constructor"]);
  store.removeRenderer("__proto__");
  expect(Object.keys(store.getSnapshot())).toEqual(["constructor"]);
  expect(Object.keys(snapshot)).toEqual(["__proto__", "constructor"]);
});

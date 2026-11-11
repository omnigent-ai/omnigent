import { it } from "node:test";
import assert from "node:assert/strict";

import { handle, on, push, type IpcMainHandlePort, type IpcMainOnPort } from "../src/ipc/main.ts";

interface FakeEvent {
  senderId: number;
}

type Registered = (event: FakeEvent, ...args: unknown[]) => unknown;

function fakeIpcMain() {
  const handlers = new Map<string, Registered>();
  const listeners = new Map<string, Registered>();
  const ipcMain: IpcMainHandlePort<FakeEvent> & IpcMainOnPort<FakeEvent> = {
    handle: (channel, listener) => {
      handlers.set(channel, listener);
    },
    on: (channel, listener) => {
      listeners.set(channel, listener);
    },
  };
  return { ipcMain, handlers, listeners };
}

it("registers a handler that gets the event and every renderer argument", async () => {
  const fake = fakeIpcMain();
  const seen: unknown[][] = [];
  handle(fake.ipcMain, "omnigent:reveal-file", (event, hostId, filePath) => {
    seen.push([event.senderId, hostId, filePath]);
    return typeof hostId === "string" && typeof filePath === "string";
  });

  const registered = fake.handlers.get("omnigent:reveal-file");
  assert.ok(registered);
  assert.equal(await registered({ senderId: 7 }, "host-1", "/a.txt"), true);
  assert.equal(await registered({ senderId: 7 }, 42), false);
  assert.deepEqual(seen, [
    [7, "host-1", "/a.txt"],
    [7, 42, undefined],
  ]);
});

it("registers one-way listeners and pushes to one renderer", () => {
  const fake = fakeIpcMain();
  const counts: unknown[] = [];
  on(fake.ipcMain, "omnigent:set-badge-count", (_event, count) => counts.push(count));
  fake.listeners.get("omnigent:set-badge-count")?.({ senderId: 1 }, 5);
  assert.deepEqual(counts, [5]);

  const sent: unknown[][] = [];
  push({ send: (channel, ...args) => sent.push([channel, ...args]) }, "omnigent:open-path", "/c/1");
  push({ send: (channel, ...args) => sent.push([channel, ...args]) }, "omnigent:find-opened");
  assert.deepEqual(sent, [["omnigent:open-path", "/c/1"], ["omnigent:find-opened"]]);
});

it("makes handlers narrow renderer arguments and return the contract result", () => {
  const fake = fakeIpcMain();
  handle(fake.ipcMain, "omnigent:switch-server", (_event, url) => {
    // @ts-expect-error -- a renderer argument is unknown until the handler narrows it.
    const forced: string = url;
    void forced;
  });
  // @ts-expect-error -- choose-directory resolves a path or null, not a number.
  handle(fake.ipcMain, "omnigent:choose-directory", () => 1);
  // @ts-expect-error -- a one-way message has no handler.
  handle(fake.ipcMain, "omnigent:set-badge-count", () => undefined);
  // @ts-expect-error -- the open-path push carries a string.
  push({ send: () => {} }, "omnigent:open-path", 1);
});

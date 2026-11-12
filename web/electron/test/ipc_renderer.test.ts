import { it } from "node:test";
import assert from "node:assert/strict";

import { createRendererIpc, type IpcRendererPort } from "../src/ipc/renderer.ts";

type Listener = Parameters<IpcRendererPort["on"]>[1];

function fakeIpcRenderer() {
  const calls: unknown[][] = [];
  const listeners = new Map<string, Set<Listener>>();
  const port: IpcRendererPort = {
    invoke: async (channel, ...args) => {
      calls.push(["invoke", channel, ...args]);
      return "/chosen";
    },
    send: (channel, ...args) => {
      calls.push(["send", channel, ...args]);
    },
    on: (channel, listener) => {
      listeners.set(channel, (listeners.get(channel) ?? new Set()).add(listener));
    },
    removeListener: (channel, listener) => {
      listeners.get(channel)?.delete(listener);
    },
  };
  const emit = (channel: string, ...args: unknown[]) => {
    for (const listener of listeners.get(channel) ?? []) listener({ sender: "event" }, ...args);
  };
  return { port, calls, listeners, emit };
}

it("invokes and sends with the channel and every argument", async () => {
  const fake = fakeIpcRenderer();
  const ipc = createRendererIpc(fake.port);

  assert.equal(await ipc.invoke("omnigent:choose-directory", "/home"), "/chosen");
  await ipc.invoke("omnigent:reveal-file", "host-1", "/a.txt");
  ipc.send("omnigent:set-badge-count", 3);
  ipc.send("omnigent:open-server-setup");

  assert.deepEqual(fake.calls, [
    ["invoke", "omnigent:choose-directory", "/home"],
    ["invoke", "omnigent:reveal-file", "host-1", "/a.txt"],
    ["send", "omnigent:set-badge-count", 3],
    ["send", "omnigent:open-server-setup"],
  ]);
});

it("passes pushes on without the IPC event and unsubscribes only its own listener", () => {
  const fake = fakeIpcRenderer();
  const ipc = createRendererIpc(fake.port);
  const first: string[] = [];
  const second: string[] = [];
  const unsubscribe = ipc.on("omnigent:open-path", (path) => first.push(path));
  ipc.on("omnigent:open-path", (path) => second.push(path));

  fake.emit("omnigent:open-path", "/c/one");
  unsubscribe();
  fake.emit("omnigent:open-path", "/c/two");

  assert.deepEqual(first, ["/c/one"]);
  assert.deepEqual(second, ["/c/one", "/c/two"]);
  assert.equal(fake.listeners.get("omnigent:open-path")?.size, 1);
});

it("rejects channels and arguments outside the contract at compile time", () => {
  const ipc = createRendererIpc(fakeIpcRenderer().port);
  // @ts-expect-error -- not a contract channel.
  void ipc.invoke("omnigent:not-a-channel");
  // @ts-expect-error -- a request channel cannot be sent one-way.
  ipc.send("omnigent:reveal-file", "host-1", "/a.txt");
  // @ts-expect-error -- reveal-file takes a host id and a path.
  void ipc.invoke("omnigent:reveal-file", "/a.txt");
  // @ts-expect-error -- the open-path push carries a string.
  ipc.on("omnigent:open-path", (path: number) => path);
});

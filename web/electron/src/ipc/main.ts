// Contract-typed IPC for the main process. Takes `ipcMain` and the target
// `webContents` as arguments, so modules using it need no electron import.

import type { InvokeChannels, PushChannels, SendChannels } from "./contract.ts";

/**
 * Arguments as a handler sees them: the contract's positions, each `unknown`.
 * Any renderer can send anything, so handlers must narrow.
 */
export type UntrustedArgs<Args extends unknown[]> = { [Index in keyof Args]: unknown };

/** The `ipcMain.handle` this helper registers through. */
export interface IpcMainHandlePort<Event> {
  handle: (channel: string, listener: (event: Event, ...args: unknown[]) => unknown) => void;
}

/** The `ipcMain.on` this helper registers through. */
export interface IpcMainOnPort<Event> {
  on: (channel: string, listener: (event: Event, ...args: unknown[]) => void) => unknown;
}

/** A renderer's `webContents`, as far as pushing to it goes. */
export interface PushTarget {
  send: (channel: string, ...args: unknown[]) => void;
}

/** Handle a contract request; the handler must resolve the contract's result. */
export function handle<Channel extends keyof InvokeChannels, Event>(
  ipcMain: IpcMainHandlePort<Event>,
  channel: Channel,
  handler: (
    event: Event,
    ...args: UntrustedArgs<InvokeChannels[Channel]["args"]>
  ) => InvokeChannels[Channel]["result"] | Promise<InvokeChannels[Channel]["result"]>,
): void {
  // A renderer may send fewer arguments; the missing ones arrive as undefined.
  ipcMain.handle(channel, (event, ...args) =>
    handler(event, ...(args as UntrustedArgs<InvokeChannels[Channel]["args"]>)),
  );
}

/** Listen for a contract one-way message. */
export function on<Channel extends keyof SendChannels, Event>(
  ipcMain: IpcMainOnPort<Event>,
  channel: Channel,
  listener: (event: Event, ...args: UntrustedArgs<SendChannels[Channel]["args"]>) => void,
): void {
  ipcMain.on(channel, (event, ...args) => {
    listener(event, ...(args as UntrustedArgs<SendChannels[Channel]["args"]>));
  });
}

/** Push a contract message to one renderer. */
export function push<Channel extends keyof PushChannels>(
  webContents: PushTarget,
  channel: Channel,
  ...args: PushChannels[Channel]["args"]
): void {
  webContents.send(channel, ...args);
}

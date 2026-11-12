// Contract-typed IPC for preloads. Bundled into each preload, so it takes
// `ipcRenderer` as an argument instead of importing electron.

import type { Unsubscribe } from "./bridge.ts";
import type { InvokeChannels, PushChannels, SendChannels } from "./contract.ts";

type RendererListener = (event: unknown, ...args: unknown[]) => void;

/** The parts of Electron's `ipcRenderer` the helper uses. */
export interface IpcRendererPort {
  invoke: (channel: string, ...args: unknown[]) => Promise<unknown>;
  send: (channel: string, ...args: unknown[]) => void;
  on: (channel: string, listener: RendererListener) => unknown;
  removeListener: (channel: string, listener: RendererListener) => unknown;
}

export interface RendererIpc {
  invoke: <Channel extends keyof InvokeChannels>(
    channel: Channel,
    ...args: InvokeChannels[Channel]["args"]
  ) => Promise<InvokeChannels[Channel]["result"]>;
  send: <Channel extends keyof SendChannels>(
    channel: Channel,
    ...args: SendChannels[Channel]["args"]
  ) => void;
  /** Subscribe to a push; the listener gets the payload without the IPC event. */
  on: <Channel extends keyof PushChannels>(
    channel: Channel,
    listener: (...args: PushChannels[Channel]["args"]) => void,
  ) => Unsubscribe;
}

export function createRendererIpc(ipcRenderer: IpcRendererPort): RendererIpc {
  return {
    // The contract states what main resolves; main is the trusted side.
    invoke: <Channel extends keyof InvokeChannels>(
      channel: Channel,
      ...args: InvokeChannels[Channel]["args"]
    ) => ipcRenderer.invoke(channel, ...args) as Promise<InvokeChannels[Channel]["result"]>,
    send: (channel, ...args) => {
      ipcRenderer.send(channel, ...args);
    },
    on: <Channel extends keyof PushChannels>(
      channel: Channel,
      listener: (...args: PushChannels[Channel]["args"]) => void,
    ) => {
      const forward: RendererListener = (_event, ...args) => {
        listener(...(args as PushChannels[Channel]["args"]));
      };
      ipcRenderer.on(channel, forward);
      return () => {
        ipcRenderer.removeListener(channel, forward);
      };
    },
  };
}

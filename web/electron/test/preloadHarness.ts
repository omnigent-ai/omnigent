// Runs a preload as the build ships it: bundles src/<name>.ts with
// scripts/build.mjs, then evaluates app/<name>.js in a fresh context whose only
// module is a recording stand-in for electron.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import vm from "node:vm";

/** One renderer → main call: its channel and every argument after it. */
export interface RecordedIpc {
  channel: string;
  args: unknown[];
}

type PushListener = (event: unknown, ...args: unknown[]) => void;

export interface PreloadEnvironment {
  /** `process.platform` inside the preload. */
  platform?: string;
  /** What main resolves for an invoke; null by default. */
  invoke?: (channel: string, args: unknown[]) => unknown;
  /** Page globals the preload reads, e.g. `window` and `document`. */
  globals?: Record<string, unknown>;
  /** Extra electron members, e.g. `webFrame` and `webUtils`. */
  electron?: Record<string, unknown>;
}

export interface PreloadRun {
  /** Each global the preload exposed, by name. */
  exposed: Map<string, unknown>;
  invokes: RecordedIpc[];
  sends: RecordedIpc[];
  /** Deliver a main → renderer push to the preload's listeners. */
  emit: (channel: string, ...args: unknown[]) => void;
  hasListener: (channel: string) => boolean;
}

const bundles = new Map<string, Promise<string>>();

/** The bundled preload's source, built once per test process. */
export function bundledPreloadSource(name: string): Promise<string> {
  let bundle = bundles.get(name);
  if (!bundle) {
    bundle = (async () => {
      const { bundlePreload } = await import("../scripts/build.mjs");
      const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-preload-"));
      try {
        await bundlePreload({ fileName: `${name}.ts`, outDir });
        return fs.readFileSync(path.join(outDir, `${name}.js`), "utf8");
      } finally {
        fs.rmSync(outDir, { recursive: true, force: true });
      }
    })();
    bundles.set(name, bundle);
  }
  return bundle;
}

export function runPreload(source: string, environment: PreloadEnvironment = {}): PreloadRun {
  const exposed = new Map<string, unknown>();
  const invokes: RecordedIpc[] = [];
  const sends: RecordedIpc[] = [];
  const listeners = new Map<string, Set<PushListener>>();
  const ipcRenderer = {
    invoke: async (channel: string, ...args: unknown[]) => {
      invokes.push({ channel, args });
      return environment.invoke ? environment.invoke(channel, args) : null;
    },
    send: (channel: string, ...args: unknown[]) => {
      sends.push({ channel, args });
    },
    on: (channel: string, listener: PushListener) => {
      const channelListeners = listeners.get(channel) ?? new Set();
      channelListeners.add(listener);
      listeners.set(channel, channelListeners);
      return ipcRenderer;
    },
    removeListener: (channel: string, listener: PushListener) => {
      listeners.get(channel)?.delete(listener);
      return ipcRenderer;
    },
  };
  const electron = {
    contextBridge: {
      exposeInMainWorld: (name: string, value: unknown) => {
        exposed.set(name, value);
      },
    },
    ipcRenderer,
    ...environment.electron,
  };
  vm.runInNewContext(source, {
    console,
    process: { platform: environment.platform ?? "linux" },
    ...environment.globals,
    require: (specifier: string) => {
      assert.equal(specifier, "electron");
      return electron;
    },
  });
  return {
    exposed,
    invokes,
    sends,
    emit: (channel, ...args) => {
      for (const listener of listeners.get(channel) ?? []) listener({}, ...args);
    },
    hasListener: (channel) => (listeners.get(channel)?.size ?? 0) > 0,
  };
}

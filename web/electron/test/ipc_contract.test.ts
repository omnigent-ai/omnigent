// Main-side modules register and push most channels with raw strings the compiler
// can't tie to src/ipc/contract.ts. This checks every main-side site against the
// contract both ways; a site it cannot classify fails the test.

import { it } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const SRC_DIR = path.join(import.meta.dirname, "..", "src");

type Direction = "invoke" | "send" | "push";

const CONTRACT_INTERFACES: Record<Direction, string> = {
  invoke: "InvokeChannels",
  send: "SendChannels",
  push: "PushChannels",
};

/**
 * Pushes whose channel is a parameter: `site` is the forwarding call in `file`;
 * its channels are the string literals passed to `callee` in `callerFile`.
 */
const FORWARDERS = [
  {
    file: "main.js",
    site: "win.webContents.send(channel, payload)",
    callerFile: "browserViewRegistry.ts",
    callee: "sendToRenderer",
  },
  {
    file: "browserIpc.js",
    site: "event.sender.send(channel, payload)",
    callerFile: "browserIpc.js",
    callee: "send",
  },
  {
    file: "arca_connect_window.js",
    site: "win.webContents.send(channel, payload)",
    callerFile: "arca_connect_window.js",
    callee: "send",
  },
  {
    file: "ssh_connect_window.js",
    site: "win.webContents.send(channel, payload)",
    callerFile: "ssh_connect_window.js",
    callee: "send",
  },
];

interface ChannelSite {
  direction: Direction;
  channel: string;
  where: string;
}

/** A string literal (group 1) or a comment; a quote or `//` in a regex literal can mislead it. */
const STRING_OR_COMMENT =
  /("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\[\s\S]|[^`\\])*`)|\/\*[\s\S]*?\*\/|\/\/[^\n]*/g;

/** `text` with everything but newlines turned into spaces, so offsets and line numbers hold. */
function blank(text: string): string {
  return text.replace(/[^\n]/g, " ");
}

/** Source with comments blanked (offsets kept); string literals stay intact. */
function liveCode(file: string): string {
  return fs
    .readFileSync(path.join(SRC_DIR, file), "utf8")
    .replace(STRING_OR_COMMENT, (match, literal?: string) => literal ?? blank(match));
}

function lineOf(source: string, index: number): number {
  return source.slice(0, index).split("\n").length;
}

/** The channel a call's argument names: a string literal, or a same-file string constant. */
function channelArgument(source: string, argument: string, where: string): string | null {
  const literal = /^"([^"]+)"$/.exec(argument);
  if (literal) return literal[1]!;
  if (/^[A-Za-z_$][\w$]*$/.test(argument)) {
    const constant = new RegExp(`\\bconst ${argument} = "([^"]+)";`).exec(source);
    if (constant) return constant[1]!;
  }
  if (argument === "channel") return null;
  assert.fail(`${where}: cannot classify the channel argument \`${argument}\``);
}

/** The first argument of each call matched by `callee`, with its location. */
function firstArguments(source: string, callee: RegExp, file: string) {
  return [...source.matchAll(callee)].map((match) => {
    const start = match.index + match[0].length;
    const argument = /^\s*([^,)]*?)\s*[,)]/.exec(source.slice(start))?.[1] ?? "";
    return { argument, where: `${file}:${lineOf(source, match.index)}`, index: match.index };
  });
}

/** Every main-process source file under src/, as a `/`-separated path; ipc/ holds the contract. */
function mainSideFiles(): string[] {
  return fs
    .readdirSync(SRC_DIR, { recursive: true, encoding: "utf8" })
    .map((file) => file.split(path.sep).join("/"))
    .filter(
      (file) =>
        /\.(js|ts)$/.test(file) && !file.startsWith("ipc/") && !/(^|[_/])preload\.ts$/.test(file),
    )
    .sort();
}

function mainSideSites(): ChannelSite[] {
  const sites: ChannelSite[] = [];
  for (const file of mainSideFiles()) {
    const source = liveCode(file);
    const registrations: [RegExp, Direction][] = [
      [/\bipcMain\.(?:handle|handleOnce)\(/g, "invoke"],
      [/\bipcMain\.(?:on|once|addListener)\(/g, "send"],
    ];
    for (const [callee, direction] of registrations) {
      for (const { argument, where } of firstArguments(source, callee, file)) {
        const channel = channelArgument(source, argument, where);
        if (channel === null) assert.fail(`${where}: registers a non-literal channel`);
        sites.push({ direction, channel, where });
      }
    }
    assert.doesNotMatch(source, /\bsendSync\(|\.sendTo\(/, `${file}: unexpected IPC call`);
    assert.doesNotMatch(
      source,
      /ipc\/main(\.[jt]s)?["']/,
      `${file}: extend this test for ipc/main.ts`,
    );

    const forwarders = FORWARDERS.filter((forwarder) => forwarder.file === file);
    const pushes = firstArguments(source, /\.send\(/g, file);
    let forwardingSites = 0;
    for (const { argument, where, index } of pushes) {
      const channel = channelArgument(source, argument, where);
      if (channel !== null) {
        sites.push({ direction: "push", channel, where });
        continue;
      }
      const forwarder = forwarders.find(({ site }) =>
        source.startsWith(site, index - site.indexOf(".send(")),
      );
      if (!forwarder) assert.fail(`${where}: pushes a non-literal channel outside FORWARDERS`);
      forwardingSites += 1;
      const callerSource = liveCode(forwarder.callerFile);
      const calls = firstArguments(
        callerSource,
        new RegExp(`(?<![.\\w])${forwarder.callee}\\(`, "g"),
        forwarder.callerFile,
      );
      assert.ok(calls.length > 0, `${forwarder.callerFile}: no ${forwarder.callee}() calls`);
      for (const call of calls) {
        const forwarded = channelArgument(callerSource, call.argument, call.where);
        if (forwarded === null) assert.fail(`${call.where}: forwards a non-literal channel`);
        sites.push({ direction: "push", channel: forwarded, where: call.where });
      }
    }
    assert.equal(forwardingSites, forwarders.length, `${file}: FORWARDERS entries without a site`);
  }
  return sites;
}

/** Channel names declared in each interface of contract.ts. */
function contractChannels(): Record<Direction, Set<string>> {
  const source = fs.readFileSync(path.join(SRC_DIR, "ipc", "contract.ts"), "utf8");
  const declared = (direction: Direction): Set<string> => {
    const name = CONTRACT_INTERFACES[direction];
    const body = new RegExp(`export interface ${name} \\{\\n([\\s\\S]*?)\\n\\}`).exec(source)?.[1];
    assert.ok(body, `contract.ts declares no ${name}`);
    const channels = new Set([...body.matchAll(/^ {2}"([^"]+)":/gm)].map((match) => match[1]!));
    assert.ok(channels.size > 0, `${name} lists no channels`);
    return channels;
  };
  return { invoke: declared("invoke"), send: declared("send"), push: declared("push") };
}

it("declares every main-side channel in the contract with its direction", () => {
  const sites = mainSideSites();
  const contract = contractChannels();
  const misplaced = sites
    .filter(({ direction, channel }) => !contract[direction].has(channel))
    .map(({ direction, channel, where }) => `${where}: ${direction} ${channel}`);
  assert.deepEqual(misplaced, []);
});

it("registers or pushes every contract channel in the main process", () => {
  const sites = mainSideSites();
  const contract = contractChannels();
  for (const direction of ["invoke", "send", "push"] as const) {
    const used = new Set(
      sites.filter((site) => site.direction === direction).map((site) => site.channel),
    );
    const unused = [...contract[direction]].filter((channel) => !used.has(channel));
    assert.deepEqual(unused, [], `${CONTRACT_INTERFACES[direction]} channels main never uses`);
  }
});

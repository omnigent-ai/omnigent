// No pull request call goes to this Mac's shell: the page reads and changes pull requests through
// the session's host. The GitHub fast track goes only through `githubFastTrackBridge.ts`, which
// closes when the page's server moves. This scans static imports, re-exports, dynamic imports and
// window reads; it does not follow a bridge function handed on as a value.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BRIDGE = path.join(SRC, "lib/nativeBridgeCodeHost.ts");
// The bridge itself, which declares every member.
const EXEMPT = new Set(["lib/nativeBridge.ts", "lib/nativeBridgeCodeHost.ts"]);
// The Mac read path's own bridge module in lib, which no page code imports or ships with.
const MAC_READ_PATH_MODULE = "macBridge.ts";
const FAST_TRACK_GATE = "lib/githubFastTrackBridge.ts";
// What only the fast-track gate may take from the bridge, and the shell member they call.
const FAST_TRACK_EXPORTS = new Set(["hasGitHubFastTrackBridge", "githubFastTrackQuery"]);
const FAST_TRACK_MEMBERS = new Set(["githubQuery"]);

// Code-host bridge exports that make no pull request call this Mac answers for a server.
const ALLOWED_EXPORTS = new Set([
  // Constants.
  "CODE_HOST_READ_CANCELLED",
  "CODE_HOST_WRITE_OUTCOME_UNKNOWN",
  // Diagnostics sent one way, the release flags, and signing in to GitHub.
  "reportPullRequestTiming",
  "reportPullRequestDialog",
  "reportPullRequestCommentWrites",
  "featureFlagBridge",
  "githubSignInBridge",
  "isGitHubSignInAvailable",
  // The workspace fast track reads a session's files, not pull requests.
  "onWorkspaceAccessReady",
  "hasCancelWorkspaceRequestBridge",
  "cancelWorkspaceRequest",
  "hasWorkspaceContentSearchBridge",
  "workspaceContentSearch",
  "hasWorkspaceFastTrackBridge",
  "workspaceFastTrackRequest",
  "hasWorkspaceSessionRouteBridge",
  "workspaceSessionRoute",
  "hasFastTrackRetryBridge",
  "workspaceFastTrackRetry",
  "hasHostKeyTrustBridge",
  "workspaceHostKeyStatus",
  "workspaceHostKeyReview",
  "workspaceTrustHostKey",
]);

// The shell's members those allowed exports call.
const ALLOWED_MEMBERS = new Set([
  "reportPullRequestTiming",
  "reportPullRequestDialog",
  "reportPullRequestCommentWrites",
  "featureFlags",
  "codeHostSignIn",
  "workspaceRequest",
  "workspaceContentSearch",
  "cancelWorkspaceRequest",
  "workspaceSessionRoute",
  "workspaceFastTrackRetry",
  "workspaceHostKeyStatus",
  "workspaceHostKeyReview",
  "workspaceTrustHostKey",
  "onWorkspaceAccessReady",
]);

const withoutComments = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");

const bridgeSource = withoutComments(fs.readFileSync(BRIDGE, "utf8"));

function bridgeExports(): string[] {
  return [...bridgeSource.matchAll(/^export (?:async )?(?:function|const|let) (\w+)/gm)].map(
    (match) => match[1]!,
  );
}

/** The top-level members of the shell's code-host API, as `CodeHostDesktopApi` declares them. */
function bridgeMembers(): string[] {
  const start = bridgeSource.indexOf("export interface CodeHostDesktopApi {");
  let depth = 0;
  let end = start;
  for (let index = start; index < bridgeSource.length; index += 1) {
    if (bridgeSource[index] === "{") depth += 1;
    if (bridgeSource[index] === "}" && --depth === 0) {
      end = index;
      break;
    }
  }
  return [...bridgeSource.slice(start, end).matchAll(/^ {2}(\w+)\??[:(]/gm)].map((m) => m[1]!);
}

const FORBIDDEN_EXPORTS = new Set(bridgeExports().filter((name) => !ALLOWED_EXPORTS.has(name)));
const FORBIDDEN_MEMBERS = bridgeMembers().filter((name) => !ALLOWED_MEMBERS.has(name));
// The fast-track gate may take the fast track, and nothing else this scan forbids.
const GATE_ALLOWED = new Set([...FAST_TRACK_EXPORTS, ...FAST_TRACK_MEMBERS]);
const BRIDGE_PATH = String.raw`"[^"]*nativeBridge(?:CodeHost)?"`;
const FROM_BRIDGE = String.raw`from\s+${BRIDGE_PATH}`;

/**
 * What `source` takes from the bridge that only the gate may: static imports and re-exports by
 * name, a namespace's uses, a dynamic import, and code-host members read off the window.
 */
function bypasses(source: string, allowed: ReadonlySet<string> = new Set()): string[] {
  const code = withoutComments(source);
  const found: string[] = [];
  for (const match of code.matchAll(
    new RegExp(String.raw`(import|export)\s+(type\s+)?\{([^}]*)\}\s+${FROM_BRIDGE}`, "g"),
  )) {
    if (match[2]) continue;
    for (const specifier of match[3]!.split(",")) {
      const name = specifier.trim().split(/\s+as\s+/)[0]!;
      if (FORBIDDEN_EXPORTS.has(name) && !allowed.has(name)) found.push(`${match[1]}s ${name}`);
    }
  }
  if (new RegExp(String.raw`export\s+\*(?:\s+as\s+\w+)?\s+${FROM_BRIDGE}`).test(code)) {
    found.push("re-exports the whole bridge");
  }
  if (new RegExp(String.raw`import\(\s*${BRIDGE_PATH}\s*\)`).test(code)) {
    found.push("imports the bridge dynamically");
  }
  for (const match of code.matchAll(
    new RegExp(String.raw`import\s+\*\s+as\s+(\w+)\s+${FROM_BRIDGE}`, "g"),
  )) {
    for (const use of code.matchAll(new RegExp(String.raw`\b${match[1]}\.(\w+)`, "g"))) {
      if (FORBIDDEN_EXPORTS.has(use[1]!) && !allowed.has(use[1]!)) found.push(`imports ${use[1]}`);
    }
  }
  if (/\bomnigentDesktop\b/.test(code)) {
    for (const member of FORBIDDEN_MEMBERS) {
      if (allowed.has(member)) continue;
      if (new RegExp(String.raw`\b${member}\b`).test(code))
        found.push(`reads ${member} off the window`);
    }
  }
  return found;
}

function productionFiles(): string[] {
  return fs
    .readdirSync(SRC, { recursive: true, encoding: "utf8" })
    .filter((file) => /\.(ts|tsx)$/.test(file) && !/\.(test|stories)\.tsx?$/.test(file))
    .filter((file) => !file.endsWith(".d.ts") && !file.split(path.sep).includes("test"))
    .filter((file) => !EXEMPT.has(file.split(path.sep).join("/")))
    .filter((file) => file.split(path.sep).join("/") !== `lib/${MAC_READ_PATH_MODULE}`);
}

describe("calls to this Mac's shell for pull requests", () => {
  it("never happen, and the fast track goes only through its gate", () => {
    const found = productionFiles().flatMap((file) => {
      const name = file.split(path.sep).join("/");
      const allowed = name === FAST_TRACK_GATE ? GATE_ALLOWED : new Set<string>();
      return bypasses(fs.readFileSync(path.join(SRC, file), "utf8"), allowed).map(
        (what) => `${file}: ${what}`,
      );
    });
    expect(found).toEqual([]);
  });

  it("lets the fast-track gate take the fast track only", () => {
    expect(
      bypasses(`import { githubFastTrackQuery } from "@/lib/nativeBridge";`, GATE_ALLOWED),
    ).toEqual([]);
    expect(
      bypasses(`import { desktopCodeHostQuery } from "@/lib/nativeBridge";`, GATE_ALLOWED),
    ).toEqual(["imports desktopCodeHostQuery"]);
    expect(bypasses(`import { githubFastTrackQuery } from "@/lib/nativeBridge";`)).toEqual([
      "imports githubFastTrackQuery",
    ]);
  });

  it("cover every bridge export and member the allowed lists don't name", () => {
    const exports = new Set(bridgeExports());
    const members = new Set(bridgeMembers());
    expect([...ALLOWED_EXPORTS].filter((name) => !exports.has(name))).toEqual([]);
    expect([...ALLOWED_MEMBERS].filter((name) => !members.has(name))).toEqual([]);
    expect([...FORBIDDEN_EXPORTS]).toEqual(
      expect.arrayContaining([
        "desktopCodeHostQuery",
        "desktopLocalPullRequestLinks",
        "githubFastTrackQuery",
      ]),
    );
    expect(FORBIDDEN_MEMBERS).toEqual(
      expect.arrayContaining(["codeHostQuery", "githubQuery", "localPullRequestLinks"]),
    );
  });

  it("would catch a re-export, a whole re-export and a dynamic import", () => {
    expect(bypasses(`export { desktopCodeHostQuery } from "@/lib/nativeBridge";`)).toEqual([
      "exports desktopCodeHostQuery",
    ]);
    expect(bypasses(`export * from "@/lib/nativeBridge";`)).toEqual([
      "re-exports the whole bridge",
    ]);
    expect(bypasses(`const bridge = await import("@/lib/nativeBridge");`)).toEqual([
      "imports the bridge dynamically",
    ]);
  });

  it("would catch a named import, a renamed one, a namespace use and a window read", () => {
    expect(
      bypasses(
        `import { isElectronShell, desktopLocalPullRequestLinks } from "@/lib/nativeBridge";`,
      ),
    ).toEqual(["imports desktopLocalPullRequestLinks"]);
    expect(bypasses(`import { hasCodeHostBridge as has } from "./nativeBridge";`)).toEqual([
      "imports hasCodeHostBridge",
    ]);
    expect(
      bypasses(`import * as bridge from "@/lib/nativeBridge";\nbridge.githubFastTrackQuery("x");`),
    ).toEqual(["imports githubFastTrackQuery"]);
    expect(
      bypasses(`const api = (window as any).omnigentDesktop;\napi.codeHostQuery({});`),
    ).toEqual(["reads codeHostQuery off the window"]);
  });

  it("allows types, allowed exports and other window members", () => {
    expect(bypasses(`import type { LocalPullRequestLink } from "@/lib/nativeBridge";`)).toEqual([]);
    expect(
      bypasses(
        `import { type CodeHostQueryRequest, featureFlagBridge } from "@/lib/nativeBridge";`,
      ),
    ).toEqual([]);
    expect(bypasses(`const tabs = (window as any).omnigentDesktop?.browserTabs;`)).toEqual([]);
  });
});

/**
 * Locate the `isaac` launcher for desktop host enrollment.
 * GUI-launched Electron inherits a minimal PATH, so mirror the Omnigent/Arca
 * resolver pattern: PATH first, then well-known macOS install locations.
 */

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export function candidatePaths(): string[] {
  const home = os.homedir();
  return [
    "/usr/local/bin/isaac",
    "/opt/homebrew/bin/isaac",
    path.join(home, ".local", "bin", "isaac"),
  ];
}

function isExecutableFile(value: string): boolean {
  try {
    if (!fs.statSync(value).isFile()) return false;
    fs.accessSync(value, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function whichIsaac(): string | null {
  try {
    const out = execFileSync("/bin/sh", ["-c", "command -v isaac"], { encoding: "utf8" });
    return out.trim() || null;
  } catch {
    return null;
  }
}

/** Probes {@link resolveIsaacPath} uses; each defaults to the real one. */
export interface ResolveIsaacPathDeps {
  isExecutableFile?: ((value: string) => boolean) | undefined;
  whichIsaac?: (() => string | null) | undefined;
  candidatePaths?: (() => string[]) | undefined;
}

export function resolveIsaacPath(deps: ResolveIsaacPathDeps = {}): string | null {
  const isExec = deps.isExecutableFile || isExecutableFile;
  const onPath = (deps.whichIsaac || whichIsaac)();
  if (onPath && isExec(onPath)) return onPath;
  for (const candidate of (deps.candidatePaths || candidatePaths)()) {
    if (isExec(candidate)) return candidate;
  }
  return null;
}

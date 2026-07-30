/**
 * Thin VS Code adapter for the pure `Settings` interface. This is the ONLY place
 * the config layer touches the vscode API; everything else is pure and testable
 * without an IDE host. Not exercised by unit tests (no vscode host).
 */
import * as vscode from "vscode";
import type { Settings } from "./index";

export function readSettings(): Settings {
  const cfg = vscode.workspace.getConfiguration("omnigent");
  return {
    serverUrl: cfg.get<string>("serverUrl", ""),
  };
}

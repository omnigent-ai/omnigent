/**
 * Tests for SessionsTreeProvider's getChildren node arrays across each state,
 * driven through the injected getClientOpts + an injected fetchImpl stub.
 */
import { describe, it, expect } from "vitest";
import * as vscode from "vscode";
import { SessionsTreeProvider, type SessionsNode } from "./SessionsTreeProvider";
import type { ClientOptions, Session, SessionsPage } from "../api/client";

const output = { appendLine: () => {} } as unknown as vscode.OutputChannel;

function pageFetch(data: Session[]): ClientOptions {
  const body: SessionsPage = { object: "list", data, has_more: false };
  const fetchImpl = (async () =>
    ({ ok: true, status: 200, json: async () => body }) as Response) as unknown as typeof fetch;
  return { baseUrl: "http://127.0.0.1:6767", fetchImpl };
}

function labels(nodes: SessionsNode[]): string[] {
  return nodes.map((n) => (n.kind === "message" ? n.label : n.session.id));
}

describe("SessionsTreeProvider.getChildren", () => {
  it("starts in loading before any refresh", () => {
    const p = new SessionsTreeProvider(() => undefined, output);
    expect(labels(p.getChildren())).toEqual(["Loading…"]);
  });

  it("shows the no-server message when client options are undefined", async () => {
    const p = new SessionsTreeProvider(() => undefined, output);
    await p.refresh();
    expect(labels(p.getChildren())).toEqual(["Omnigent server unreachable"]);
  });

  it("shows an error message when the list request fails", async () => {
    const fetchImpl = (async () => {
      throw new Error("ECONNREFUSED");
    }) as unknown as typeof fetch;
    const p = new SessionsTreeProvider(
      () => ({ baseUrl: "http://127.0.0.1:6767", fetchImpl }),
      output,
    );
    await p.refresh();
    expect(labels(p.getChildren())).toEqual(["Omnigent server unreachable"]);
  });

  it("shows 'No sessions' for an empty ready list", async () => {
    const p = new SessionsTreeProvider(() => pageFetch([]), output);
    await p.refresh();
    expect(labels(p.getChildren())).toEqual(["No sessions"]);
  });

  it("returns session nodes sorted by updated_at desc when ready", async () => {
    const p = new SessionsTreeProvider(
      () =>
        pageFetch([
          { id: "old", updated_at: 100 },
          { id: "new", updated_at: 200 },
        ]),
      output,
    );
    await p.refresh();
    expect(labels(p.getChildren())).toEqual(["new", "old"]);
  });

  it("builds a TreeItem with the open-from-tree command for a session node", async () => {
    const p = new SessionsTreeProvider(
      () => pageFetch([{ id: "conv_1", title: "Hi", status: "running" }]),
      output,
    );
    await p.refresh();
    const [node] = p.getChildren();
    const item = p.getTreeItem(node) as unknown as {
      id: string;
      command: { command: string; arguments: string[] };
      contextValue: string;
    };
    expect(item.id).toBe("conv_1");
    expect(item.command.command).toBe("omnigent.openSessionFromTree");
    expect(item.command.arguments).toEqual(["conv_1"]);
    expect(item.contextValue).toBe("omnigentSession");
  });
});

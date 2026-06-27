// Tests for the custom-agent composer dialog's smoke-test (dry-run validate)
// affordance. Mocks the bundle builder + the control-plane validate client.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentTestResult } from "@/lib/controlPlaneApi";

const mocks = vi.hoisted(() => ({
  buildAgentBundle: vi.fn(),
  validateAgentBundle: vi.fn(),
}));

vi.mock("@/lib/agentBundle", async (orig) => {
  const actual = (await orig()) as object;
  return { ...actual, buildAgentBundle: mocks.buildAgentBundle };
});
vi.mock("@/lib/controlPlaneApi", () => ({ validateAgentBundle: mocks.validateAgentBundle }));

import { CreateAgentDialog } from "./CreateAgentDialog";

function renderDialog(onCreate = vi.fn()) {
  render(<CreateAgentDialog open onOpenChange={vi.fn()} onCreate={onCreate} />);
  return { onCreate };
}

function fillRequired() {
  fireEvent.change(screen.getByTestId("create-agent-name"), { target: { value: "my-agent" } });
  fireEvent.change(screen.getByTestId("create-agent-model"), { target: { value: "gpt-4o-mini" } });
}

beforeEach(() => {
  mocks.buildAgentBundle.mockReset();
  mocks.validateAgentBundle.mockReset();
  mocks.buildAgentBundle.mockResolvedValue(new File(["x"], "agent.tar.gz"));
});
afterEach(cleanup);

describe("CreateAgentDialog smoke test", () => {
  it("Test is disabled until name + model are provided", () => {
    renderDialog();
    expect((screen.getByTestId("create-agent-test") as HTMLButtonElement).disabled).toBe(true);
    fillRequired();
    expect((screen.getByTestId("create-agent-test") as HTMLButtonElement).disabled).toBe(false);
  });

  it("builds the bundle and shows ✓ per-check on a valid result", async () => {
    const result: AgentTestResult = {
      ok: true,
      agent_id: null,
      harness: "openai-agents",
      model: "gpt-4o-mini",
      mcp_server_count: 0,
      checks: [{ name: "bundle_valid", ok: true, detail: "harness=openai-agents" }],
    };
    mocks.validateAgentBundle.mockResolvedValue({ ok: true, result });
    renderDialog();
    fillRequired();
    fireEvent.click(screen.getByTestId("create-agent-test"));
    await waitFor(() => expect(mocks.buildAgentBundle).toHaveBeenCalled());
    await waitFor(() => expect(mocks.validateAgentBundle).toHaveBeenCalled());
    const box = await screen.findByTestId("create-agent-test-result");
    expect(box.textContent).toContain("Bundle looks valid");
    expect(box.textContent).toContain("bundle_valid");
  });

  it("shows ✗ when the bundle is invalid", async () => {
    mocks.validateAgentBundle.mockResolvedValue({
      ok: true,
      result: {
        ok: false,
        agent_id: null,
        harness: null,
        model: null,
        mcp_server_count: null,
        checks: [{ name: "bundle_valid", ok: false, detail: "invalid bundle: boom" }],
      },
    });
    renderDialog();
    fillRequired();
    fireEvent.click(screen.getByTestId("create-agent-test"));
    const box = await screen.findByTestId("create-agent-test-result");
    expect(box.textContent).toContain("Bundle has problems");
  });

  it("surfaces a friendly message when validation isn't available (404)", async () => {
    mocks.validateAgentBundle.mockResolvedValue({ ok: false, error: "Not found.", status: 404 });
    renderDialog();
    fillRequired();
    fireEvent.click(screen.getByTestId("create-agent-test"));
    const box = await screen.findByTestId("create-agent-test-result");
    expect(box.textContent).toContain("isn't available in this deployment");
  });

  it("Test does not create the agent (advisory only)", async () => {
    mocks.validateAgentBundle.mockResolvedValue({
      ok: true,
      result: {
        ok: true,
        agent_id: null,
        harness: "openai-agents",
        model: "gpt-4o-mini",
        mcp_server_count: 0,
        checks: [{ name: "bundle_valid", ok: true, detail: "" }],
      },
    });
    const { onCreate } = renderDialog();
    fillRequired();
    fireEvent.click(screen.getByTestId("create-agent-test"));
    await screen.findByTestId("create-agent-test-result");
    expect(onCreate).not.toHaveBeenCalled();
  });

  it("passes the Databricks profile through to onCreate", () => {
    const { onCreate } = renderDialog();
    fireEvent.change(screen.getByTestId("create-agent-name"), { target: { value: "db-agent" } });
    fireEvent.change(screen.getByTestId("create-agent-model"), {
      target: { value: "databricks-claude-opus-4-8" },
    });
    fireEvent.change(screen.getByTestId("create-agent-profile"), { target: { value: "DEFAULT" } });
    fireEvent.click(screen.getByTestId("create-agent-submit"));
    expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({ profile: "DEFAULT" }));
  });

  it("omits profile from onCreate when left blank", () => {
    const { onCreate } = renderDialog();
    fillRequired();
    fireEvent.click(screen.getByTestId("create-agent-submit"));
    expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({ profile: undefined }));
  });
});

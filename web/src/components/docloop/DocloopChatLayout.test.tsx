import { useEffect } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { DocloopChatLayout } from "./DocloopChatLayout";

const fetcher = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetcher }));

function mount(enabled = true) {
  const mounts = vi.fn();
  const unmounts = vi.fn();
  function Chat() {
    const navigate = useNavigate();
    useEffect(() => {
      mounts();
      return () => unmounts();
    }, []);
    return (
      <div>
        <textarea aria-label="Message" defaultValue="Keep this instruction" />
        <button type="button" onClick={() => navigate("/c/second")}>
          Switch session
        </button>
      </div>
    );
  }
  const element = (
    <DocloopChatLayout>
      <Chat />
    </DocloopChatLayout>
  );
  const view = render(
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { docloop_notebook: enabled } }}
    >
      <MemoryRouter initialEntries={["/c/first"]}>
        <Routes>
          <Route path="/c/:conversationId" element={element} />
        </Routes>
      </MemoryRouter>
    </CapabilitiesProvider>,
  );
  return { ...view, mounts, unmounts };
}

describe("native chat notebook pane", () => {
  it("leaves chat alone when disabled", () => {
    fetcher.mockClear();
    const view = mount(false);
    expect(screen.queryByRole("button", { name: "Notebook" })).toBeNull();
    expect(screen.getByLabelText("Message")).toHaveValue("Keep this instruction");
    expect(fetcher).not.toHaveBeenCalled();
    view.unmount();
  });

  it("preserves chat, its draft and instance across pane toggles and session changes", async () => {
    fetcher.mockImplementation(
      async (url: string) =>
        new Response(
          JSON.stringify({
            schema_version: 1,
            session_id: url.includes("/first/") ? "first" : "second",
            revision: "a".repeat(64),
            binding_id: "b".repeat(64),
            format: "org",
            document_name: "agent.org",
            nodes: [],
            capabilities: { create_node: true, edit_source: true, direct_execution: false },
          }),
          { status: 200 },
        ),
    );
    const view = mount();
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Edited instruction" } });
    fireEvent.click(screen.getByRole("button", { name: "Notebook" }));
    await waitFor(() =>
      expect(fetcher).toHaveBeenCalledWith(
        "/v1/sessions/first/docloop/document",
        expect.any(Object),
      ),
    );
    expect(screen.getByRole("button", { name: "Notebook" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    fireEvent.click(screen.getByRole("button", { name: "Notebook" }));
    fireEvent.click(screen.getByRole("button", { name: "Switch session" }));
    await waitFor(() =>
      expect(fetcher).toHaveBeenCalledWith(
        "/v1/sessions/second/docloop/document",
        expect.any(Object),
      ),
    );
    expect(screen.getByLabelText("Message")).toHaveValue("Edited instruction");
    expect(view.mounts).toHaveBeenCalledTimes(1);
    expect(view.unmounts).not.toHaveBeenCalled();
    view.unmount();
    expect(view.unmounts).toHaveBeenCalledTimes(1);
  });
});

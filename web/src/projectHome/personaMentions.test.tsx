import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/projectsApi";
import { MentionTextarea } from "./MentionTextarea";
import {
  emphasizeMentions,
  insertMention,
  mentionQueryAt,
  payloadMentions,
} from "./personaMentions";

vi.mock("@/lib/projectsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  listProjectIdentities: vi.fn(),
}));

function identity(id: string, name: string, builtin = false): api.ProjectIdentity {
  return {
    id,
    project_id: "project",
    name,
    builtin,
    instructions: "",
    agent_id: null,
    model: null,
    effort: null,
    active_session_id: null,
    active_session_state: "absent",
  };
}

afterEach(cleanup);

describe("persona mention helpers", () => {
  it("finds the @query at the caret and inserts the chosen name", () => {
    expect(mentionQueryAt("cc @p", 5)).toEqual({ start: 3, query: "p" });
    expect(mentionQueryAt("mail a@b", 8)).toBeNull();
    expect(insertMention("cc @p rest", 5, 3, "PM")).toEqual({ text: "cc @PM  rest", caret: 7 });
  });

  it("bolds recorded mentions outside code and reads them from a payload", () => {
    const mentions = payloadMentions({ mentions: [{ id: "pm", name: "PM" }, { bad: 1 }] });
    expect(mentions).toEqual([{ id: "pm", name: "PM" }]);
    expect(emphasizeMentions("cc @pm and `@PM` and @PMs", mentions)).toBe(
      "cc **@pm** and `@PM` and @PMs",
    );
  });
});

describe("MentionTextarea", () => {
  it("offers the project's personas after @ and inserts the chosen one", async () => {
    vi.mocked(api.listProjectIdentities).mockResolvedValue([
      identity("orchestrator", "Orchestrator", true),
      identity("pm", "PM"),
      identity("designer", "Designer"),
    ]);
    function Harness() {
      const [value, setValue] = useState("");
      return (
        <MentionTextarea
          projectId="project"
          aria-label="Add a comment"
          value={value}
          onValueChange={setValue}
        />
      );
    }
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Harness />
      </QueryClientProvider>,
    );
    const input = screen.getByLabelText("Add a comment") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "cc @", selectionStart: 4 } });
    const all = await screen.findAllByTestId("persona-mention-option");
    expect(all.map((option) => option.textContent)).toEqual([
      "Orchestratorbuilt in",
      "PM",
      "Designer",
    ]);
    fireEvent.change(input, { target: { value: "cc @de", selectionStart: 6 } });
    expect(screen.getAllByTestId("persona-mention-option").map((o) => o.textContent)).toEqual([
      "Designer",
    ]);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(input.value).toBe("cc @Designer ");
    expect(screen.queryByTestId("persona-mention-suggestions")).toBeNull();
  });
});

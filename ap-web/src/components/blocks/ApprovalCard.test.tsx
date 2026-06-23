import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { ApprovalCard } from "./ApprovalCard";

afterEach(() => {
  cleanup();
});

describe("ApprovalCard — binary approve/reject", () => {
  it("renders Approve and Reject buttons when requestedSchema has no enum", () => {
    // Policy-ASK and PermissionRequest cards arrive with an empty
    // schema (binary decision). The card should render the
    // existing two-button layout.
    render(
      <ApprovalCard
        elicitationId="elic_x"
        message="Approve running rm -rf /tmp/cache?"
        phase="tool_call"
        policyName="approve_shell_commands"
        contentPreview="rm -rf /tmp/cache"
        requestedSchema={{}}
        status="pending"
        response={null}
      />,
    );

    expect(screen.getByRole("button", { name: /approve/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDefined();
    expect(screen.queryByTestId("approval-card-options")).toBeNull();
  });

  it("renders Codex command approvals from structured extras instead of raw JSON", () => {
    // Codex command approval frames carry internal correlation ids in
    // content_preview. The card should show only user-relevant command
    // details; otherwise the web prompt becomes a raw transport dump.
    render(
      <ApprovalCard
        elicitationId="elic_cmd"
        message="Codex wants to run **/bin/zsh -lc '.venv/bin/python -m pytest -q'**"
        phase="codex_command_approval"
        policyName="codex_native_command_approval"
        contentPreview={JSON.stringify({
          threadId: "thread_123",
          turnId: "turn_123",
          itemId: "item_cmd",
          commandActions: [{ type: "unknown", command: "pytest" }],
          command: "/bin/zsh -lc '.venv/bin/python -m pytest -q'",
        })}
        requestedSchema={{}}
        status="pending"
        response={null}
        codexCommand={{
          command: "/bin/zsh -lc '.venv/bin/python -m pytest -q'",
          cwd: "/Users/example/project",
          reason: "Run the focused pytest suite.",
          execPolicyAmendment: null,
        }}
      />,
    );

    expect(screen.getByText("Command approval")).toBeDefined();
    expect(screen.getByText("Codex wants to run this command.")).toBeDefined();
    expect(screen.getByText("Run the focused pytest suite.")).toBeDefined();
    expect(screen.getByText("/bin/zsh -lc '.venv/bin/python -m pytest -q'")).toBeDefined();
    expect(screen.getByText("/Users/example/project")).toBeDefined();
    expect(screen.queryByText(/thread_123/)).toBeNull();
    expect(screen.queryByText(/commandActions/)).toBeNull();
    expect(screen.queryByText(/\*\*\/bin\/zsh/)).toBeNull();
    expect(screen.getByTestId("codex-command-actions")).toBeDefined();
    expect(screen.getByRole("button", { name: /approve/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDefined();
  });

  it("submits Codex execpolicy amendments when the remember option is clicked", () => {
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);

    render(
      <ApprovalCard
        elicitationId="elic_cmd_policy"
        message="Codex wants to run pytest"
        phase="codex_command_approval"
        policyName="codex_native_command_approval"
        contentPreview=""
        requestedSchema={{}}
        status="pending"
        response={null}
        codexCommand={{
          command: ".venv/bin/python -m pytest -q",
          cwd: "/Users/example/project",
          reason: "Run the focused pytest suite.",
          execPolicyAmendment: [".venv/bin/python", "-m", "pytest"],
        }}
      />,
    );

    const rememberButton = screen.getByRole("button", { name: /approve and remember/i });
    expect(rememberButton.getAttribute("data-variant")).toBe("outline");

    fireEvent.click(rememberButton);

    expect(submitSpy).toHaveBeenCalledWith("elic_cmd_policy", "accept", {
      execpolicy_amendment: [".venv/bin/python", "-m", "pytest"],
    });
  });
});

describe("ApprovalCard — accept & allow all edits", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_abc", blocks: [] });
  });

  it("renders the third button only when allowAllEdits is set", () => {
    // The button must appear exactly when the server stamped the
    // edit-tool hint. If it leaked onto every binary card, users on
    // non-edit / non-claude-native prompts would see an action that
    // can't switch acceptEdits mode (a no-op).
    const { rerender } = render(
      <ApprovalCard
        elicitationId="elic_edit"
        message="Claude wants to call **Edit**"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview="Edit({})"
        requestedSchema={{}}
        status="pending"
        response={null}
        allowAllEdits={true}
      />,
    );
    expect(screen.getByRole("button", { name: /accept & allow all edits/i })).toBeDefined();
    // Approve and Reject still flank it.
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDefined();

    // Without the hint (the default for Bash, codex, claude-sdk, etc.)
    // the third button is gone — proving the gating, not just that the
    // button can render.
    rerender(
      <ApprovalCard
        elicitationId="elic_bash"
        message="Claude wants to call **Bash**"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview="Bash({})"
        requestedSchema={{}}
        status="pending"
        response={null}
      />,
    );
    expect(screen.queryByRole("button", { name: /accept & allow all edits/i })).toBeNull();
  });

  it("submits {action: 'accept', content: {allow_all_edits: true}} on click", () => {
    // The server reads ``content.allow_all_edits`` to emit the
    // ``setMode`` permission update. A wrong content shape here means
    // the session never switches to acceptEdits and the button
    // silently degrades to a plain Approve.
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);

    render(
      <ApprovalCard
        elicitationId="elic_edit_click"
        message="Claude wants to call **Write**"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview="Write({})"
        requestedSchema={{}}
        status="pending"
        response={null}
        allowAllEdits={true}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /accept & allow all edits/i }));

    expect(submitSpy).toHaveBeenCalledWith("elic_edit_click", "accept", {
      allow_all_edits: true,
    });
  });

  it("renders the auto-accepting-edits label in the responded state", () => {
    // After accepting via the button, the store stamps
    // ``content.allow_all_edits`` so the responded pill reflects that
    // the session switched to auto-accept-edits — distinct from a
    // plain "Approved".
    render(
      <ApprovalCard
        elicitationId="elic_edit_done"
        message="Claude wants to call **Edit**"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview="Edit({})"
        requestedSchema={{}}
        status="responded"
        response={{ action: "accept", content: { allow_all_edits: true } }}
        allowAllEdits={true}
      />,
    );

    expect(screen.getByText(/auto-accepting edits/i)).toBeDefined();
  });
});

describe("ApprovalCard — approve & don't ask again (persistent allow rule)", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_abc", blocks: [] });
  });

  it("labels the remember button by the WebFetch host and hides it without the hint", () => {
    // The server stamps ``remember_scope`` only for non-edit tools.
    // For WebFetch the button names the domain so the user knows the
    // rule is domain-scoped, not tool-wide.
    const { rerender } = render(
      <ApprovalCard
        elicitationId="elic_wf"
        message="Claude wants to call **WebFetch**"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview='WebFetch({"url": "https://github.com/a/b"})'
        requestedSchema={{}}
        status="pending"
        response={null}
        rememberScope={{ tool: "WebFetch", host: "github.com" }}
      />,
    );
    const rememberButton = screen.getByRole("button", {
      name: /don't ask again for github\.com/i,
    });
    expect(rememberButton).toBeDefined();
    // The tooltip spells out the (session-scoped) domain grant.
    expect(rememberButton.getAttribute("title")).toBe(
      "Won't ask again for github.com for the rest of this session",
    );
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDefined();

    // No hint (edit tool / ExitPlanMode / AskUserQuestion) → no button.
    rerender(
      <ApprovalCard
        elicitationId="elic_edit"
        message="Claude wants to call **Edit**"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview="Edit({})"
        requestedSchema={{}}
        status="pending"
        response={null}
      />,
    );
    expect(screen.queryByTestId("approval-card-remember")).toBeNull();
  });

  it("labels the remember button by the tool name for a tool-wide scope", () => {
    // Non-WebFetch tools get a tool-wide scope (no host), so the
    // button names the tool instead of a domain.
    render(
      <ApprovalCard
        elicitationId="elic_bash"
        message="Claude wants to call **Bash**"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview="Bash({})"
        requestedSchema={{}}
        status="pending"
        response={null}
        rememberScope={{ tool: "Bash" }}
      />,
    );
    const rememberButton = screen.getByRole("button", { name: /don't ask again for Bash/i });
    expect(rememberButton).toBeDefined();
    // Tool-wide grant is broader than a domain — the tooltip says "any".
    expect(rememberButton.getAttribute("title")).toBe(
      "Won't ask again for any Bash call for the rest of this session",
    );
  });

  it("submits {action: 'accept', content: {remember: true}} on click", () => {
    // The server reads ``content.remember`` to emit the ``addRules``
    // permission update; it re-derives the scope itself, so the client
    // sends only the flag.
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);

    render(
      <ApprovalCard
        elicitationId="elic_wf_click"
        message="Claude wants to call **WebFetch**"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview='WebFetch({"url": "https://github.com/a/b"})'
        requestedSchema={{}}
        status="pending"
        response={null}
        rememberScope={{ tool: "WebFetch", host: "github.com" }}
      />,
    );

    fireEvent.click(screen.getByTestId("approval-card-remember"));

    expect(submitSpy).toHaveBeenCalledWith("elic_wf_click", "accept", {
      remember: true,
    });
  });

  it("renders the won't-ask-again label in the responded state", () => {
    render(
      <ApprovalCard
        elicitationId="elic_wf_done"
        message="Claude wants to call **WebFetch**"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview='WebFetch({"url": "https://github.com/a/b"})'
        requestedSchema={{}}
        status="responded"
        response={{ action: "accept", content: { remember: true } }}
        rememberScope={{ tool: "WebFetch", host: "github.com" }}
      />,
    );

    expect(screen.getByText(/won't ask again for github\.com/i)).toBeDefined();
  });
});

describe("ApprovalCard — multi-choice options", () => {
  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_abc",
      blocks: [],
    });
  });

  it("renders one button per option when requestedSchema describes an enum answer", () => {
    // The ask-user-question endpoint produces a schema with an
    // enum on `properties.answer`. The card must render those as
    // option buttons — without this branch the user sees an
    // approve/reject pair that doesn't match the question Claude
    // asked.
    render(
      <ApprovalCard
        elicitationId="elic_aqu"
        message="Which framework should we use?"
        phase="ask_user_question"
        policyName="claude_native_ask_user_question"
        contentPreview="Which framework should we use?"
        requestedSchema={{
          type: "object",
          properties: {
            answer: { type: "string", enum: ["React", "Vue", "Svelte"] },
          },
          required: ["answer"],
        }}
        status="pending"
        response={null}
      />,
    );

    const container = screen.getByTestId("approval-card-options");
    expect(container).toBeDefined();
    expect(screen.getByRole("button", { name: "React" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Vue" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Svelte" })).toBeDefined();
    // No binary buttons in this mode.
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reject/i })).toBeNull();
  });

  it("submits {action: 'accept', content: {answer: <label>}} when an option is clicked", () => {
    // The hook endpoint shapes `updatedInput.answers` from the
    // content payload — if the click submits an empty or wrong
    // content shape, the endpoint falls through to TUI and the
    // selection is lost.
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);

    render(
      <ApprovalCard
        elicitationId="elic_pick"
        message="Pick one"
        phase="ask_user_question"
        policyName="claude_native_ask_user_question"
        contentPreview="Pick one"
        requestedSchema={{
          type: "object",
          properties: {
            answer: { type: "string", enum: ["Alpha", "Beta"] },
          },
        }}
        status="pending"
        response={null}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Beta" }));

    expect(submitSpy).toHaveBeenCalledWith("elic_pick", "accept", { answer: "Beta" });
  });

  it("renders 'Selected: <label>' on the responded card when content carries an answer", () => {
    // The store stamps `response.content.answer` after a successful
    // submit so the responded pill can show the actual choice
    // rather than a generic "Approved" label.
    render(
      <ApprovalCard
        elicitationId="elic_done"
        message="Pick one"
        phase="ask_user_question"
        policyName="claude_native_ask_user_question"
        contentPreview="Pick one"
        requestedSchema={{
          type: "object",
          properties: {
            answer: { type: "string", enum: ["Alpha", "Beta"] },
          },
        }}
        status="responded"
        response={{ action: "accept", content: { answer: "Alpha" } }}
      />,
    );

    expect(screen.getByText(/Selected: Alpha/)).toBeDefined();
  });
});

describe("ApprovalCard — AskUserQuestion form (parsed from content_preview)", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_abc", blocks: [] });
  });

  const sampleSinglePreview =
    'AskUserQuestion({"questions": [{"question": "Which framework?", ' +
    '"header": "Framework", "options": [{"label": "React"}, {"label": "Vue"}], ' +
    '"multiSelect": false}]})';

  const sampleMultiPreview =
    'AskUserQuestion({"questions": [' +
    '{"question": "Pick snacks", "options": ' +
    '[{"label": "Popcorn"}, {"label": "Pretzels"}], "multiSelect": true}]})';

  it("renders the form (radio inputs) for a single-select question", () => {
    // Single-select must use radios so the user can only pick one
    // — checkboxes would imply multi-select which mismatches the
    // tool's contract. Includes the trailing custom-input row's
    // radio for free-form entry.
    render(
      <ApprovalCard
        elicitationId="elic_form"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview={sampleSinglePreview}
        requestedSchema={{}}
        status="pending"
        response={null}
      />,
    );

    expect(screen.getByTestId("ask-user-question-form")).toBeDefined();
    expect(screen.getByText("Which framework?")).toBeDefined();
    expect(screen.getByText("Framework")).toBeDefined();
    // 2 option radios + 1 custom-row radio (free-form).
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(3);
    expect(radios[0]!.getAttribute("type")).toBe("radio");
  });

  it("renders checkbox inputs for a multiSelect question", () => {
    render(
      <ApprovalCard
        elicitationId="elic_multi"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview={sampleMultiPreview}
        requestedSchema={{}}
        status="pending"
        response={null}
      />,
    );

    // 2 option checkboxes + 1 custom-row checkbox (free-form).
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(3);
    expect(checkboxes[0]!.getAttribute("type")).toBe("checkbox");
  });

  it("gates Submit until every question has at least one selection", () => {
    // Without this gate Submit fires with half-filled answers and
    // the user can't tell which questions still need attention.
    render(
      <ApprovalCard
        elicitationId="elic_gate"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview={sampleSinglePreview}
        requestedSchema={{}}
        status="pending"
        response={null}
      />,
    );

    const submit = screen.getByRole("button", { name: /submit/i });
    expect(submit.hasAttribute("disabled")).toBe(true);

    fireEvent.click(screen.getByLabelText("React"));
    expect(submit.hasAttribute("disabled")).toBe(false);
  });

  it("submits gathered answers via submitApproval on click", () => {
    // The chat store gets ``{action: "accept", content}`` where
    // ``content`` IS the flat answers map — each question text is
    // a top-level key, with the selected label as the value.
    // Matches MCP's ElicitResult.content shape (str|int|float|bool|
    // list[str]|null per field). Wrapping in a nested ``answers``
    // object would fail server-side validation.
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);

    render(
      <ApprovalCard
        elicitationId="elic_submit"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview={sampleSinglePreview}
        requestedSchema={{}}
        status="pending"
        response={null}
      />,
    );

    fireEvent.click(screen.getByLabelText("Vue"));
    fireEvent.click(screen.getByRole("button", { name: /submit/i }));

    expect(submitSpy).toHaveBeenCalledWith("elic_submit", "accept", {
      "Which framework?": "Vue",
    });
  });

  it("submits structured question answers keyed by id when present", () => {
    // Codex requestUserInput questions carry stable ids that the
    // app-server expects in the result. The display text is only UI
    // copy; submitting by text would make the server return an empty
    // answers object to Codex.
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);

    render(
      <ApprovalCard
        elicitationId="elic_codex_id"
        message="Codex needs input"
        phase="codex_request_user_input"
        policyName="codex_native_request_user_input"
        contentPreview=""
        requestedSchema={{}}
        status="pending"
        response={null}
        askUserQuestion={{
          questions: [
            {
              id: "framework",
              question: "Which framework?",
              header: "Framework",
              options: [{ label: "React" }, { label: "Vue" }],
              multiSelect: false,
            },
          ],
        }}
      />,
    );

    fireEvent.click(screen.getByLabelText("React"));
    fireEvent.click(screen.getByRole("button", { name: /submit/i }));

    expect(submitSpy).toHaveBeenCalledWith("elic_codex_id", "accept", {
      framework: "React",
    });
  });

  it("labels Codex structured input prompts as Codex prompts", () => {
    // Codex and Claude share the structured AskUserQuestion renderer,
    // but the title must reflect the producer. Otherwise a Codex plan
    // prompt in web incorrectly says "Claude has questions".
    render(
      <ApprovalCard
        elicitationId="elic_codex_label"
        message="Codex needs input"
        phase="codex_request_user_input"
        policyName="codex_native_request_user_input"
        contentPreview=""
        requestedSchema={{}}
        status="pending"
        response={null}
        askUserQuestion={{
          questions: [
            {
              id: "plan_decision",
              question: "Implement this plan?",
              header: "Plan",
              options: [{ label: "Yes, implement this plan" }],
              multiSelect: false,
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("Codex needs input")).toBeDefined();
    expect(screen.queryByText("Claude has questions")).toBeNull();
  });

  it("submits multi-select answers as an array", () => {
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);

    render(
      <ApprovalCard
        elicitationId="elic_multi_submit"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview={sampleMultiPreview}
        requestedSchema={{}}
        status="pending"
        response={null}
      />,
    );

    fireEvent.click(screen.getByLabelText("Popcorn"));
    fireEvent.click(screen.getByLabelText("Pretzels"));
    fireEvent.click(screen.getByRole("button", { name: /submit/i }));

    expect(submitSpy).toHaveBeenCalledWith("elic_multi_submit", "accept", {
      "Pick snacks": ["Popcorn", "Pretzels"],
    });
  });

  it("prefers the server-stamped askUserQuestion payload over content_preview", () => {
    // The structured payload carries the FULL questions+options
    // (not subject to content_preview's 1024-char truncation). The
    // card MUST render the structured version when both are
    // present, even if content_preview is empty or wrong.
    render(
      <ApprovalCard
        elicitationId="elic_structured"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview="AskUserQuestion(<TRUNCATED-GARBAGE>"
        requestedSchema={{}}
        status="pending"
        response={null}
        askUserQuestion={{
          questions: [
            {
              question: "Which database?",
              options: [{ label: "Postgres" }, { label: "MySQL" }],
              multiSelect: false,
            },
          ],
        }}
      />,
    );

    expect(screen.getByTestId("ask-user-question-form")).toBeDefined();
    expect(screen.getByText("Which database?")).toBeDefined();
    expect(screen.getByLabelText("Postgres")).toBeDefined();
    expect(screen.getByLabelText("MySQL")).toBeDefined();
  });

  it("renders one question at a time and navigates between them via Prev/Next", () => {
    // Carousel layout: only the current question's fieldset is in
    // the DOM. Without this guard the form stacks every question
    // vertically, which becomes unreadable for 3+ question batches.
    render(
      <ApprovalCard
        elicitationId="elic_carousel"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview=""
        requestedSchema={{}}
        status="pending"
        response={null}
        askUserQuestion={{
          questions: [
            {
              question: "First?",
              header: "",
              options: [{ label: "A", description: "" }],
              multiSelect: false,
            },
            {
              question: "Second?",
              header: "",
              options: [{ label: "B", description: "" }],
              multiSelect: false,
            },
          ],
        }}
      />,
    );

    // Only the first question's fieldset is rendered.
    expect(screen.getByTestId("ask-user-question-progress").textContent).toBe("Question 1 of 2:");
    expect(screen.getByText("First?")).toBeDefined();
    expect(screen.queryByText("Second?")).toBeNull();

    // Next reveals the second question.
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(screen.getByTestId("ask-user-question-progress").textContent).toBe("Question 2 of 2:");
    expect(screen.queryByText("First?")).toBeNull();
    expect(screen.getByText("Second?")).toBeDefined();

    // Prev returns to the first.
    fireEvent.click(screen.getByTestId("ask-user-question-prev"));
    expect(screen.getByText("First?")).toBeDefined();
  });

  it("hides Submit until the carousel is on the final question", () => {
    // Submit is the only path to commit the form, so it only
    // makes sense on the last slide — earlier slides expose Next.
    // Without this, users could submit prematurely and the LLM
    // sees half-filled answers.
    render(
      <ApprovalCard
        elicitationId="elic_submit_last"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview=""
        requestedSchema={{}}
        status="pending"
        response={null}
        askUserQuestion={{
          questions: [
            {
              question: "Q1",
              header: "",
              options: [{ label: "A", description: "" }],
              multiSelect: false,
            },
            {
              question: "Q2",
              header: "",
              options: [{ label: "B", description: "" }],
              multiSelect: false,
            },
          ],
        }}
      />,
    );

    expect(screen.queryByTestId("ask-user-question-submit")).toBeNull();
    fireEvent.click(screen.getByTestId("ask-user-question-next"));
    expect(screen.getByTestId("ask-user-question-submit")).toBeDefined();
  });

  it("accepts a custom-text answer that wins over the radio selection", () => {
    // Single-select: typing in the custom input OVERRIDES any
    // selected radio. The user clearly wants their typed value;
    // submitting a stale radio click would silently lose the
    // free-form answer.
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);

    render(
      <ApprovalCard
        elicitationId="elic_custom_single"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview={sampleSinglePreview}
        requestedSchema={{}}
        status="pending"
        response={null}
      />,
    );

    fireEvent.click(screen.getByLabelText("React"));
    fireEvent.change(screen.getByTestId("ask-user-question-custom-input"), {
      target: { value: "Solid" },
    });
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));

    expect(submitSpy).toHaveBeenCalledWith("elic_custom_single", "accept", {
      "Which framework?": "Solid",
    });
  });

  it("appends custom-text answer to multi-select checkbox selections", () => {
    // Multi-select: custom text is APPENDED to the checked
    // options on submit. The user can both pick known options
    // AND volunteer a free-form addition.
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);

    render(
      <ApprovalCard
        elicitationId="elic_custom_multi"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview={sampleMultiPreview}
        requestedSchema={{}}
        status="pending"
        response={null}
      />,
    );

    fireEvent.click(screen.getByLabelText("Popcorn"));
    fireEvent.change(screen.getByTestId("ask-user-question-custom-input"), {
      target: { value: "Chips" },
    });
    fireEvent.click(screen.getByTestId("ask-user-question-submit"));

    expect(submitSpy).toHaveBeenCalledWith("elic_custom_multi", "accept", {
      "Pick snacks": ["Popcorn", "Chips"],
    });
  });

  it("auto-checks the custom radio when the user starts typing", () => {
    // The custom row's radio/checkbox needs to be selected for
    // the typed value to count as the answer. Auto-checking on
    // first keystroke means the user doesn't have to click twice
    // (type + click radio). Without this, typing alone leaves the
    // question "unanswered" which surprises the user — they typed
    // something, yet Submit stays disabled.
    render(
      <ApprovalCard
        elicitationId="elic_autocheck"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview={sampleSinglePreview}
        requestedSchema={{}}
        status="pending"
        response={null}
      />,
    );

    const customToggle = screen.getByTestId("ask-user-question-custom-toggle") as HTMLInputElement;
    expect(customToggle.checked).toBe(false);

    fireEvent.change(screen.getByTestId("ask-user-question-custom-input"), {
      target: { value: "Solid" },
    });

    // Picking up the latest DOM state — auto-check should now be on.
    expect(
      (screen.getByTestId("ask-user-question-custom-toggle") as HTMLInputElement).checked,
    ).toBe(true);
    expect(screen.getByTestId("ask-user-question-submit").hasAttribute("disabled")).toBe(false);
  });

  it("renders the preview of selected options that carry a preview field", () => {
    // Selected option's `preview` ride through into a <pre> below
    // the options. Unselected previews stay hidden so the card
    // doesn't dump every preview at once.
    render(
      <ApprovalCard
        elicitationId="elic_preview"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview=""
        requestedSchema={{}}
        status="pending"
        response={null}
        askUserQuestion={{
          questions: [
            {
              question: "Layout?",
              header: "",
              options: [
                {
                  label: "Two-pane",
                  description: "Side-by-side",
                  preview: "[editor] | [output]",
                },
                { label: "Single", description: "One pane", preview: "[editor only]" },
              ],
              multiSelect: false,
            },
          ],
        }}
      />,
    );

    // No selection yet → no preview block.
    expect(screen.queryByTestId("ask-user-question-previews")).toBeNull();

    // Description is concatenated into the label text by the DOM
    // so a literal ``getByLabelText("Two-pane")`` doesn't match;
    // a regex picks out the label by its prefix.
    fireEvent.click(screen.getByLabelText(/Two-pane/));
    const previews = screen.getByTestId("ask-user-question-previews");
    expect(previews.textContent).toContain("[editor] | [output]");
    expect(previews.textContent).not.toContain("[editor only]");

    fireEvent.click(screen.getByLabelText(/Single/));
    expect(screen.getByTestId("ask-user-question-previews").textContent).toContain("[editor only]");
  });

  it("renders 'Submitted' with the answer summary in the responded state", () => {
    // After submit the card retains the response so the user (and
    // anyone scrolling history) can see what was chosen rather than
    // a generic "Approved" pill. ``content`` is the flat MCP map.
    render(
      <ApprovalCard
        elicitationId="elic_done"
        message="Claude wants to call AskUserQuestion"
        phase="pre_tool_use"
        policyName="claude_native_permission"
        contentPreview={sampleSinglePreview}
        requestedSchema={{}}
        status="responded"
        response={{
          action: "accept",
          content: { "Which framework?": "Vue" },
        }}
      />,
    );

    expect(screen.getByText(/Submitted/)).toBeDefined();
    expect(screen.getByText(/Vue/)).toBeDefined();
  });
});

describe("ApprovalCard — ExitPlanMode plan review", () => {
  const samplePlan =
    "# Migration plan\n\nMove the parser into **its own module**.\n\n- step one\n- step two\n";
  const planProps = {
    message: "Claude wants to call **ExitPlanMode**",
    phase: "pre_tool_use",
    policyName: "claude_native_permission",
    contentPreview: `ExitPlanMode({"plan": "# Migration plan"})`,
    requestedSchema: {},
    exitPlanMode: {
      plan: samplePlan,
      planFilePath: "/Users/example/.claude/plans/migration.md",
    },
    // The server stamps the auto-mode hint on every plan card.
    allowAllEdits: true,
  } as const;

  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_abc", blocks: [] });
  });

  it("renders the plan as markdown with the three plan-review actions", async () => {
    // The structured `exitPlanMode` extra must drive a dedicated
    // plan card: markdown-rendered plan + the three native-dialog
    // actions — NOT the generic binary card with a raw JSON preview.
    render(
      <ApprovalCard elicitationId="elic_plan" status="pending" response={null} {...planProps} />,
    );

    expect(screen.getByText("Plan review")).toBeDefined();
    // Markdown parsed: the heading text renders as element content
    // (Streamdown parses async — wait for it). If the plan were
    // dumped as preformatted JSON, the literal "# Migration plan"
    // marker would appear instead of a parsed heading.
    expect(await screen.findByRole("heading", { name: "Migration plan" })).toBeDefined();
    expect(screen.getByRole("button", { name: /yes, and use auto mode/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /yes, manually approve edits/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /reject with feedback/i })).toBeDefined();
    // The generic binary buttons and the raw preview must NOT render.
    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();
    expect(screen.queryByText(/ExitPlanMode\(/)).toBeNull();
  });

  it("submits {accept, allow_all_edits} for 'Yes, and use auto mode'", () => {
    // Auto mode rides the same content flag as the edit-tool
    // affordance; the server echoes setMode→auto. A wrong shape
    // here means the plan executes but every edit re-prompts.
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);
    render(
      <ApprovalCard
        elicitationId="elic_plan_auto"
        status="pending"
        response={null}
        {...planProps}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /yes, and use auto mode/i }));

    expect(submitSpy).toHaveBeenCalledWith("elic_plan_auto", "accept", {
      allow_all_edits: true,
    });
  });

  it("submits a plain accept for 'Yes, manually approve edits'", () => {
    // Plain accept must carry NO content — an accidental
    // allow_all_edits here would silently flip the session into
    // acceptEdits without the user choosing auto mode.
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);
    render(
      <ApprovalCard
        elicitationId="elic_plan_manual"
        status="pending"
        response={null}
        {...planProps}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /yes, manually approve edits/i }));

    // Third arg is the content slot — explicitly undefined (no flags).
    expect(submitSpy).toHaveBeenCalledWith("elic_plan_manual", "accept", undefined);
  });

  it("reveals a feedback textarea and submits decline with the typed feedback", () => {
    // The feedback must reach the server as `content.feedback` — it
    // becomes the deny `message` Claude revises the plan against.
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);
    render(
      <ApprovalCard
        elicitationId="elic_plan_reject"
        status="pending"
        response={null}
        {...planProps}
      />,
    );

    // No textarea until the reject action is chosen.
    expect(screen.queryByTestId("exit-plan-mode-feedback")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /reject with feedback/i }));
    const textarea = screen.getByPlaceholderText(/what should change/i);
    fireEvent.change(textarea, { target: { value: "Use a feature flag instead." } });
    fireEvent.click(screen.getByRole("button", { name: /reject plan/i }));

    expect(submitSpy).toHaveBeenCalledWith("elic_plan_reject", "decline", {
      feedback: "Use a feature flag instead.",
    });
  });

  it("submits a plain decline when the feedback is left empty", () => {
    // Whitespace-only feedback must not ship an empty `feedback`
    // string — the server would forward "" as the deny message.
    const submitSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ submitApproval: submitSpy } as Partial<
      ReturnType<typeof useChatStore.getState>
    >);
    render(
      <ApprovalCard
        elicitationId="elic_plan_reject_bare"
        status="pending"
        response={null}
        {...planProps}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /reject with feedback/i }));
    fireEvent.change(screen.getByPlaceholderText(/what should change/i), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByRole("button", { name: /reject plan/i }));

    expect(submitSpy).toHaveBeenCalledWith("elic_plan_reject_bare", "decline", undefined);
  });

  it("renders plan-specific responded labels and echoes rejection feedback", () => {
    // The responded pill must say what happened to the PLAN (and in
    // which mode), and a rejection must surface the feedback so the
    // chat history shows why the plan went back for revision.
    const { rerender } = render(
      <ApprovalCard
        elicitationId="elic_plan_done"
        status="responded"
        response={{ action: "accept", content: { allow_all_edits: true } }}
        {...planProps}
      />,
    );
    expect(screen.getByText("Plan approved · auto mode")).toBeDefined();

    rerender(
      <ApprovalCard
        elicitationId="elic_plan_done"
        status="responded"
        response={{ action: "accept" }}
        {...planProps}
      />,
    );
    expect(screen.getByText("Plan approved")).toBeDefined();

    rerender(
      <ApprovalCard
        elicitationId="elic_plan_done"
        status="responded"
        response={{ action: "decline", content: { feedback: "Too risky, split it up." } }}
        {...planProps}
      />,
    );
    expect(screen.getByText("Plan rejected")).toBeDefined();
    expect(screen.getByTestId("plan-rejection-feedback").textContent).toContain(
      "Too risky, split it up.",
    );
  });
});

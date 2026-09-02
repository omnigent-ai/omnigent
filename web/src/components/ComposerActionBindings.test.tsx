import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  ActionScope,
  ActionsProvider,
  HANDLED,
  KeybindingDispatcher,
  NOT_HANDLED,
  useActions,
} from "@/actions";
import { ComposerActionBindings } from "./ComposerActionBindings";

const DEFAULT_SEND = () => HANDLED;
const DEFAULT_ACCEPT = () => HANDLED;

function Harness({
  onSend = DEFAULT_SEND,
  onAccept = DEFAULT_ACCEPT,
  composing = false,
}: {
  onSend?: () => "handled" | "notHandled";
  onAccept?: (behavior: "openOrAttach" | "attach") => "handled" | "notHandled";
  composing?: boolean;
}) {
  const { execute } = useActions();
  const textareaRef = { current: null as HTMLTextAreaElement | null };
  return (
    <ActionScope mode="composer" context={{ composerSuggestionsOpen: false }}>
      <form>
        <ComposerActionBindings
          textareaRef={textareaRef}
          isComposing={() => composing}
          onSend={onSend}
          onStop={() => NOT_HANDLED}
          onRecallPrevious={() => NOT_HANDLED}
          onRecallNext={() => NOT_HANDLED}
          onSelectPreviousSuggestion={() => NOT_HANDLED}
          onSelectNextSuggestion={() => NOT_HANDLED}
          onAcceptSuggestion={onAccept}
          onDismissSuggestions={() => NOT_HANDLED}
        />
        <textarea
          ref={(element) => {
            textareaRef.current = element;
          }}
          aria-label="composer"
        />
        <input aria-label="other input" />
        <button
          type="button"
          onClick={() => execute({ action: "composer.action.send", source: "api" })}
        >
          Run send
        </button>
      </form>
    </ActionScope>
  );
}

function renderHarness(props: Parameters<typeof Harness>[0]) {
  return render(
    <ActionsProvider>
      <KeybindingDispatcher />
      <Harness {...props} />
    </ActionsProvider>,
  );
}

describe("ComposerActionBindings", () => {
  it("routes Enter and direct API execution through the same send handler", () => {
    const onSend = vi.fn(() => HANDLED);
    renderHarness({ onSend });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "composer" }), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Run send" }));
    expect(onSend).toHaveBeenCalledTimes(2);
  });

  it("does not treat Enter in another scoped input as composer send", () => {
    const onSend = vi.fn(() => HANDLED);
    renderHarness({ onSend });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "other input" }), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("keeps direct actions available while an IME owns keyboard input", () => {
    const onSend = vi.fn(() => HANDLED);
    renderHarness({ onSend, composing: true });
    fireEvent.click(screen.getByRole("button", { name: "Run send" }));
    expect(onSend).toHaveBeenCalledOnce();
  });

  it("honors the local IME composition fallback", () => {
    const onSend = vi.fn(() => HANDLED);
    renderHarness({ onSend, composing: true });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "composer" }), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });
});

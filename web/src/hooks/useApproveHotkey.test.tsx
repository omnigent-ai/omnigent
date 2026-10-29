// Cmd/Ctrl+Enter accepts the newest pending accept/decline prompt; skips
// already-responded prompts and AskUserQuestion (which needs an explicit
// choice); platform-aware (only ⌘ on macOS, only Ctrl on Win/Linux); ignores
// bare Enter, Alt/Shift-modified Enter, and a chord landing in a text field
// that holds a draft (a send intent, not a verdict).

import { fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const submitApproval = vi.fn();
let blocks: Record<string, unknown>[] = [];
vi.mock("@/store/chatStore", () => ({
  useChatStore: { getState: () => ({ blocks, submitApproval }) },
}));
let embedded = false;
vi.mock("@/lib/embedded", () => ({ useIsEmbedded: () => embedded }));
// The drawer's own tree stands in as one folder button.
vi.mock("@/shell/FilesPanel", () => ({
  FilesPanel: () => <button type="button">src</button>,
}));

import { setEmbedRoot } from "@/lib/host";
import { FilesPanelDrawer } from "@/shell/FilesPanelDrawer";
import { useApproveHotkey } from "./useApproveHotkey";

/** Dispatch a keydown that reaches window from body (default: Cmd+Enter). */
function press(
  mods: Partial<Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey">> = {
    metaKey: true,
  },
  key = "Enter",
): void {
  document.body.dispatchEvent(
    new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...mods }),
  );
}

beforeEach(() => {
  submitApproval.mockClear();
  blocks = [];
});
afterEach(() => {
  blocks = [];
  embedded = false;
  setEmbedRoot(null);
});

describe("useApproveHotkey", () => {
  const pending = { type: "elicitation", elicitationId: "e1", status: "pending" };

  const FIELD_SCHEMA = {
    type: "object",
    properties: { branch: { type: "string" } },
    required: ["branch"],
  };

  it("does not accept a prompt that asks for fields", () => {
    // The card disables Submit until the required fields are answered. A
    // keystroke that accepts anyway walks around that gate and sends the
    // server none of what it asked for.
    blocks = [{ ...pending, requestedSchema: FIELD_SCHEMA }];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("does not reach past a form to accept an older binary prompt", () => {
    // The newest prompt is the one on screen. Accepting an older one while
    // someone fills in a form approves something they cannot see.
    blocks = [
      { type: "elicitation", elicitationId: "old", status: "pending" },
      { ...pending, elicitationId: "form", requestedSchema: FIELD_SCHEMA },
    ];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it.each(["sending", "unconfirmed", "ready"])(
    "only considers the newest pending approval when its delivery is %s",
    (state) => {
      blocks = [
        { ...pending, elicitationId: "older", nativeDelivery: { state: "ready" } },
        { ...pending, elicitationId: "newer", nativeDelivery: { state } },
      ];
      renderHook(() => useApproveHotkey(true));
      press();
      if (state === "ready") {
        expect(submitApproval).toHaveBeenCalledExactlyOnceWith("newer", "accept");
      } else {
        expect(submitApproval).not.toHaveBeenCalled();
      }
    },
  );

  it("still accepts a bare consent prompt that names no fields", () => {
    blocks = [{ ...pending, requestedSchema: { type: "object" } }];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).toHaveBeenCalledWith("e1", "accept");
  });

  it("Cmd+Enter accepts the pending approval (macOS)", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).toHaveBeenCalledWith("e1", "accept");
  });

  it("Ctrl+Enter accepts on Windows/Linux", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(false));
    press({ ctrlKey: true });
    expect(submitApproval).toHaveBeenCalledWith("e1", "accept");
  });

  it("ignores Ctrl+Enter on macOS (only ⌘↵ fires there)", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(true));
    press({ ctrlKey: true });
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("accepts the most recent pending approval", () => {
    blocks = [
      { type: "elicitation", elicitationId: "old", status: "pending" },
      { type: "text" },
      { type: "elicitation", elicitationId: "new", status: "pending" },
    ];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).toHaveBeenCalledWith("new", "accept");
  });

  it("ignores already-responded prompts", () => {
    blocks = [{ type: "elicitation", elicitationId: "e1", status: "responded" }];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("skips AskUserQuestion (needs an explicit choice)", () => {
    blocks = [{ type: "elicitation", elicitationId: "q1", status: "pending", askUserQuestion: {} }];
    renderHook(() => useApproveHotkey(true));
    press();
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("ignores bare Enter and Alt/Shift-modified Enter", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(true));
    press({}); // bare Enter
    press({ metaKey: true, shiftKey: true });
    press({ metaKey: true, altKey: true });
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it("does not accept from a textarea holding a draft (send intent)", () => {
    // The user was typing when the prompt appeared. Ctrl+Enter is their
    // send chord (the ONLY one under the Mod+Enter preference); it must
    // never resolve a prompt that mounted moments earlier.
    blocks = [pending];
    renderHook(() => useApproveHotkey(false));
    const ta = document.createElement("textarea");
    ta.value = "deploying the fix, hold on";
    document.body.appendChild(ta);
    try {
      ta.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Enter",
          ctrlKey: true,
          bubbles: true,
          cancelable: true,
        }),
      );
      expect(submitApproval).not.toHaveBeenCalled();
    } finally {
      ta.remove();
    }
  });

  it("does not accept from an empty field marked as holding a draft (attachments/mentions)", () => {
    // A draft isn't only text: pending attachments or @-mentions make the
    // composer sendable with an empty value, and it advertises that via
    // data-has-draft. The chord is still a send intent there, not a verdict.
    blocks = [pending];
    renderHook(() => useApproveHotkey(false));
    const ta = document.createElement("textarea");
    ta.dataset.hasDraft = "true";
    document.body.appendChild(ta);
    try {
      ta.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Enter",
          ctrlKey: true,
          bubbles: true,
          cancelable: true,
        }),
      );
      expect(submitApproval).not.toHaveBeenCalled();
    } finally {
      ta.remove();
    }
  });

  it("still accepts from a focused checkbox (its constant value is not a draft)", () => {
    // A checkbox's value defaults to "on" without any user composition; a
    // chord from there is a verdict, not a send intent, and must not be
    // suppressed by the drafting guard.
    blocks = [pending];
    renderHook(() => useApproveHotkey(false));
    const box = document.createElement("input");
    box.type = "checkbox";
    document.body.appendChild(box);
    try {
      box.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Enter",
          ctrlKey: true,
          bubbles: true,
          cancelable: true,
        }),
      );
      expect(submitApproval).toHaveBeenCalledWith("e1", "accept");
    } finally {
      box.remove();
    }
  });

  it("still accepts from an empty text field (no draft, no send intent)", () => {
    // Post-send, focus can legitimately sit in the cleared composer; an
    // empty field carries no draft, so the chord keeps meaning "approve".
    blocks = [pending];
    renderHook(() => useApproveHotkey(false));
    const ta = document.createElement("textarea");
    document.body.appendChild(ta);
    try {
      ta.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Enter",
          ctrlKey: true,
          bubbles: true,
          cancelable: true,
        }),
      );
      expect(submitApproval).toHaveBeenCalledWith("e1", "accept");
    } finally {
      ta.remove();
    }
  });
});

describe("useApproveHotkey Esc", () => {
  const pending = { type: "elicitation", elicitationId: "e1", status: "pending" };

  afterEach(() => {
    document.body.innerHTML = "";
  });

  function pressEscapeFrom(target: HTMLElement): KeyboardEvent {
    target.focus();
    const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    target.dispatchEvent(event);
    return event;
  }

  function mount(html: string, selector: string): HTMLElement {
    document.body.innerHTML = html;
    return document.querySelector<HTMLElement>(selector)!;
  }

  it("declines the newest plain prompt", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(true));
    press({}, "Escape");
    expect(submitApproval).toHaveBeenCalledWith("e1", "decline");
  });

  it("declines from the chat composer unless its suggestion list is open", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(true));
    const composer = mount("<div data-composer-card><textarea></textarea></div>", "textarea");
    expect(pressEscapeFrom(composer).defaultPrevented).toBe(true);
    expect(submitApproval).toHaveBeenCalledTimes(1);

    composer.closest<HTMLElement>("[data-composer-card]")!.dataset.suggestionsOpen = "true";
    pressEscapeFrom(composer);
    expect(submitApproval).toHaveBeenCalledTimes(1);
  });

  it("leaves question prompts and other text fields alone", () => {
    blocks = [{ ...pending, askUserQuestion: {} }];
    renderHook(() => useApproveHotkey(true));
    press({}, "Escape");
    expect(submitApproval).not.toHaveBeenCalled();

    blocks = [pending];
    pressEscapeFrom(mount('<input type="search" />', "input"));
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it.each(["src", "Files", "composer"])(
    "leaves Escape to the open Files drawer with focus on %s",
    (focused) => {
      blocks = [pending];
      renderHook(() => useApproveHotkey(true));
      const onClose = vi.fn();
      render(
        <>
          {/* The header toggle and the composer sit outside the drawer. */}
          <button type="button">Files</button>
          <div data-composer-card="">
            <textarea aria-label="composer" />
          </div>
          <FilesPanelDrawer
            open
            onClose={onClose}
            onFileSelect={vi.fn()}
            flatView={false}
            showHidden={false}
            onShowHiddenChange={vi.fn()}
            sort="recent"
            onSortChange={vi.fn()}
          />
        </>,
      );
      const target =
        focused === "composer"
          ? screen.getByLabelText("composer")
          : screen.getByRole("button", { name: focused });
      target.focus();
      fireEvent.keyDown(target, { key: "Escape" });
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(submitApproval).not.toHaveBeenCalled();
    },
  );

  it("leaves Escape to an open panel that closes on it, such as Execution Logs, wherever focus is", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(true));
    document.body.innerHTML =
      '<aside data-escape-owner=""><button>Close</button></aside><nav><button>Other</button></nav>';
    const panelClose = vi.fn();
    const onWindowEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") panelClose();
    };
    window.addEventListener("keydown", onWindowEscape);
    try {
      pressEscapeFrom(screen.getByRole("button", { name: "Close" }));
      pressEscapeFrom(screen.getByRole("button", { name: "Other" }));
      expect(panelClose).toHaveBeenCalledTimes(2);
      expect(submitApproval).not.toHaveBeenCalled();

      // Once the panel has closed, Escape declines again.
      document.querySelector("aside")!.remove();
      pressEscapeFrom(screen.getByRole("button", { name: "Other" }));
      expect(submitApproval).toHaveBeenCalledWith("e1", "decline");
    } finally {
      window.removeEventListener("keydown", onWindowEscape);
    }
  });

  it("embedded, declines only for Escape pressed inside the app", () => {
    blocks = [pending];
    embedded = true;
    document.body.innerHTML = '<div id="app"><button>In app</button></div><button>Host</button>';
    setEmbedRoot(document.getElementById("app"));
    renderHook(() => useApproveHotkey(true));
    const hostHandler = vi.fn();
    const host = screen.getByRole("button", { name: "Host" });
    host.addEventListener("keydown", hostHandler);

    expect(pressEscapeFrom(host).defaultPrevented).toBe(false);
    expect(hostHandler).toHaveBeenCalledTimes(1);
    expect(submitApproval).not.toHaveBeenCalled();

    pressEscapeFrom(screen.getByRole("button", { name: "In app" }));
    expect(submitApproval).toHaveBeenCalledWith("e1", "decline");
  });

  it("yields to an open dialog", () => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(true));
    pressEscapeFrom(mount('<div role="dialog"><button>Close</button></div>', "button"));
    expect(submitApproval).not.toHaveBeenCalled();
  });

  it.each([
    ["a fullscreen diagram", "<div data-fullscreen-overlay></div>"],
    ["the model/effort preview", "<div data-cycle-preview></div>"],
  ])("yields to %s open with focus outside it", (_name, html) => {
    blocks = [pending];
    renderHook(() => useApproveHotkey(true));
    document.body.innerHTML = html;
    press({}, "Escape");
    expect(submitApproval).not.toHaveBeenCalled();
  });
});

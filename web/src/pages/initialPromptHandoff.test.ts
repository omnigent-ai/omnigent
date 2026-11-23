import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { acceptLaunchOffer } from "@/shell/floatingComposer/floatingLaunch";
import { consumePendingInitialPrompt, type PendingInitialPrompt } from "@/store/chatStore";
import { initialPromptAgentId, useInitialPromptDispatch } from "./ChatPage";

const bridge = vi.hoisted(() => ({
  takeLaunchOffer: vi.fn(),
  confirmLaunchOffer: vi.fn(),
  declineLaunchOffer: vi.fn(),
}));

/** The shell's bridge, as the preload exposes it to a main window's page. */
function stubShell(): void {
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    kind: "electron",
    floatingComposer: bridge,
  };
}

const message = {
  text: "Fix the build",
  skill: null,
  files: [{ name: "notes.txt", type: "text/plain", bytes: new Uint8Array([104, 105]) }],
};

type Dispatch = (prompt: PendingInitialPrompt, agentId: string) => void;

interface PageProps {
  urlConvId: string;
  boundAgentId: string | null;
  agentId: string | null;
  loadingConversation: boolean;
}

/** A session page as ChatPage wires it: the send agent comes from the same helper. */
function renderSessionPage(initial: PageProps, dispatch: Dispatch) {
  return renderHook(
    (props: PageProps) =>
      useInitialPromptDispatch({
        conversationId: props.urlConvId,
        loadingConversation: props.loadingConversation,
        agentId: initialPromptAgentId({
          urlConvId: props.urlConvId,
          boundAgentId: props.boundAgentId,
          agentId: props.agentId,
        }),
        dispatch,
      }),
    { initialProps: initial },
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  stubShell();
  bridge.takeLaunchOffer.mockResolvedValue(message);
  bridge.confirmLaunchOffer.mockResolvedValue(true);
  bridge.declineLaunchOffer.mockResolvedValue(true);
});

afterEach(() => {
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = undefined;
});

describe("a floating-panel launch handed to its session page", () => {
  it("is sent once the session loads, with the bound agent when no agent is selected", async () => {
    const dispatch = vi.fn<Dispatch>();
    const page = renderSessionPage(
      {
        urlConvId: "conv_handoff_1",
        boundAgentId: "agent_bound",
        agentId: null,
        loadingConversation: true,
      },
      dispatch,
    );

    await act(() => acceptLaunchOffer({ offerId: 4, sessionId: "conv_handoff_1" }));
    expect(bridge.confirmLaunchOffer).toHaveBeenCalledWith(4);
    expect(dispatch).not.toHaveBeenCalled();

    page.rerender({
      urlConvId: "conv_handoff_1",
      boundAgentId: "agent_bound",
      agentId: null,
      loadingConversation: false,
    });
    expect(dispatch).toHaveBeenCalledTimes(1);
    expect(dispatch).toHaveBeenCalledWith(
      { text: "Fix the build", skill: null, files: [expect.any(File)] },
      "agent_bound",
    );
    expect(consumePendingInitialPrompt("conv_handoff_1")).toBeNull();
  });
});

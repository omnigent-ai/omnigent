import { useContext, useEffect, useMemo } from "react";
import { ChatPage } from "@/pages/ChatPage";
import { ChatStoreContext, createSessionChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useSession } from "@/hooks/useSession";
import { useBrowserAgentRelay } from "@/hooks/useBrowserAgentRelay";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { isNativeWrapper } from "@/lib/nativeCodingAgents";
import { useNavigate } from "@/lib/routing";
import { FileViewerContext, type OpenFileOptions } from "@/shell/FileViewerContext";
import { citedFilePosition } from "@/shell/filePositionState";
import { TerminalFirstContextProvider } from "@/shell/TerminalFirstContext";
import { ConversationScopeContext } from "./conversationScope";

/** The main chat, with state and actions pinned to this panel's session. */
export function SessionChatPane({ sessionId }: { sessionId: string }) {
  const store = useMemo(() => createSessionChatStore(sessionId), [sessionId]);
  const { session } = useSession(sessionId);
  const navigate = useNavigate();
  const files = useContext(FileViewerContext);
  const healthSessions = useMemo(() => [{ id: sessionId }], [sessionId]);
  useRunnerHealthRegistration(healthSessions);
  useBrowserAgentRelay(sessionId);
  useEffect(() => conversationRegistry.retain(sessionId), [sessionId]);
  const wrapper = session?.labels?.["omnigent.wrapper"];
  const fileContext = useMemo(
    () => ({
      ...files,
      conversationId: sessionId,
      workspaceRoot: session?.workspace ?? null,
      workspaceHome: null,
      sessionHostId: session?.hostId,
      openFile: (path: string, options?: OpenFileOptions) => {
        const params = new URLSearchParams({ file: path });
        for (const [key, value] of Object.entries(citedFilePosition(options) ?? {})) {
          params.set(key, String(value));
        }
        navigate(`/c/${sessionId}?${params}`);
      },
      openGithubTab: () => navigate(`/c/${sessionId}?panel=github`),
      openFolder: undefined,
      isChangedPath: () => false,
      registerNavigationGuard: undefined,
    }),
    [files, sessionId, session?.workspace, session?.hostId, navigate],
  );
  return (
    <ChatStoreContext.Provider value={store}>
      <ConversationScopeContext.Provider value={sessionId}>
        <TerminalFirstContextProvider
          value={{
            isClaudeNative: wrapper === "claude-code-native-ui",
            isNativeWrapper: isNativeWrapper(wrapper),
            isTerminalFirst: false,
            isShellView: false,
            view: "chat",
            terminalViewKey: null,
            setView: () => {},
            terminalsAvailable: false,
            terminalStartingUp: false,
          }}
        >
          <FileViewerContext.Provider value={fileContext}>
            <ChatPage sessionId={sessionId} />
          </FileViewerContext.Provider>
        </TerminalFirstContextProvider>
      </ConversationScopeContext.Provider>
    </ChatStoreContext.Provider>
  );
}

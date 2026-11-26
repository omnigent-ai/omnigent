import {
  createContext,
  startTransition,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export const WorkspacePreviewContext = createContext({ deferred: false, visible: true });

export function WorkspacePreviewProvider({
  conversationId,
  visible,
  children,
}: {
  conversationId: string;
  visible: boolean;
  children: ReactNode;
}) {
  const [renderedConversationId, setRenderedConversationId] = useState(conversationId);
  const deferred = !visible && renderedConversationId !== conversationId;
  if (visible && renderedConversationId !== conversationId) {
    setRenderedConversationId(conversationId);
  }
  useEffect(() => {
    if (!deferred) return;
    // Let the session switch paint before starting synchronous Markdown parsing.
    let task: ReturnType<typeof setTimeout> | undefined;
    const frame = requestAnimationFrame(() => {
      task = setTimeout(() => {
        startTransition(() => setRenderedConversationId(conversationId));
      }, 0);
    });
    return () => {
      cancelAnimationFrame(frame);
      clearTimeout(task);
    };
  }, [conversationId, deferred]);
  const value = useMemo(() => ({ deferred, visible }), [deferred, visible]);
  return (
    <WorkspacePreviewContext.Provider value={value}>{children}</WorkspacePreviewContext.Provider>
  );
}

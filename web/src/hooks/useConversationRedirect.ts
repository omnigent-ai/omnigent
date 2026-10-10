import { useEffect } from "react";
import { isCanvasPathname } from "@/canvas/canvasNavigation";
import { useLocation, useNavigate, useRebasePath } from "@/lib/routing";
import { useChatStore } from "@/store/chatStore";

/** Follow session supersession once, replacing the cleared session in history. */
export function useConversationRedirect(conversationId: string | undefined) {
  const navigate = useNavigate();
  const { pathname, search } = useLocation();
  const rebasePath = useRebasePath();
  const target = useChatStore((state) => state.redirectToConversationId);
  useEffect(() => {
    if (!target) return;
    if (target !== conversationId) {
      const canvas = isCanvasPathname(pathname, rebasePath("/canvas"));
      navigate(
        {
          pathname: `${canvas ? "/canvas" : ""}/c/${encodeURIComponent(target)}`,
          search: canvas ? search : "",
        },
        { replace: true },
      );
    }
    useChatStore.setState({ redirectToConversationId: null });
  }, [target, conversationId, navigate, pathname, search, rebasePath]);
}

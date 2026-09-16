import { HANDLED, useRegisterAction } from "@/actions";
import type { UserMessageNav } from "./useUserMessageNav";

/** Register transcript navigation actions against one shared navigation cursor. */
export function useMessageNavigationActions(nav: UserMessageNav): void {
  useRegisterAction("chat.action.openPreviousMessage", {
    acceptsKeybindings: true,
    run: () => {
      nav.goPrev();
      return HANDLED;
    },
  });
  useRegisterAction("chat.action.openNextMessage", {
    acceptsKeybindings: true,
    run: () => {
      nav.goNext();
      return HANDLED;
    },
  });
}

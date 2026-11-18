// Where the new-chat composer is shown: the full page, or the floating New Agent panel. The
// panel reuses the same screen and changes only its chrome and who delivers the first message.
import { createContext, useContext } from "react";
import type { PendingInitialPrompt } from "@/store/chatStore";

/** A session the floating composer created, whose first message it now delivers. */
export interface FloatingLaunchRequest extends PendingInitialPrompt {
  sessionId: string;
  /** The create itself carried the first message, so there is nothing left to post. */
  inputAccepted: boolean;
}

/** App pages the composer links to; the panel shows them in a main window instead. */
export type ComposerAppPage = "/settings/integrations";

export type ComposerPresentation =
  | { kind: "page" }
  | {
      kind: "floating";
      /** Opens the created session and delivers its first message unless the create took it. */
      launch: (request: FloatingLaunchRequest) => void;
      /** The native file picker opened or closed, so the panel stays up while it's open. */
      onFilePicker: (state: "open" | "closed") => void;
      openAppPage: (page: ComposerAppPage) => void;
    };

export const ComposerPresentationContext = createContext<ComposerPresentation>({ kind: "page" });

export function useComposerPresentation(): ComposerPresentation {
  return useContext(ComposerPresentationContext);
}

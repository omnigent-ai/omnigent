// Opens a session's own page, provided by the app shell, which holds the router. Outside the
// shell, such as in a list rendered by itself, opening does nothing.

import { createContext, useContext } from "react";

export type OpenSessionPage = (sessionId: string) => void;

const OpenSessionPageContext = createContext<OpenSessionPage>(() => {});

export const OpenSessionPageProvider = OpenSessionPageContext.Provider;

/** The function that opens a session's page, e.g. `/c/conv_child123`. */
export function useOpenSessionPage(): OpenSessionPage {
  return useContext(OpenSessionPageContext);
}

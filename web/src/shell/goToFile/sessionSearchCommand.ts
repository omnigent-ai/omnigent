// Search in Sessions…: ⌘K's way into the finder's Sessions tab, which searches the names and
// messages of the sessions the user can see. One definition for the ⌘K row and the tab.

import { MessagesSquareIcon } from "lucide-react";

import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";
import { requestSessionSearch } from "@/store/sessionSearch";

export const SEARCH_SESSIONS_LABEL = "Search in Sessions…";

export const SEARCH_SESSIONS_COMMAND: PaletteCommand = {
  id: "search-sessions",
  label: SEARCH_SESSIONS_LABEL,
  icon: MessagesSquareIcon,
  keywords: ["find", "messages", "conversations", "chat history", "search", "session"],
  // After the palette closes and restores focus, so the finder returns focus there too.
  movesFocus: true,
  run: () => requestSessionSearch(),
};

const COMMANDS = [SEARCH_SESSIONS_COMMAND];

/** Offers Search in Sessions… in ⌘K wherever the finder is hosted, with or without a session. */
export function useSessionSearchPaletteCommand(): void {
  usePaletteCommands("session-search", "Search", COMMANDS);
}

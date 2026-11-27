// Go to File and Search Text, its two modes: one definition each for their ⌘K rows and chords, so
// the label, the key caps and what runs can't drift between them. The Files page's button
// and the New tab page's row ask through the same file-finder request.

import { useEffect, useMemo, useRef } from "react";
import { FileSearchIcon, TextSearchIcon } from "lucide-react";

import { isMacPlatform } from "@/lib/hotkeys";
import { matchesShortcut, type ShortcutId } from "@/lib/shortcuts/registry";
import { WORKSPACE_TEXT_SEARCH_COPY } from "@/lib/workspaceTextSearchCopy";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

export const GO_TO_FILE_LABEL = "Go to File in Workspace…";
/** Why Go to File can't open, where an entry point shows it unavailable. */
export const GO_TO_FILE_UNAVAILABLE =
  "Go to File is available once this session's workspace is connected.";

export interface GoToFileCommand {
  id: "go-to-file" | "search-text";
  label: string;
  keywords: readonly string[];
  shortcut: ShortcutId;
  icon: PaletteCommand["icon"];
  /** What its chord does when pressed; its ⌘K row always opens. */
  run: (origin: "chord" | "palette") => void;
}

export interface GoToFileCommandHandlers {
  /** Whether Search Text is offered: the session's host searches its files' text. */
  textSearch: boolean;
  /** Opens Go to File; from its chord, closes it when it already shows. */
  goToFile: (options: { toggle: boolean }) => void;
  /** Opens Go to File on Text; from its chord, closes it when Text already shows. */
  searchText: (options: { toggle: boolean }) => void;
}

/** The two commands, bound to the session's handlers. */
export function goToFileCommands(handlers: GoToFileCommandHandlers): GoToFileCommand[] {
  const commands: GoToFileCommand[] = [
    {
      id: "go-to-file",
      label: GO_TO_FILE_LABEL,
      keywords: ["file", "open", "find", "quick open", "path"],
      shortcut: "palette.files",
      icon: FileSearchIcon,
      run: (origin) => handlers.goToFile({ toggle: origin === "chord" }),
    },
    {
      id: "search-text",
      label: WORKSPACE_TEXT_SEARCH_COPY.command,
      keywords: ["search", "find", "grep", "content", "text", "find in files"],
      shortcut: "palette.text",
      icon: TextSearchIcon,
      run: (origin) => handlers.searchText({ toggle: origin === "chord" }),
    },
  ];
  return handlers.textSearch
    ? commands
    : commands.filter((command) => command.id !== "search-text");
}

/**
 * Offers Go to File and Search Text in ⌘K and binds their chords while `sessionId` is set;
 * null offers and binds nothing.
 */
export function useGoToFileCommands(
  sessionId: string | null,
  handlers: GoToFileCommandHandlers,
  isMac = isMacPlatform(),
): void {
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  const { textSearch } = handlers;
  const commands = useMemo(
    () =>
      sessionId === null
        ? []
        : goToFileCommands({
            textSearch,
            goToFile: (options) => handlersRef.current.goToFile(options),
            searchText: (options) => handlersRef.current.searchText(options),
          }),
    [sessionId, textSearch],
  );
  const paletteCommands = useMemo<PaletteCommand[]>(
    () =>
      commands.map((command) => ({
        // The session is part of the id, so a row built for another session never runs.
        id: `go-to-file:${sessionId}:${command.id}`,
        label: command.label,
        icon: command.icon,
        keywords: [...command.keywords],
        shortcut: command.shortcut,
        // Each opens its own surface, which takes focus once the palette has closed.
        movesFocus: true,
        run: () => command.run("palette"),
      })),
    [commands, sessionId],
  );
  usePaletteCommands(`go-to-file:${sessionId ?? "none"}`, "Workspace", paletteCommands);

  useEffect(() => {
    if (commands.length === 0) return;
    const handler = (event: globalThis.KeyboardEvent): void => {
      // Holding the chord would flap Go to File open and shut.
      if (event.repeat) return;
      const command = commands.find((candidate) =>
        matchesShortcut(event, candidate.shortcut, { mac: isMac }),
      );
      if (!command) return;
      event.preventDefault();
      event.stopPropagation();
      command.run("chord");
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [commands, isMac]);
}

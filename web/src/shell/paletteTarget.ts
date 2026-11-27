import type { ActionCommand } from "./appCommands";
import type { PaletteCommandRow } from "./paletteCommandRows";
import type { SessionCommandId } from "./sessionCommands/commandIds";

/** What accepting a ⌘K row does: open a session, run a command, or pick on a command's page. */
export type PaletteTarget =
  | { kind: "session"; id: string }
  | { kind: "action"; action: ActionCommand }
  | PaletteCommandRow
  /** A session group's paging row; the selection parks on `lastRowId` while more load. */
  | { kind: "more"; load: () => void; lastRowId: string | undefined }
  /** A value on a setting's page; `itemId` is the setting row that opened it. */
  | { kind: "setting-choice"; itemId: string; value: string }
  /** One of a command's options, on its page; `itemId` is the command row that opened it. */
  | { kind: "command-option"; itemId: string; optionId: string }
  /** One of a session's actions, on its page. */
  | { kind: "session-action"; sessionId: string; command: SessionCommandId };

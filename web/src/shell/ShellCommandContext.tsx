import { createContext } from "react";

export const ShellCommandContext = createContext<{
  open: (command: string, language: string) => void;
  disabled: boolean;
} | null>(null);

export const ShellCommandDraftContext = createContext<{
  drafts: Record<string, string>;
  setDraft: (key: string, command: string | null) => void;
} | null>(null);

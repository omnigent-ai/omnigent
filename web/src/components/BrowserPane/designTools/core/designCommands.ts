// The design tools' commands (Send, Undo) declare what they need once, so a button and
// its key press are checked the same way.

export interface DesignCommand {
  /** Why it can't run now, however it's asked for; undefined when it can. */
  blocked?: string;
  /** Do it; a command whose outcome comes later (a Send's capture) resolves with it. */
  run: () => void | Promise<CommandResult>;
}

export type CommandResult = { ok: true } | { ok: false; error: string };

/** Run `command` if nothing blocks it (its later outcome goes unheard, a failure logged). */
export function runCommand(command: DesignCommand): CommandResult {
  const { blocked } = command;
  if (blocked) return { ok: false, error: blocked };
  const failed = (error: unknown) => console.error("A design command failed", error);
  try {
    command.run()?.catch(failed);
  } catch (error) {
    failed(error);
  }
  return { ok: true };
}

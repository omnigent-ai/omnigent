import { HANDLED, useRegisterAction } from "@/actions";

/** Register the workspace's default-shell launch command. */
export function useNewShellAction(onLaunch: () => void, enabled = true): void {
  useRegisterAction(
    "terminal.action.new",
    {
      scope: "global",
      acceptsKeybindings: true,
      isEnabled: () => enabled,
      run: () => {
        onLaunch();
        return HANDLED;
      },
    },
    enabled,
  );
}

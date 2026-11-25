import { skipToken, useQuery } from "@tanstack/react-query";
import { LogInIcon } from "lucide-react";
import { useEffect } from "react";

import { githubSignInController } from "@/lib/codeHostSignIn";
import { githubSignInBridge, isGitHubSignInAvailable } from "@/lib/nativeBridge";
import {
  AVAILABLE,
  HIDDEN,
  type CommandAvailability,
  type PaletteCommand,
} from "@/store/paletteCommands";

export const GITHUB_SIGN_IN_COMMAND_LABEL = "Sign in to GitHub on this Mac…";
export const GITHUB_SIGN_IN_AVAILABLE_KEY = ["github-sign-in", "available"] as const;

/**
 * Whether this shell can sign in to GitHub on this Mac, asked again each time the palette
 * opens, so a kill switch or a newly installed CLI applies. Hidden in browsers and older shells.
 */
export function useGitHubSignInAvailability(paletteOpen: boolean): CommandAvailability {
  const bridge = githubSignInBridge();
  const query = useQuery({
    queryKey: GITHUB_SIGN_IN_AVAILABLE_KEY,
    queryFn: bridge ? () => isGitHubSignInAvailable() : skipToken,
    staleTime: Infinity,
    retry: false,
  });
  const { refetch } = query;
  useEffect(() => {
    if (paletteOpen && bridge) void refetch();
  }, [paletteOpen, bridge, refetch]);
  return bridge && query.data === true ? AVAILABLE : HIDDEN;
}

/** "Sign in to GitHub on this Mac…": the one command every sign-in entry point shares. */
export function githubSignInCommand(availability: CommandAvailability): PaletteCommand {
  return {
    id: "sign-in-to-github-on-this-mac",
    label: GITHUB_SIGN_IN_COMMAND_LABEL,
    icon: LogInIcon,
    keywords: ["github", "sign in", "log in", "login", "gh", "auth", "account", "enterprise"],
    availability,
    run: () => {
      void githubSignInController.open();
    },
  };
}

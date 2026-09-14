import { and, equals } from "./context";
import type { ContextExpression } from "./types";

export function keybindingEnvironmentExpression({
  isMac,
  isNativeShell,
  isEmbedded,
}: {
  isMac: boolean;
  isNativeShell: boolean;
  isEmbedded: boolean;
}): ContextExpression {
  return and(
    equals("isMac", isMac),
    equals("isNativeShell", isNativeShell),
    equals("isEmbedded", isEmbedded),
  );
}

export function isMacKeyboardPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const withData = navigator as Navigator & { userAgentData?: { platform?: string } };
  const platform = withData.userAgentData?.platform ?? navigator.platform ?? navigator.userAgent;
  return /Mac|iPhone|iPad|iPod/i.test(platform);
}

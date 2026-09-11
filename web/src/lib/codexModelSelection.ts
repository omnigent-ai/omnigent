import type { NativeModelOption } from "@/lib/types";

/** The provider ID behind a choice from the native model picker. */
export function selectedCodexModelId(
  selected: string | null,
  options: readonly NativeModelOption[],
): string | undefined {
  return options.find((option) => option.id === selected)?.model || undefined;
}

import type { ModelConfigurationSource } from "@/lib/types";

export interface ModelConfigurationSourceRow {
  label: string;
  value: string;
}

function titleCaseProvider(name: string): string {
  return name.charAt(0).toUpperCase() + name.slice(1);
}

/** Turn non-secret provider coordinates into concise, labeled UI rows. */
export function modelConfigurationSourceRows(
  source: ModelConfigurationSource | null | undefined,
): ModelConfigurationSourceRow[] {
  if (!source) return [];

  const name = source.name ? titleCaseProvider(source.name) : null;
  const connection =
    source.kind === "subscription" && name
      ? `${name} subscription`
      : source.kind === "databricks"
        ? ["Databricks", source.name ?? source.host].filter(Boolean).join(" · ")
        : source.kind === "gateway"
          ? ["AI Gateway", source.name ?? source.host].filter(Boolean).join(" · ")
          : source.kind === "bedrock"
            ? ["Bedrock", source.name ?? source.host].filter(Boolean).join(" · ")
            : source.kind === "key"
              ? ["API key", source.name ?? source.host].filter(Boolean).join(" · ")
              : [source.label, source.name, source.host].filter(Boolean).join(" · ");
  return [
    {
      label: "Connection",
      value: connection,
    },
  ];
}

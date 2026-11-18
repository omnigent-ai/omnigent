// The Changes tab's path filter: narrows the changed-file list by path. Escape clears it.

import { Input } from "@/components/ui/input";

export function PullRequestPathFilter({
  value,
  onChange,
}: {
  value: string;
  onChange: (query: string) => void;
}) {
  return (
    <Input
      type="search"
      aria-label="Filter changed files"
      placeholder="Filter files…"
      value={value}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        // Stop Escape from also closing an enclosing drawer while a filter is set.
        if (event.key !== "Escape" || !value) return;
        event.stopPropagation();
        onChange("");
      }}
    />
  );
}

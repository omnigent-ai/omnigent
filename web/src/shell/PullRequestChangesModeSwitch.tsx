// The Changes toolbar's segmented control: the PR's changed files or its commits.

import { cn } from "@/lib/utils";

export type ChangesSource = "changes" | "commits";

const OPTIONS: { value: ChangesSource; label: string }[] = [
  { value: "changes", label: "Changes" },
  { value: "commits", label: "Commits" },
];

export function PullRequestChangesModeSwitch({
  value,
  onChange,
}: {
  value: ChangesSource;
  onChange: (value: ChangesSource) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Show changes or commits"
      className="flex shrink-0 items-center rounded-md border border-border p-px text-xs"
    >
      {OPTIONS.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange(option.value)}
            className={cn(
              "rounded-sm px-2 py-0.5",
              selected
                ? "bg-muted font-medium text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

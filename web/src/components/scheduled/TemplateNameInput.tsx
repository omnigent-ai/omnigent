import { useRef, type ComponentPropsWithoutRef, type UIEvent } from "react";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export interface TemplateNamePart {
  kind: "text" | "placeholder";
  text: string;
}

export function splitNameTemplate(value: string): TemplateNamePart[] {
  const parts: TemplateNamePart[] = [];
  let plainStart = 0;
  let index = 0;

  // Mirrors the delimiter and escape scan in omnigent/server/scheduled/name_template.py.
  while (index < value.length) {
    if (value.startsWith("\\{{", index)) {
      index += 3;
      continue;
    }
    if (!value.startsWith("{{", index)) {
      index += 1;
      continue;
    }

    const closing = value.indexOf("}}", index + 2);
    if (closing === -1) break;
    if (plainStart < index) {
      parts.push({ kind: "text", text: value.slice(plainStart, index) });
    }
    const end = closing + 2;
    parts.push({ kind: "placeholder", text: value.slice(index, end) });
    index = end;
    plainStart = end;
  }

  if (plainStart < value.length) {
    parts.push({ kind: "text", text: value.slice(plainStart) });
  }
  if (parts.length === 0) {
    parts.push({ kind: "text", text: value });
  }
  return parts;
}

type TemplateNameInputProps = Omit<ComponentPropsWithoutRef<typeof Input>, "value"> & {
  value: string;
};

export function TemplateNameInput({
  className,
  onScroll,
  value,
  ...inputProps
}: TemplateNameInputProps) {
  const backdropRef = useRef<HTMLDivElement>(null);
  const parts = splitNameTemplate(value);
  let offset = 0;

  const syncScroll = (event: UIEvent<HTMLInputElement>) => {
    if (backdropRef.current) {
      backdropRef.current.scrollLeft = event.currentTarget.scrollLeft;
    }
    onScroll?.(event);
  };

  return (
    <div className="relative w-full">
      <div
        ref={backdropRef}
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 flex items-center overflow-hidden whitespace-pre rounded-lg border border-transparent px-2.5 py-1 text-ui text-foreground"
        data-testid="task-name-template-overlay"
      >
        <span className="w-max shrink-0 whitespace-pre">
          {parts.map((part) => {
            const key = offset;
            offset += part.text.length;
            const displayedText = part.text.replace(/[\r\n]/g, "");
            return part.kind === "placeholder" ? (
              <mark
                key={key}
                className="rounded-sm bg-accent/50 text-accent-foreground"
                data-template-placeholder
              >
                {displayedText}
              </mark>
            ) : (
              <span key={key}>{displayedText}</span>
            );
          })}
        </span>
      </div>
      <Input
        {...inputProps}
        className={cn(
          className,
          "relative bg-transparent dark:bg-transparent text-transparent caret-foreground selection:bg-accent selection:text-accent-foreground",
        )}
        onScroll={syncScroll}
        value={value}
      />
    </div>
  );
}

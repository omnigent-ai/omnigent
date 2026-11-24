import { personaColor, personaInitials } from "@/lib/personaAvatar";
import { cn } from "@/lib/utils";

const SIZES = {
  xs: "size-4 text-[0.5rem]",
  sm: "size-5 text-[0.5625rem]",
  md: "size-6 text-[0.625rem]",
} as const;

/** A persona drawn as a name-colored circle with one or two initials. */
export function PersonaAvatar({
  name,
  size = "sm",
  className,
}: {
  name: string;
  size?: keyof typeof SIZES;
  className?: string;
}) {
  return (
    <span
      aria-hidden
      data-testid="persona-avatar"
      data-persona={name}
      className={cn(
        "inline-flex shrink-0 select-none items-center justify-center rounded-full font-semibold leading-none tracking-tight text-white",
        SIZES[size],
        className,
      )}
      style={{ backgroundColor: personaColor(name) }}
    >
      {personaInitials(name)}
    </span>
  );
}

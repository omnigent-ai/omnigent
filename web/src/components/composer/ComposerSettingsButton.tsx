import type { ComponentPropsWithoutRef } from "react";
import { SettingsIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { COMPOSER_COLLAPSED_LABEL_CLASS } from "./ChatComposer";

export function ComposerSettingsButton({
  className,
  ...props
}: Omit<ComponentPropsWithoutRef<typeof Button>, "children">) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      aria-label="Advanced settings"
      title="Advanced settings"
      className={cn(
        "h-8 w-auto shrink-0 gap-1 rounded-lg px-2 text-ui font-normal text-muted-foreground hover:text-foreground md:h-7",
        className,
      )}
      {...props}
    >
      <SettingsIcon className="size-4 shrink-0" />
      <span className={COMPOSER_COLLAPSED_LABEL_CLASS}>Advanced settings</span>
    </Button>
  );
}

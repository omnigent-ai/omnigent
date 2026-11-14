import { useState } from "react";
import { PlugIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * An operator-configured service icon, or the default plug when none is set or
 * the image fails to load. `className` sizes both, so swapping never shifts layout.
 */
export function McpServiceIcon({
  service,
  className,
}: {
  service: { icon?: string | null };
  className?: string;
}) {
  const [failedIcon, setFailedIcon] = useState<string | null>(null);
  const icon = service.icon;
  if (!icon || icon === failedIcon) {
    return <PlugIcon aria-hidden="true" className={cn("shrink-0", className)} />;
  }
  return (
    <img
      src={icon}
      alt=""
      loading="lazy"
      referrerPolicy="no-referrer"
      draggable={false}
      onError={() => setFailedIcon(icon)}
      className={cn("shrink-0 object-contain", className)}
    />
  );
}

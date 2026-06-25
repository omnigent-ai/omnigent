import { RouteIcon } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";

/**
 * Strip the ``databricks-`` provider prefix for a compact pill label.
 *
 * ``"databricks-claude-opus-4-7"`` -> ``"claude-opus-4-7"``. Non-prefixed
 * model ids pass through unchanged. The full id stays in the tooltip.
 *
 * @param model - The model id to shorten, e.g. ``"databricks-gpt-5-5"``.
 * @returns The model id without the ``databricks-`` prefix.
 */
export function shortAdvisorModel(model: string): string {
  return model.startsWith("databricks-") ? model.slice("databricks-".length) : model;
}

/**
 * Pill surfacing that the cost-control advisor auto-picked this session's
 * model tier. Rendered next to the context ring; reads the tier/model the
 * store hydrated from the ``cost_control.{tier,model}`` session labels.
 * Hidden unless an advisor actually routed this session. The expensive
 * tier reads in the accent color, the cheap tier muted, so the cost weight
 * is glanceable; the tooltip names the full model and how to override.
 */
export function AdvisorBadge() {
  const tier = useChatStore((s) => s.costControlTier);
  const model = useChatStore((s) => s.costControlModel);
  const conversationId = useChatStore((s) => s.conversationId);

  if (!conversationId || !tier) return null;
  const isExpensive = tier === "expensive";
  // Prefer the concrete model on the pill; fall back to the tier word for
  // sessions judged before the model label existed.
  const pillLabel = model ? shortAdvisorModel(model) : tier;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            "inline-flex h-6 select-none items-center gap-1 rounded-full border px-2 text-xs font-medium",
            isExpensive
              ? "border-primary/30 bg-primary/10 text-primary"
              : "border-border bg-muted text-muted-foreground",
          )}
          aria-label={`Cost advisor routed this session to the ${tier} tier${
            model ? ` using ${model}` : ""
          }`}
        >
          <RouteIcon className="size-3" aria-hidden="true" />
          <span className="font-mono">{pillLabel}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-64 text-center text-xs">
        <p>
          Cost advisor routed this chat to the <span className="font-semibold">{tier}</span> tier
          {model ? (
            <>
              {" "}
              → <span className="font-mono">{model}</span>
            </>
          ) : null}
          .
        </p>
        <p className="mt-1 text-muted-foreground">
          Type <span className="font-mono">/model &lt;name&gt;</span> to override.
        </p>
      </TooltipContent>
    </Tooltip>
  );
}

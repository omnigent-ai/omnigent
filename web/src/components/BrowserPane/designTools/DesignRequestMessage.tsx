// A design-tool message's body in the chat, as a compact card: what was asked and what it
// was about. It renders inside the ordinary user bubble; desktop design mode sends the same
// format, so its messages get the card too.
import { MousePointerClickIcon, PencilLineIcon, type LucideIcon } from "lucide-react";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { DesignRequestContent, type DesignRequestCardProps } from "./DesignRequestContent";
import type { DesignRequest } from "./parseDesignRequest";

const KIND_ICONS: Record<DesignRequest["kind"], LucideIcon> = {
  element: MousePointerClickIcon,
  drawing: PencilLineIcon,
};

/** A design-tool message's text, compact: the request and its subject. */
export function DesignRequestBodyView({ request, words }: DesignRequestCardProps) {
  const Icon = KIND_ICONS[request.kind];
  return (
    <div data-testid="design-request" className="flex min-w-0 flex-col gap-1">
      <DesignRequestContent
        request={request}
        words={words}
        icon={<Icon className="mt-0.5 size-3.5 shrink-0" />}
      />
    </div>
  );
}

/** A design-tool message's body in the chat; if the card fails, the same content plainly. */
export function DesignRequestBody(props: DesignRequestCardProps) {
  return (
    <RenderErrorBoundary
      name="the design request card"
      fallback={
        <div className="flex min-w-0 flex-col gap-1">
          {/* Without `words`: the chat's render of them is what most likely failed. */}
          <DesignRequestContent request={props.request} />
        </div>
      }
    >
      <DesignRequestBodyView {...props} />
    </RenderErrorBoundary>
  );
}

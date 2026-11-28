import { useState } from "react";
import { Loader2Icon } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type Gadget,
  type GadgetTemplateShare,
  createGadgetFromTemplate,
  listGadgetTemplateShares,
} from "@/gadgets/api/gadgetApi";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { gadgetTemplatesAvailable } from "@/lib/permissionsApi";
import { cn } from "@/lib/utils";
import { GadgetErrorNotice } from "./GadgetErrorNotice";
import { GADGETS_KEY, invalidateGadgets } from "./gadgetQueries";

const TEMPLATE_SHARES_KEY = [...GADGETS_KEY, "template-shares"] as const;

/**
 * Gadgets shared with the viewer as templates, beside the built-in ones. Choosing one makes
 * the viewer's own gadget from its published code and starter data; the template itself never
 * opens. Shown only where the server shares gadgets.
 */
export function SharedGadgetTemplates({
  onCreated,
  minCardWidth = 200,
}: {
  onCreated: (gadget: Gadget) => void | Promise<void>;
  minCardWidth?: number;
}) {
  const info = useServerInfo();
  const enabled = gadgetTemplatesAvailable(info);
  const queryClient = useQueryClient();
  const shares = useQuery({
    queryKey: TEMPLATE_SHARES_KEY,
    queryFn: ({ signal }) => listGadgetTemplateShares(signal),
    enabled,
  });
  const [creating, setCreating] = useState<string | null>(null);
  const [failed, setFailed] = useState<{ share: GadgetTemplateShare; error: unknown } | null>(null);
  // A gadget made whose next step (showing it beside a session) failed: trying again goes on
  // from it instead of making another.
  const [made, setMade] = useState<{ shareId: string; gadget: Gadget } | null>(null);
  if (!enabled) return null;
  if (shares.isError && !shares.data) {
    // Not the same as none shared: say so, and let the viewer try again.
    return (
      <GadgetErrorNotice
        title="Couldn’t load the templates shared with you"
        error={shares.error}
        onRetry={() => void shares.refetch()}
      />
    );
  }
  if (!shares.data?.length) return null;

  const create = async (share: GadgetTemplateShare) => {
    if (creating !== null) return;
    setCreating(share.id);
    setFailed(null);
    try {
      let gadget = made?.shareId === share.id ? made.gadget : null;
      if (gadget === null) {
        gadget = await createGadgetFromTemplate(share.id);
        setMade({ shareId: share.id, gadget });
        invalidateGadgets(queryClient);
      }
      await onCreated(gadget);
      setMade(null);
    } catch (error) {
      setFailed({ share, error });
    } finally {
      setCreating(null);
    }
  };

  return (
    <section aria-label="Shared with you" className="flex flex-col gap-2">
      <h3 className="text-sm font-medium text-muted-foreground">Shared with you</h3>
      <ul
        className="m-0 grid list-none gap-3 p-0"
        style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${minCardWidth}px, 1fr))` }}
      >
        {shares.data.map((share) => (
          <li
            key={share.id}
            aria-busy={creating === share.id || undefined}
            className={cn(
              "relative flex flex-col gap-1 rounded-[20px] border border-border bg-card p-3 transition-colors",
              "hover:border-foreground/20 hover:bg-muted/50",
              "has-[button:focus-visible]:ring-3 has-[button:focus-visible]:ring-ring/50",
              creating === share.id && "opacity-70",
            )}
          >
            <button
              type="button"
              aria-label={share.name}
              aria-describedby={`gadget-template-share-${share.id}`}
              disabled={creating !== null}
              onClick={() => void create(share)}
              className="flex cursor-pointer items-center gap-2 text-left text-ui font-medium outline-none after:absolute after:inset-0 after:content-[''] disabled:cursor-default"
            >
              {share.name}
              {creating === share.id && (
                <Loader2Icon className="size-3.5 animate-spin text-muted-foreground" aria-hidden />
              )}
            </button>
            <span
              id={`gadget-template-share-${share.id}`}
              className="text-sm text-muted-foreground"
            >
              From {share.owner_email}
            </span>
          </li>
        ))}
      </ul>
      {failed && (
        <GadgetErrorNotice
          title="Couldn’t create the gadget"
          error={failed.error}
          onRetry={() => void create(failed.share)}
        />
      )}
    </section>
  );
}

// Starting a new gadget: describe it, or make one from a template. The New gadget dialog and the
// create page show it. Build makes the gadget and opens New session with the gadget beside the
// composer and the build instructions in it; the viewer picks where the session runs and sends it.
import { type RefObject, useCallback, useEffect, useRef } from "react";
import { ChevronRightIcon } from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Link, useNavigate } from "@/lib/routing";
import { type Gadget, createGadget, deleteGadget } from "@/gadgets/api/gadgetApi";
import { composeGadgetBuildPrompt, gadgetNameFrom } from "@/gadgets/host/gadgetBuild";
import { gadgetBoardNewSessionUrl, gadgetBoardPath } from "@/gadgets/host/gadgetPages";
import { markGadgetSessionPending } from "@/gadgets/host/gadgetSession";
import type { GadgetTemplate } from "@/gadgets/templates/gadgetTemplates";
import { GadgetDescribeForm } from "./GadgetDescribeForm";
import { GadgetTemplatesSection } from "./GadgetTemplateGallery";
import { IN_APP_GADGET_OPEN, appPathFromUrl } from "./gadgetNavigation";
import { rememberUnbuiltGadget } from "./useAdoptStartedGadgetSession";

export const GADGET_INTRO =
  "A gadget is a small app you build with an agent: it keeps its own data, hands work to agents, and you can share it.";

const START_EXPLANATION = (
  <>
    <span className="block text-foreground">{GADGET_INTRO}</span>
    <span className="block">
      Say what it shows, what data it uses and what people do with it. Build opens a new session
      with the gadget beside it; send the message there to build it.
    </span>
  </>
);

function GadgetStartContent({
  onCancel,
  onLeave,
  compact = false,
}: {
  onCancel?: () => void;
  /** After it opens the new gadget somewhere else. */
  onLeave?: () => void;
  /** In the dialog, the description grows less, keeping the templates in view. */
  compact?: boolean;
}) {
  const navigate = useNavigate();
  // Closing New gadget, or Cancel, while a create is under way: the gadget it then makes is
  // deleted instead of opened.
  const dismissed = useRef(false);
  useEffect(() => {
    dismissed.current = false;
    return () => {
      dismissed.current = true;
    };
  }, []);
  const openNewSession = useCallback(
    (gadget: Gadget, prompt: string) => {
      markGadgetSessionPending(gadget.id);
      // Leaving New session without sending the message deletes it again.
      rememberUnbuiltGadget(gadget.id);
      navigate(appPathFromUrl(gadgetBoardNewSessionUrl(gadget.id, prompt)));
      onLeave?.();
    },
    [navigate, onLeave],
  );
  const build = async (description: string) => {
    const gadget = await createGadget(gadgetNameFrom(description));
    if (dismissed.current) {
      void deleteGadget(gadget.id).catch(() => undefined);
      return;
    }
    openNewSession(gadget, composeGadgetBuildPrompt(gadget, description));
  };
  const onTemplateCreated = (gadget: Gadget, template: GadgetTemplate) => {
    if (dismissed.current) {
      // A template's gadget is complete; a blank one has nothing in it yet.
      if (!template.revision) void deleteGadget(gadget.id).catch(() => undefined);
      return;
    }
    if (template.revision) {
      navigate(gadgetBoardPath(gadget.id), { state: IN_APP_GADGET_OPEN });
      onLeave?.();
    } else {
      // A blank gadget is built in a new session, which then shows it beside itself.
      openNewSession(gadget, composeGadgetBuildPrompt(gadget));
    }
  };
  return (
    <>
      <GadgetDescribeForm
        explanation={START_EXPLANATION}
        onBuild={build}
        onCancel={() => {
          dismissed.current = true;
          onCancel?.();
        }}
        errorTitle="Couldn’t create the gadget"
        compact={compact}
      />
      <GadgetTemplatesSection onCreated={onTemplateCreated} />
    </>
  );
}

/** New gadget, opened by the Gadget page's New as Automations' New opens New automation. */
export function GadgetStartDialog({
  open,
  onOpenChange,
  returnFocusRef,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Takes focus back when the dialog closes, such as the button that opened it. */
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const close = useCallback(() => onOpenChange(false), [onOpenChange]);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        aria-describedby={undefined}
        // A fixed height, so a growing description or an error scrolls inside instead of moving
        // the dialog.
        className="flex h-[min(680px,90vh)] flex-col gap-0 overflow-hidden p-0 sm:max-w-[720px]"
        data-testid="new-gadget-dialog"
        onCloseAutoFocus={(event) => {
          const target = returnFocusRef?.current;
          if (!target?.isConnected) return;
          event.preventDefault();
          target.focus();
        }}
      >
        <DialogHeader className="shrink-0 px-6 pt-6 pb-0">
          <DialogTitle>New gadget</DialogTitle>
        </DialogHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-6 py-5">
          {open && <GadgetStartContent onCancel={close} onLeave={close} compact />}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** `/gadget/new`: the dialog's content as a page. */
export function GadgetStartPage() {
  const navigate = useNavigate();
  return (
    <PageScroll
      maxWidthClassName="max-w-4xl"
      className="[scrollbar-gutter:stable]"
      contentClassName="px-4 md:px-8"
    >
      <nav
        aria-label="Breadcrumb"
        className="mb-1 flex items-center gap-1 text-ui text-muted-foreground"
      >
        <Link to="/gadget" className="hover:text-foreground">
          Gadget
        </Link>
        <ChevronRightIcon className="size-3.5" aria-hidden />
      </nav>
      <h1 className="mb-6 text-2xl font-medium">New gadget</h1>
      <div className="flex flex-col gap-6">
        <GadgetStartContent onCancel={() => navigate("/gadget")} />
      </div>
    </PageScroll>
  );
}

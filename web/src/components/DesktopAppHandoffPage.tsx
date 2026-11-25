import omnigentLogo from "@/assets/omnigent-starfish-icon.png";
import { Button } from "@/components/ui/button";
import { continueInBrowser, desktopAppDeepLink } from "@/lib/desktopAppHandoff";

/**
 * Shown in place of a conversation the desktop app just took from this tab.
 *
 * @param conversationId - The conversation that was handed over.
 */
export function DesktopAppHandoffPage({ conversationId }: { conversationId: string }) {
  const deepLink = desktopAppDeepLink(conversationId);
  return (
    <div
      className="fixed inset-0 z-[100] flex flex-col items-center justify-center gap-6 bg-background p-6 text-center text-foreground"
      data-testid="desktop-app-handoff-page"
    >
      <img src={omnigentLogo} alt="" className="size-16 object-contain" />
      <div className="flex flex-col gap-1">
        <h1 className="text-lg font-medium" role="status">
          Opening this conversation in the desktop app
        </h1>
        {deepLink && <p className="text-sm text-muted-foreground">Nothing happened?</p>}
      </div>
      <div className="flex flex-col items-center gap-2">
        {deepLink && (
          <Button asChild>
            <a href={deepLink}>Open the desktop app</a>
          </Button>
        )}
        <Button variant="ghost" onClick={continueInBrowser}>
          Use the browser instead
        </Button>
      </div>
    </div>
  );
}

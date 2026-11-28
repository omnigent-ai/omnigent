// The desktop app's design-tool platform (core/designPlatform.ts), one for each Browser view
// while its toolbar is mounted. Each page operation is a request to main, which builds and
// runs the page's script itself; screenshots are main's captures of the page alone.
// Live Edit's page state belongs to a scope main hands out, begun on a session's first use.
import type { DesignPage, DesignPlatform } from "./core/designPlatform";
import type { Answer } from "./desktopBridge";
import type { CssRect, Shape } from "./core/designToolsStore";
import { drawingScreenshot, elementScreenshot } from "./desktopCapture";
import type { DesignOverlay } from "./desktopOverlay";

/** The platform of the Browser view `overlay` shows its tools on. */
export function desktopDesignPlatform(overlay: DesignOverlay): DesignPlatform {
  const result = async (op: string, args: Record<string, unknown>) =>
    (await overlay.request(op, args)).result ?? "";
  // Core's Live Edit scope, to main's for it (once its begin has answered).
  const scopes = new Map<string, Promise<string>>();
  const mainScope = (scope: string) => {
    let begun = scopes.get(scope);
    if (!begun) {
      begun = overlay.request("liveEditBegin").then((answer) => {
        if (typeof answer.scope !== "string") throw new Error("Live Edit couldn't start.");
        return answer.scope;
      });
      // A begin that failed is tried again by the next operation.
      begun.catch(() => {
        if (scopes.get(scope) === begun) scopes.delete(scope);
      });
      scopes.set(scope, begun);
    }
    return begun;
  };
  // End `scope` in the page if it began there. One that fails keeps its scope, so the
  // session's second try still ends it (main keeps a scope whose end the page didn't take).
  const end = async (scope: string, texts: [number, string][] | null, restore = false) => {
    const begun = scopes.get(scope);
    const main = await begun?.catch(() => null);
    if (!main) {
      if (scopes.get(scope) === begun) scopes.delete(scope);
      return "";
    }
    const answer = await overlay
      .endLiveEdit(main, texts, restore)
      .catch((error: unknown): Answer => ({ ok: false, error: String(error) }));
    if (!answer.ok) throw new Error(answer.error ?? "The page didn't answer.");
    if (scopes.get(scope) === begun) scopes.delete(scope);
    return answer.result ?? "";
  };
  const page: DesignPage = {
    hitTest: (x, y) => result("hitTest", { x, y }),
    scroll: (x, y, dx, dy, modifiers = []) =>
      result("scroll", { x, y, dx, dy, ...(modifiers.length > 0 ? { modifiers } : {}) }),
    liveEditPick: async (scope, x, y) =>
      result("liveEditPick", { scope: await mainScope(scope), x, y }),
    liveEditPreview: async (scope, handle, text) =>
      result("liveEditPreview", { scope: await mainScope(scope), handle, text }),
    liveEditRestore: async (scope) => result("liveEditRestore", { scope: await mainScope(scope) }),
    liveEditSettle: (scope, texts) => end(scope, texts),
    liveEditForget: (scope) => end(scope, null),
    liveEditDiscard: (scope) => end(scope, null, true),
  };
  return {
    page: async () => page,
    capture: {
      screenshot: async (_tabId: string, marks: readonly Shape[]) =>
        drawingScreenshot(await overlay.capture(), marks),
      element: (_tabId: string, rect: CssRect) => elementScreenshot(overlay, rect),
    },
  };
}

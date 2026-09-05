import { createRoot, type Root } from "react-dom/client";
import {
  defineExtension,
  type Disposable,
  type ExtensionContext,
} from "@omnigent/extension-sdk";
import { CanvasApp } from "./CanvasApp";
import "@xyflow/react/dist/style.css";
import "./style.css";

let root: Root | null = null;
let themeSubscription: Disposable | null = null;
let activationGeneration = 0;
let cancelPendingReady: (() => void) | null = null;

function setTheme(theme: "light" | "dark"): void {
  document.documentElement.dataset.theme = theme;
}

function clearActiveResources(): void {
  cancelPendingReady?.();
  cancelPendingReady = null;
  themeSubscription?.dispose();
  themeSubscription = null;
  root?.unmount();
  root = null;
}

defineExtension({
  async activate(context: ExtensionContext) {
    const generation = ++activationGeneration;
    clearActiveResources();
    const container = document.getElementById("root");
    if (!container) throw new Error("Canvas root is missing");
    let resolveReady!: () => void;
    const canvasReady = new Promise<void>((resolve) => {
      resolveReady = resolve;
    });
    cancelPendingReady = resolveReady;
    root = createRoot(container);
    root.render(
      <CanvasApp
        context={context}
        onReady={() => {
          if (generation !== activationGeneration) return;
          resolveReady();
          if (cancelPendingReady === resolveReady) cancelPendingReady = null;
        }}
      />,
    );
    const subscription = await context.theme.subscribe((theme) =>
      setTheme(theme.theme),
    );
    if (generation !== activationGeneration) {
      subscription.dispose();
      return;
    }
    themeSubscription = subscription;
    await canvasReady;
  },
  deactivate() {
    activationGeneration += 1;
    clearActiveResources();
  },
});

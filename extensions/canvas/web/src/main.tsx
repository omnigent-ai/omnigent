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

function setTheme(theme: "light" | "dark"): void {
  document.documentElement.dataset.theme = theme;
}

function clearActiveResources(): void {
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
    root = createRoot(container);
    root.render(<CanvasApp context={context} />);
    const subscription = await context.theme.subscribe((theme) =>
      setTheme(theme.theme),
    );
    if (generation !== activationGeneration) {
      subscription.dispose();
      return;
    }
    themeSubscription = subscription;
  },
  deactivate() {
    activationGeneration += 1;
    clearActiveResources();
  },
});

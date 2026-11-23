import * as React from "react";
import * as ToolbarPrimitive from "radix-ui/toolbar";

// A row of controls the keyboard enters at one stop: the arrows, Home and End move between them.
// forwardRef, as Button's: on React 18 a plain function component drops an asChild parent's ref.
const Toolbar = React.forwardRef<
  React.ElementRef<typeof ToolbarPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof ToolbarPrimitive.Root>
>((props, ref) => <ToolbarPrimitive.Root ref={ref} data-slot="toolbar" {...props} />);
Toolbar.displayName = "Toolbar";

const ToolbarButton = React.forwardRef<
  React.ElementRef<typeof ToolbarPrimitive.Button>,
  React.ComponentPropsWithoutRef<typeof ToolbarPrimitive.Button>
>((props, ref) => <ToolbarPrimitive.Button ref={ref} data-slot="toolbar-button" {...props} />);
ToolbarButton.displayName = "ToolbarButton";

export { Toolbar, ToolbarButton };

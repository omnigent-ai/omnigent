import type { Meta, StoryObj } from "@storybook/react-vite";
import { BellIcon, CheckIcon, InfoIcon, TriangleAlertIcon } from "lucide-react";
import { Alert, AlertAction, AlertDescription, AlertTitle } from "./alert";
import { Avatar, AvatarBadge, AvatarFallback, AvatarGroup, AvatarGroupCount } from "./avatar";
import { Badge } from "./badge";
import { Button } from "./button";
import { Input } from "./input";
import { Switch } from "./switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./tabs";

const buttonVariants = ["default", "secondary", "outline", "ghost", "destructive", "link"] as const;
const badgeVariants = ["default", "secondary", "outline", "ghost", "destructive", "link"] as const;
const panel = "space-y-2 rounded-lg border bg-card p-3";

const meta = {
  title: "Foundations/Primitives",
  tags: ["visual-snapshot"],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const Gallery: Story = {
  render: () => (
    <div className="w-[720px] max-w-full space-y-3 text-foreground">
      <header>
        <h1 className="text-lg font-semibold">Foundation primitives</h1>
        <p className="text-sm text-muted-foreground">Durable variants and component states.</p>
      </header>
      <div className="grid gap-3 sm:grid-cols-2">
        <section className={`${panel} sm:col-span-2`}>
          <h2 className="font-medium">Buttons</h2>
          <div className="flex flex-wrap gap-2">
            {buttonVariants.map((variant) => (
              <Button key={variant} variant={variant}>
                {variant}
              </Button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="xs">XS</Button>
            <Button size="sm">Small</Button>
            <Button>Default</Button>
            <Button size="lg">Large</Button>
            <Button variant="outline" size="icon-sm" aria-label="Notifications">
              <BellIcon />
            </Button>
            <Button loading>Loading</Button>
            <Button disabled>Disabled</Button>
          </div>
        </section>
        <section className={panel}>
          <h2 className="font-medium">Badges</h2>
          <div className="flex flex-wrap gap-2">
            {badgeVariants.map((variant, index) => (
              <Badge key={variant} variant={variant}>
                {index === 0 && <CheckIcon data-icon="inline-start" />}
                {variant}
              </Badge>
            ))}
          </div>
        </section>
        <section className={panel}>
          <h2 className="font-medium">Avatars</h2>
          <div className="flex items-center gap-3">
            <Avatar size="lg">
              <AvatarFallback>OM</AvatarFallback>
              <AvatarBadge>
                <CheckIcon />
              </AvatarBadge>
            </Avatar>
            <Avatar>
              <AvatarFallback>UI</AvatarFallback>
            </Avatar>
            <AvatarGroup>
              <Avatar>
                <AvatarFallback>AA</AvatarFallback>
              </Avatar>
              <Avatar>
                <AvatarFallback>BB</AvatarFallback>
              </Avatar>
              <AvatarGroupCount>+3</AvatarGroupCount>
            </AvatarGroup>
          </div>
        </section>
        <section className={panel}>
          <h2 className="font-medium">Inputs</h2>
          <Input aria-label="Default input" placeholder="Placeholder" />
          <Input aria-label="Invalid input" aria-invalid="true" defaultValue="Invalid value" />
          <Input aria-label="Disabled input" defaultValue="Disabled value" disabled />
        </section>
        <section className={panel}>
          <h2 className="font-medium">Switches</h2>
          <SwitchRow label="Checked">
            <Switch aria-label="Checked switch" defaultChecked />
          </SwitchRow>
          <SwitchRow label="Small">
            <Switch aria-label="Small switch" size="sm" />
          </SwitchRow>
          <SwitchRow label="Disabled">
            <Switch aria-label="Disabled switch" defaultChecked disabled />
          </SwitchRow>
        </section>
        <section className={`${panel} sm:col-span-2`}>
          <h2 className="font-medium">Tabs</h2>
          <div className="grid gap-4 sm:grid-cols-3">
            <TabsExample variant="default" />
            <TabsExample variant="line" />
            <TabsExample variant="pill" />
          </div>
        </section>
        <section className={`${panel} sm:col-span-2`}>
          <h2 className="font-medium">Alerts</h2>
          <div className="grid gap-2 sm:grid-cols-2">
            <Alert>
              <InfoIcon />
              <AlertTitle>Informational alert</AlertTitle>
              <AlertDescription>Title, description, icon, and action.</AlertDescription>
              <AlertAction>
                <Button variant="outline" size="xs">
                  Action
                </Button>
              </AlertAction>
            </Alert>
            <Alert variant="destructive">
              <TriangleAlertIcon />
              <AlertTitle>Destructive alert</AlertTitle>
              <AlertDescription>A concise failure using destructive tokens.</AlertDescription>
            </Alert>
          </div>
        </section>
      </div>
    </div>
  ),
};

function SwitchRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between rounded-md border px-2.5 py-2">
      <span className="text-ui">{label}</span>
      {children}
    </div>
  );
}

function TabsExample({ variant }: { variant: "default" | "line" | "pill" }) {
  return (
    <Tabs defaultValue="active">
      <TabsList variant={variant} aria-label={`${variant} tabs`}>
        <TabsTrigger value="active">Active</TabsTrigger>
        <TabsTrigger value="idle">Idle</TabsTrigger>
        <TabsTrigger value="disabled" disabled>
          Disabled
        </TabsTrigger>
      </TabsList>
      <TabsContent value="active" className="pt-1 text-muted-foreground">
        Active content
      </TabsContent>
    </Tabs>
  );
}

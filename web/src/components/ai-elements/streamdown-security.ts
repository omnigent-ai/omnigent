import { cjk } from "@streamdown/cjk";
import { code } from "@streamdown/code";
import { math } from "@streamdown/math";
import { mermaid } from "@streamdown/mermaid";
import { defaultRehypePlugins, type LinkSafetyConfig, type StreamdownProps } from "streamdown";

type StreamdownRehypePlugins = NonNullable<StreamdownProps["rehypePlugins"]>;
type StreamdownRehypePlugin = StreamdownRehypePlugins[number];
type StreamdownPluginTuple = Extract<StreamdownRehypePlugin, readonly unknown[]>;
type StreamdownHardenOptions = {
  allowedImagePrefixes: string[];
  allowedLinkPrefixes?: string[];
  allowedProtocols?: string[];
  allowDataImages?: boolean;
  defaultOrigin?: string;
};
type StreamdownHardenPlugin = StreamdownPluginTuple & {
  1: StreamdownHardenOptions;
};

export const STREAMDOWN_PLUGINS = { cjk, code, math, mermaid };
export const SECURE_STREAMDOWN_REHYPE_PLUGINS = createSecureStreamdownRehypePlugins();

// Streamdown enables a link-safety confirmation modal by default: clicking any
// markdown link pops an "Open external link?" dialog instead of following the
// link. Disable it so chat links behave like ordinary links — a plain click
// opens them and cmd/ctrl-click opens a new tab. With safety off Streamdown
// still renders the anchor as `<a target="_blank" rel="noreferrer">`, so we
// keep new-tab opening plus referrer/reverse-tabnabbing protection without the
// extra confirmation click.
export const CHAT_LINK_SAFETY: LinkSafetyConfig = { enabled: false };

function isStreamdownHardenPlugin(
  plugin: StreamdownRehypePlugin,
): plugin is StreamdownHardenPlugin {
  return Array.isArray(plugin) && plugin.length >= 2 && isHardenOptions(plugin[1]);
}

function isHardenOptions(value: unknown): value is StreamdownHardenOptions {
  return (
    typeof value === "object" &&
    value !== null &&
    "allowedImagePrefixes" in value &&
    Array.isArray(value.allowedImagePrefixes)
  );
}

function createSecureStreamdownRehypePlugins(): StreamdownRehypePlugins {
  return Object.entries(defaultRehypePlugins).map(([key, plugin]) => {
    if (key !== "harden") {
      return plugin;
    }

    if (!isStreamdownHardenPlugin(plugin)) {
      throw new Error("Streamdown harden plugin must be a [plugin, options] tuple");
    }

    return [plugin[0], { ...plugin[1], allowedImagePrefixes: [] }] satisfies StreamdownPluginTuple;
  });
}

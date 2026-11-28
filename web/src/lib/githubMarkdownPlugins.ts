import { rehypeGitHubAlerts } from "@/lib/githubAlerts";
import { rehypeGitHubEmoji } from "@/lib/githubEmojiShortcodes";
import { rehypeMentions, rehypePullRequestReferences } from "@/lib/pullRequestReferences/rehype";

/**
 * GitHub's own Markdown for PR descriptions and comments: alerts, `:shortcode:` emoji, PR
 * references and mentions. They add their own markup, so they run after sanitizing.
 */
export const GITHUB_MARKDOWN_REHYPE_PLUGINS = [
  rehypeGitHubAlerts,
  rehypeGitHubEmoji,
  rehypePullRequestReferences,
  rehypeMentions,
];

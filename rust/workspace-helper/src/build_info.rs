//! The build this binary identifies as. A release build sets
//! `WORKSPACE_HELPER_BUILD_VERSION` to the release version and
//! `WORKSPACE_HELPER_BUILD_COMMIT` to the newest commit that changed the helper's
//! sources. A plain `cargo build` is a dev build without a commit.

/// Incompatible protocol changes bump this; additions are capabilities.
pub const PROTOCOL_VERSION: u32 = 1;

/// What GetCapabilities and GetInfo list: the WorkspaceService methods this
/// helper serves, then its optional features, so clients can negotiate per call.
pub const CAPABILITIES: &[&str] = &[
    "ListDirectory",
    "ReadFile",
    "StatPath",
    "SearchFiles",
    "SearchText",
    "ResolveWorkingCopyBase",
    "ListWorkingCopyChanges",
    "ReadBaseFile",
    "ReadLinkText",
    "ListRepositoryRefs",
    "ListBranchCommits",
    "ResolveRepositoryContext",
    "search_files.fuzzy",
    "search_files.substring",
    "gitignore",
    "text_search_priority",
    "revision_comparison",
    "open_workspace.ahead_of_search",
    "branch_commits",
];

/// The release version, or `dev` for a local build.
pub fn version() -> &'static str {
    display_version(option_env!("WORKSPACE_HELPER_BUILD_VERSION").unwrap_or_default())
}

pub fn commit() -> &'static str {
    option_env!("WORKSPACE_HELPER_BUILD_COMMIT").unwrap_or_default()
}

fn display_version(version: &str) -> &str {
    match version {
        "" | "0.0.0" => "dev",
        version => version,
    }
}

#[cfg(test)]
#[path = "build_info_test.rs"]
mod tests;

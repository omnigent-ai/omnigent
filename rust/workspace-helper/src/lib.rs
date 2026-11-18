//! The Omnigent workspace helper: a workspace's files, file search, text
//! search, and Git comparisons over gRPC, served to the Omnigent host that
//! starts it.

pub mod build_info;
pub mod file_index;
pub mod git_repository;
pub mod git_runner;
pub mod globs;
pub mod host_reader;
pub mod ignore_rules;
pub mod index_slot;
pub mod listing;
pub mod repository_context;
pub mod revision_comparison;
pub mod search_priority;
pub mod server;
pub mod socket;
pub mod text_search;
pub mod walk;
pub mod walk_ignore;
pub mod working_copy;
pub mod workspace;
pub mod workspace_path;

/// The workspace API and the helper's control service, from
/// `omnigent/api/workspace/v1alpha1/`.
pub mod proto {
    tonic::include_proto!("omnigent.api.workspace.v1alpha1");
}

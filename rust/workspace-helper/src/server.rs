//! The workspace API (`WorkspaceService`) and the host's control service
//! (`WorkspaceHelperService`). Each workspace call names its workspace in the
//! `omnigent-workspace-id` header, which only the host on the private
//! connection sets. Paths are confined to the workspace root as a policy
//! guard; the helper runs as the user and reads only what the user can.

use std::{
    io,
    path::Path,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};

use tokio::sync::mpsc;
use tokio_stream::{Stream, wrappers::ReceiverStream};
use tonic::{Request, Response, Status};

use crate::{
    build_info,
    file_index::{DirectoryScopes, SearchHit},
    globs::PathFilter,
    host_reader, listing,
    proto::{
        self, CloseWorkspaceRequest, CloseWorkspaceResponse, ContentEncoding, FileMetadata,
        FileSearchMode, FileSearchResult, GetCapabilitiesRequest, GetCapabilitiesResponse,
        GetInfoRequest, GetInfoResponse, ListDirectoryRequest, ListDirectoryResponse,
        OpenWorkspaceRequest, OpenWorkspaceResponse, ReadBaseFileResponse, ReadFileRequest,
        ReadFileResponse, RepositoryContextProblem, SearchFilesRequest, SearchFilesResponse,
        SearchTextRequest, SearchTextResponse, StatPathRequest, StatPathResponse, TextSearchMatch,
        workspace_helper_service_server::{
            WorkspaceHelperService as ControlService, WorkspaceHelperServiceServer,
        },
        workspace_service_server::{WorkspaceService, WorkspaceServiceServer},
    },
    repository_context::{self, ContextProblem, PushUnresolved, RepositoryContext},
    revision_comparison::{
        self, RevisionComparison, RevisionField, RevisionRequest, RevisionUnavailable,
    },
    search_priority,
    text_search::{self, Priorities, TextMatch, TextQuery},
    working_copy::{self, ComparisonKind, ReadError, ReadLinkError, Unavailable},
    workspace::{Workspace, Workspaces},
    workspace_path::{PathError, normalize_relative},
};

/// The header the host names each call's workspace in.
pub const WORKSPACE_HEADER: &str = "omnigent-workspace-id";
const READ_CHUNK_BYTES: usize = 64 * 1024;
const DEFAULT_SEARCH_RESULTS: usize = 500;
const MAXIMUM_SEARCH_RESULTS: usize = 10_000;

type ResponseStream<T> = Pin<Box<dyn Stream<Item = Result<T, Status>> + Send>>;

fn status(error: PathError) -> Status {
    match error {
        PathError::Invalid(message) => Status::invalid_argument(message),
        PathError::OutsideWorkspace => Status::permission_denied(error.to_string()),
        PathError::NotFound(_) => Status::not_found(error.to_string()),
        PathError::Io(error) => io_status(&error),
    }
}

fn io_status(error: &io::Error) -> Status {
    match error.kind() {
        io::ErrorKind::NotFound => Status::not_found(error.to_string()),
        io::ErrorKind::PermissionDenied => Status::permission_denied(error.to_string()),
        _ => Status::internal(error.to_string()),
    }
}

/// A contained directory's path relative to the root, through symlinks.
fn workspace_directory(workspace: &Workspace, path: &str) -> Result<String, Status> {
    let (relative, resolved) = workspace.root.resolve(path).map_err(status)?;
    if !resolved.is_dir() {
        return Err(Status::invalid_argument(format!(
            "Not a directory: {relative}"
        )));
    }
    Ok(workspace.root.relative(&resolved).unwrap_or_default())
}

/// Include directories must exist; an exclude that does not exist excludes
/// nothing. Both are compared by their resolved paths.
fn directory_scopes(
    workspace: &Workspace,
    include: &[String],
    exclude: &[String],
) -> Result<DirectoryScopes, Status> {
    let include = include
        .iter()
        .map(|directory| workspace_directory(workspace, directory))
        .collect::<Result<Vec<_>, _>>()?;
    let mut excluded = Vec::new();
    for directory in exclude {
        let normalized = normalize_relative(directory).map_err(status)?;
        if normalized.is_empty() {
            return Err(Status::invalid_argument(
                "The workspace root cannot be excluded",
            ));
        }
        if let Ok(resolved) = workspace_directory(workspace, &normalized) {
            excluded.push(resolved);
        }
        excluded.push(normalized);
    }
    excluded.sort();
    excluded.dedup();
    Ok(DirectoryScopes {
        include,
        exclude: excluded,
    })
}

/// The longest a priority search waits for Git's changed directories once
/// it has searched the focused files and their directories.
const PRIORITY_GIT_WAIT: std::time::Duration = std::time::Duration::from_secs(2);
/// How often a wait for Git checks whether the search was cancelled.
const CANCEL_POLL: std::time::Duration = std::time::Duration::from_millis(20);

/// A priority search's hint as an absolute path inside the workspace, under
/// the same containment as every other request path, and only when no
/// symlink lies on the way: a full walk, which never follows links, couldn't
/// reach it otherwise. The root itself is never a hint.
fn priority_path(workspace: &Workspace, path: &str) -> Option<std::path::PathBuf> {
    let (relative, resolved) = workspace.root.resolve(path).ok()?;
    (!relative.is_empty() && resolved == workspace.root.join(&relative)).then_some(resolved)
}

/// Cancels a search when its client closes the stream, whether or not it has
/// sent anything, and stops `git`. Ends when the returned sender is dropped.
fn cancel_when_closed<T: Send + 'static>(
    stream: &mpsc::Sender<T>,
    cancelled: Arc<AtomicBool>,
    git: Option<tokio::task::AbortHandle>,
) -> tokio::sync::oneshot::Sender<()> {
    let (finished, finished_signal) = tokio::sync::oneshot::channel::<()>();
    let watcher = stream.clone();
    tokio::spawn(async move {
        tokio::select! {
            _ = watcher.closed() => {
                cancelled.store(true, Ordering::Relaxed);
                if let Some(git) = git {
                    git.abort();
                }
            }
            // The search ended (or the sender was dropped): stop watching, so the stream can end.
            _ = finished_signal => {}
        }
    });
    finished
}

/// Git's changed directories once they arrive, or `None` after `wait`, on
/// cancellation, or when the listing gave up.
fn wait_for_changes(
    answer: &std::sync::mpsc::Receiver<Option<Vec<String>>>,
    wait: std::time::Duration,
    cancelled: &AtomicBool,
) -> Option<Vec<String>> {
    let until = std::time::Instant::now() + wait;
    loop {
        if cancelled.load(Ordering::Relaxed) {
            return None;
        }
        let left = until.saturating_duration_since(std::time::Instant::now());
        if left.is_zero() {
            return None;
        }
        match answer.recv_timeout(left.min(CANCEL_POLL)) {
            Ok(listed) => return listed,
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => return None,
        }
    }
}

fn text_match_message(found: TextMatch) -> TextSearchMatch {
    TextSearchMatch {
        path: found.path,
        line: found.line,
        column: found.column,
        text: found.text,
        match_start: found.match_start,
        match_end: found.match_end,
    }
}

/// The workspace API as the helper serves it.
pub fn workspace_server(
    service: WorkspaceHelperService,
) -> WorkspaceServiceServer<WorkspaceHelperService> {
    WorkspaceServiceServer::new(service)
}

/// The host's control service, over the same open workspaces.
pub fn control_server(
    service: WorkspaceHelperService,
) -> WorkspaceHelperServiceServer<WorkspaceHelperService> {
    WorkspaceHelperServiceServer::new(service)
}

fn encoding(is_text: bool) -> ContentEncoding {
    if is_text {
        ContentEncoding::Utf8
    } else {
        ContentEncoding::Binary
    }
}

fn result_limit(requested: u32) -> usize {
    match requested as usize {
        0 => DEFAULT_SEARCH_RESULTS,
        requested => requested.min(MAXIMUM_SEARCH_RESULTS),
    }
}

/// Both services' state; clones share the open workspaces.
#[derive(Clone)]
pub struct WorkspaceHelperService {
    workspaces: Arc<Workspaces>,
    priority_git_wait: std::time::Duration,
}

impl Default for WorkspaceHelperService {
    /// Uses the user's Git global excludes file.
    fn default() -> Self {
        Self::new(crate::ignore_rules::system_global_excludes())
    }
}

impl WorkspaceHelperService {
    pub fn new(global_excludes: Option<std::path::PathBuf>) -> Self {
        Self {
            workspaces: Arc::new(Workspaces::new(global_excludes)),
            priority_git_wait: PRIORITY_GIT_WAIT,
        }
    }

    /// How long a priority search waits for Git's changed directories, for
    /// tests that depend on Git's answer however long it takes.
    #[doc(hidden)]
    pub fn with_priority_git_wait(self, wait: std::time::Duration) -> Self {
        Self {
            priority_git_wait: wait,
            ..self
        }
    }

    /// The workspace a call's header names.
    fn workspace<T>(&self, request: &Request<T>) -> Result<Arc<Workspace>, Status> {
        let id = request
            .metadata()
            .get(WORKSPACE_HEADER)
            .and_then(|value| value.to_str().ok())
            .filter(|id| !id.is_empty())
            .ok_or_else(|| Status::failed_precondition("The call names no open workspace"))?;
        self.workspaces
            .get(id)
            .ok_or_else(|| Status::not_found("The workspace is not open. Open it again."))
    }
}

/// Whether `path` is on disk as the kind the file list recorded, following
/// symlinks. A submodule, a symlink to a directory, or a file replaced by a
/// directory is not a file hit.
fn is_live(path: &Path, is_dir: bool) -> bool {
    std::fs::metadata(path).is_ok_and(|metadata| {
        if is_dir {
            metadata.is_dir()
        } else {
            metadata.is_file()
        }
    })
}

/// Up to `limit + 1` hits that are still live, in the search's order. The
/// file list can be older than the disk, so this ranks extra candidates and
/// widens until enough are live or every match was seen.
fn live_hits(
    indexed: usize,
    scope_directory: &Path,
    limit: usize,
    search: impl Fn(usize) -> (Vec<SearchHit>, bool),
) -> Vec<SearchHit> {
    let wanted = limit + 1;
    let all = indexed.max(wanted);
    let mut candidates = wanted.saturating_mul(2).max(wanted + 32).min(all);
    loop {
        let (hits, more) = search(candidates);
        let live: Vec<SearchHit> = hits
            .into_iter()
            .filter(|hit| is_live(&scope_directory.join(&hit.path), hit.is_dir))
            .take(wanted)
            .collect();
        if live.len() >= wanted || !more || candidates >= all {
            return live;
        }
        candidates = candidates.saturating_mul(2).min(all);
    }
}

#[tonic::async_trait]
impl ControlService for WorkspaceHelperService {
    async fn get_info(
        &self,
        _: Request<GetInfoRequest>,
    ) -> Result<Response<GetInfoResponse>, Status> {
        Ok(Response::new(GetInfoResponse {
            protocol_version: build_info::PROTOCOL_VERSION,
            version: build_info::version().into(),
            commit: build_info::commit().into(),
            capabilities: build_info::CAPABILITIES
                .iter()
                .map(|&name| name.into())
                .collect(),
        }))
    }

    async fn open_workspace(
        &self,
        request: Request<OpenWorkspaceRequest>,
    ) -> Result<Response<OpenWorkspaceResponse>, Status> {
        let request = request.into_inner();
        let workspace = self.workspaces.open(&request.root).map_err(status)?;
        // List tracked files now so the first search does not wait for Git;
        // untracked discovery waits for that search, and so does any walk when opened ahead of one.
        if request.ahead_of_search {
            workspace.files(false).prepare_tracked();
        } else {
            workspace.files(false).prepare();
        }
        Ok(Response::new(OpenWorkspaceResponse {
            workspace_id: workspace.id.clone(),
            root: workspace.root.path().to_string_lossy().into_owned(),
            repository_root: workspace
                .repository_root
                .as_ref()
                .map(|root| root.to_string_lossy().into_owned())
                .unwrap_or_default(),
        }))
    }

    async fn close_workspace(
        &self,
        request: Request<CloseWorkspaceRequest>,
    ) -> Result<Response<CloseWorkspaceResponse>, Status> {
        self.workspaces.close(&request.into_inner().workspace_id);
        Ok(Response::new(CloseWorkspaceResponse {}))
    }
}

#[tonic::async_trait]
impl WorkspaceService for WorkspaceHelperService {
    async fn get_capabilities(
        &self,
        _: Request<GetCapabilitiesRequest>,
    ) -> Result<Response<GetCapabilitiesResponse>, Status> {
        Ok(Response::new(GetCapabilitiesResponse {
            capabilities: build_info::CAPABILITIES
                .iter()
                .map(|&name| name.into())
                .collect(),
        }))
    }

    async fn stat_path(
        &self,
        request: Request<StatPathRequest>,
    ) -> Result<Response<StatPathResponse>, Status> {
        let workspace = self.workspace(&request)?;
        let request = request.into_inner();
        let relative = normalize_relative(&request.path).map_err(status)?;
        // Resolve the parent for containment but describe the leaf itself, so a
        // symlink reports as one.
        let absolute = match relative.rsplit_once('/') {
            _ if relative.is_empty() => workspace.root.path().to_path_buf(),
            Some((parent, name)) => workspace.root.resolve(parent).map_err(status)?.1.join(name),
            None => workspace.root.path().join(&relative),
        };
        let entry =
            tokio::task::block_in_place(|| listing::stat(&workspace.rules, &absolute, &relative))
                .map_err(|error| match error.kind() {
                io::ErrorKind::NotFound => {
                    Status::not_found(format!("No such file or directory: {relative}"))
                }
                _ => io_status(&error),
            })?;
        Ok(Response::new(StatPathResponse { entry: Some(entry) }))
    }

    /// Paths, errors and paging as the host's reader has them; entries also
    /// carry whether they are symlinks or ignored.
    async fn list_directory(
        &self,
        request: Request<ListDirectoryRequest>,
    ) -> Result<Response<ListDirectoryResponse>, Status> {
        let workspace = self.workspace(&request)?;
        let request = request.into_inner();
        if request.page_size < 0 {
            return Err(Status::invalid_argument("page_size must not be negative"));
        }
        let (entries, next_page_token) = tokio::task::spawn_blocking(move || {
            let (relative, resolved) = host_reader::resolve(&workspace, &request.path)?;
            let entries = listing::list(
                &workspace.rules,
                &resolved,
                &relative,
                request.hide_ignored,
                request.collapse_single_child_directories,
            )
            .map_err(|_| host_reader::unlistable(&request.path))?;
            host_reader::page(entries, request.page_size, &request.page_token)
        })
        .await
        .map_err(|error| Status::internal(error.to_string()))??;
        Ok(Response::new(ListDirectoryResponse {
            entries,
            next_page_token,
        }))
    }

    type ReadFileStream = ResponseStream<ReadFileResponse>;

    /// A regular file's bytes up to the host's read limit, metadata first,
    /// exactly as the host's reader delivers them.
    async fn read_file(
        &self,
        request: Request<ReadFileRequest>,
    ) -> Result<Response<Self::ReadFileStream>, Status> {
        let workspace = self.workspace(&request)?;
        let request = request.into_inner();
        if request.path.is_empty() {
            return Err(Status::invalid_argument("path is required"));
        }
        let maximum = match request.maximum_bytes {
            0 => host_reader::MAX_READ_BYTES,
            maximum => (maximum.min(host_reader::MAX_READ_BYTES as u64)) as usize,
        };
        let (file, modified) = tokio::task::spawn_blocking(move || {
            let (_, resolved) = host_reader::resolve(&workspace, &request.path)?;
            let (data, metadata) = host_reader::read_capped(&resolved, &request.path, maximum)?;
            Ok::<_, Status>((
                host_reader::cap_file_bytes(data, maximum),
                listing::modify_time(&metadata),
            ))
        })
        .await
        .map_err(|error| Status::internal(error.to_string()))??;
        let metadata = FileMetadata {
            size: file.data.len() as i64,
            truncated: file.truncated,
            encoding: encoding(file.is_text) as i32,
            // The host fills in the MIME type from its own `mimetypes`.
            content_type: String::new(),
            modify_time: modified,
        };
        let messages = std::iter::once(ReadFileResponse {
            metadata: Some(metadata),
            data: Vec::new(),
        })
        .chain(
            file.data
                .chunks(READ_CHUNK_BYTES)
                .map(|data| ReadFileResponse {
                    metadata: None,
                    data: data.to_vec(),
                }),
        )
        .map(Ok)
        .collect::<Vec<_>>();
        Ok(Response::new(Box::pin(tokio_stream::iter(messages))))
    }

    async fn search_files(
        &self,
        request: Request<SearchFilesRequest>,
    ) -> Result<Response<SearchFilesResponse>, Status> {
        let workspace = self.workspace(&request)?;
        let request = request.into_inner();
        // Search the index under the resolved directory, so a symlinked alias
        // inside the workspace finds what its target holds.
        let scope = workspace_directory(&workspace, &request.path)?;
        let directories = directory_scopes(
            &workspace,
            &request.include_directories,
            &request.exclude_directories,
        )?;
        let limit = result_limit(request.maximum_results);
        let filter = PathFilter::new(&request.include, &request.exclude);
        let substring = FileSearchMode::try_from(request.mode) == Ok(FileSearchMode::Substring);
        let slot = workspace.files(request.include_ignored);
        let generation = slot
            .generation(request.retry_untracked)
            .await
            .ok_or_else(|| Status::unavailable("The workspace was closed"))?;
        let scope_directory = workspace.root.join(&scope);
        let index = &generation.index;
        let (results, truncated) = tokio::task::block_in_place(|| {
            let live = live_hits(index.len(), &scope_directory, limit, |candidates| {
                if substring {
                    index.search_substring(
                        &request.query,
                        &scope,
                        &filter,
                        &directories,
                        candidates,
                    )
                } else {
                    index.search_fuzzy(&request.query, &scope, &filter, &directories, candidates)
                }
            });
            let truncated = live.len() > limit;
            let results = live
                .into_iter()
                .take(limit)
                .filter_map(|hit| {
                    let absolute = scope_directory.join(&hit.path);
                    let name = hit.path.rsplit('/').next().unwrap_or_default();
                    let mut entry = listing::describe(&absolute, name, &hit.path, None)?;
                    entry.ignored = request.include_ignored
                        && workspace.rules.is_ignored(&absolute, hit.is_dir);
                    Some(FileSearchResult {
                        entry: Some(entry),
                        score: hit.score,
                        matched_indices: hit.matched_indices,
                    })
                })
                .collect();
            (results, truncated)
        });
        Ok(Response::new(SearchFilesResponse {
            results,
            truncated,
            indexed_paths: generation.index.len() as u64,
            index_incomplete: generation.incomplete,
            // Read after ranking, so a list published meanwhile is asked for again.
            refreshing: slot.refreshing(&generation),
            untracked_unavailable: generation.untracked_unavailable,
        }))
    }

    type SearchTextStream = ResponseStream<SearchTextResponse>;

    async fn search_text(
        &self,
        request: Request<SearchTextRequest>,
    ) -> Result<Response<Self::SearchTextStream>, Status> {
        let workspace = self.workspace(&request)?;
        let request = request.into_inner();
        let query = TextQuery {
            query: request.query,
            case_sensitive: request.case_sensitive,
            regular_expression: request.regular_expression,
            whole_word: request.whole_word,
            include: request.include,
            exclude: request.exclude,
            limit: match request.maximum_results {
                0 => text_search::DEFAULT_LIMIT,
                limit => limit as usize,
            },
            include_ignored: request.include_ignored,
            include_directories: Vec::new(),
            exclude_directories: Vec::new(),
        };
        let directories = directory_scopes(
            &workspace,
            &request.include_directories,
            &request.exclude_directories,
        )?;
        let query = TextQuery {
            include_directories: directories.include,
            exclude_directories: directories.exclude,
            ..query
        };
        text_search::validate(&query).map_err(Status::invalid_argument)?;
        let prioritized =
            proto::SearchOrder::try_from(request.order) == Ok(proto::SearchOrder::Priority);
        let priorities = Priorities {
            focused_files: request
                .focused_paths
                .iter()
                .take(text_search::MAXIMUM_FOCUSED_FILES)
                .filter_map(|path| priority_path(&workspace, path))
                .collect(),
        };
        let changed_against = match proto::ComparisonKind::try_from(request.changed_against) {
            Ok(proto::ComparisonKind::BranchPoint) => ComparisonKind::BranchPoint,
            _ => ComparisonKind::LastCommit,
        };
        // One deadline for the whole search: its passes and its Git work.
        let search_deadline = std::time::Instant::now() + text_search::TIME_LIMIT;
        let priority_git_wait = self.priority_git_wait;
        let (sender, receiver) = mpsc::channel(16);
        let cancelled = Arc::new(AtomicBool::new(false));

        // A priority search lists the changed directories alongside its first
        // passes, within the search's own deadline.
        let mut changes = prioritized.then(|| {
            let workspace = workspace.clone();
            let (ready, answer) = std::sync::mpsc::channel();
            let task = tokio::spawn(async move {
                let directories = search_priority::changed_directories(
                    workspace.root.path(),
                    changed_against,
                    &workspace.changed_directories,
                    tokio::time::Instant::from_std(search_deadline),
                )
                .await;
                let _ = ready.send(directories);
            });
            (task, answer)
        });

        let finished = cancel_when_closed(
            &sender,
            cancelled.clone(),
            changes.as_ref().map(|(task, _)| task.abort_handle()),
        );

        tokio::task::spawn_blocking(move || {
            let root = workspace.root.path();
            let global_excludes = workspace.rules.global_excludes();
            let (matches, found) = std::sync::mpsc::channel::<TextMatch>();
            let summary = std::thread::scope(|scope| {
                let (batches, cancelled) = (&sender, &*cancelled);
                scope.spawn(move || {
                    text_search::batch_matches(found, |batch| {
                        let response = SearchTextResponse {
                            matches: batch.into_iter().map(text_match_message).collect(),
                            ..SearchTextResponse::default()
                        };
                        if batches.blocking_send(Ok(response)).is_err() {
                            cancelled.store(true, Ordering::Relaxed);
                        }
                    });
                });
                let emit = move |found: TextMatch| {
                    let _ = matches.send(found);
                };
                let summary = if prioritized {
                    let mut changed_directories = || {
                        let (git, answer) = changes.take()?;
                        // Wait at most this long, and never past the search's own deadline.
                        let wait = priority_git_wait.min(
                            search_deadline.saturating_duration_since(std::time::Instant::now()),
                        );
                        let listed = wait_for_changes(&answer, wait, cancelled);
                        git.abort();
                        // Changed paths are workspace-relative; each must resolve inside it.
                        Some(
                            listed?
                                .iter()
                                .filter_map(|directory| priority_path(&workspace, directory))
                                .collect(),
                        )
                    };
                    text_search::search_prioritized(
                        root,
                        &query,
                        &priorities,
                        global_excludes,
                        cancelled,
                        &mut changed_directories,
                        &emit,
                        text_search::Deadline::at(search_deadline),
                    )
                } else {
                    text_search::search(
                        root,
                        &query,
                        global_excludes,
                        cancelled,
                        &emit,
                        text_search::Deadline::at(search_deadline),
                    )
                };
                // The batcher sends what's left once the search stops sending.
                drop(emit);
                summary
            });
            if let Some((git, _)) = changes {
                git.abort();
            }
            let last = match summary {
                Ok(summary) => Ok(SearchTextResponse {
                    matches: Vec::new(),
                    done: true,
                    truncated: summary.truncated,
                    warning: summary.warning.unwrap_or_default(),
                }),
                Err(message) => Err(Status::invalid_argument(message)),
            };
            let _ = sender.blocking_send(last);
            drop(finished);
        });
        Ok(Response::new(Box::pin(ReceiverStream::new(receiver))))
    }

    async fn resolve_repository_context(
        &self,
        request: Request<proto::ResolveRepositoryContextRequest>,
    ) -> Result<Response<proto::ResolveRepositoryContextResponse>, Status> {
        let workspace = self.workspace(&request)?;
        let deadline = tokio::time::Instant::now() + REPOSITORY_CONTEXT_DEADLINE;
        let resolved = repository_context::resolve(workspace.root.path(), deadline).await;
        Ok(Response::new(repository_context_message(resolved)))
    }

    async fn resolve_working_copy_base(
        &self,
        request: Request<proto::ResolveWorkingCopyBaseRequest>,
    ) -> Result<Response<proto::ResolveWorkingCopyBaseResponse>, Status> {
        let workspace = self.workspace(&request)?;
        let request = request.into_inner().comparison.unwrap_or_default();
        let deadline = tokio::time::Instant::now() + working_copy::LIST_DEADLINE;
        if is_revisions(request.kind) {
            let resolved = async {
                let repository =
                    working_copy::open_repository(workspace.root.path(), deadline).await?;
                revision_comparison::resolve_revisions(
                    &repository,
                    revisions_of(&request),
                    deadline,
                )
                .await
            }
            .await;
            let base = match resolved {
                Ok(comparison) => revision_base_message(&comparison),
                Err(problem) => unavailable_revisions(problem),
            };
            return Ok(Response::new(proto::ResolveWorkingCopyBaseResponse {
                base: Some(base),
            }));
        }
        let kind = comparison_kind(request.kind)?;
        let resolved = async {
            let repository = working_copy::open_repository(workspace.root.path(), deadline).await?;
            working_copy::resolve_base(&repository, kind, deadline).await
        }
        .await;
        let base = match resolved {
            Ok(base) => base_message(&base),
            Err(problem) => unavailable_base(kind, problem),
        };
        Ok(Response::new(proto::ResolveWorkingCopyBaseResponse {
            base: Some(base),
        }))
    }

    async fn list_working_copy_changes(
        &self,
        request: Request<proto::ListWorkingCopyChangesRequest>,
    ) -> Result<Response<proto::ListWorkingCopyChangesResponse>, Status> {
        let workspace = self.workspace(&request)?;
        let request = request.into_inner().comparison.unwrap_or_default();
        let deadline = tokio::time::Instant::now() + working_copy::LIST_DEADLINE;
        if is_revisions(request.kind) {
            let listed = async {
                let repository =
                    working_copy::open_repository(workspace.root.path(), deadline).await?;
                let comparison = revision_comparison::resolve_revisions(
                    &repository,
                    revisions_of(&request),
                    deadline,
                )
                .await?;
                let changes =
                    revision_comparison::list_revision_changes(&repository, &comparison, deadline)
                        .await?;
                Ok::<_, RevisionUnavailable>((comparison, changes))
            }
            .await;
            return Ok(Response::new(match listed {
                Ok((comparison, changes)) => proto::ListWorkingCopyChangesResponse {
                    base: Some(revision_base_message(&comparison)),
                    files: changes.entries.iter().map(changed_file_message).collect(),
                    unrepresentable_names: changes.unrepresentable_names,
                },
                Err(problem) => proto::ListWorkingCopyChangesResponse {
                    base: Some(unavailable_revisions(problem)),
                    files: Vec::new(),
                    unrepresentable_names: 0,
                },
            }));
        }
        let kind = comparison_kind(request.kind)?;
        let listed = async {
            let repository = working_copy::open_repository(workspace.root.path(), deadline).await?;
            let base = working_copy::resolve_base(&repository, kind, deadline).await?;
            working_copy::list_changes(&repository, base, deadline).await
        }
        .await;
        Ok(Response::new(match listed {
            Ok(changes) => proto::ListWorkingCopyChangesResponse {
                base: Some(base_message(&changes.base)),
                files: changes.entries.iter().map(changed_file_message).collect(),
                unrepresentable_names: changes.unrepresentable_names,
            },
            Err(problem) => proto::ListWorkingCopyChangesResponse {
                base: Some(unavailable_base(kind, problem)),
                files: Vec::new(),
                unrepresentable_names: 0,
            },
        }))
    }

    async fn list_repository_refs(
        &self,
        request: Request<proto::ListRepositoryRefsRequest>,
    ) -> Result<Response<proto::ListRepositoryRefsResponse>, Status> {
        let workspace = self.workspace(&request)?;
        let deadline = tokio::time::Instant::now() + working_copy::LIST_DEADLINE;
        let listed = async {
            let repository = working_copy::open_repository(workspace.root.path(), deadline).await?;
            revision_comparison::list_refs(&repository, deadline).await
        }
        .await;
        match listed {
            Ok(refs) => Ok(Response::new(proto::ListRepositoryRefsResponse {
                refs: refs.refs,
                truncated: refs.truncated,
                default_branch_ref: refs.default_branch.unwrap_or_default(),
            })),
            Err(Unavailable::NotARepository) => Err(Status::failed_precondition(
                "The workspace isn't in a Git repository",
            )),
            Err(Unavailable::Timeout) => Err(Status::deadline_exceeded("Git took too long")),
            Err(Unavailable::GitUnavailable) => {
                Err(Status::unavailable("Git isn't installed or couldn't start"))
            }
            Err(problem) => Err(Status::internal(format!("{problem:?}"))),
        }
    }

    async fn list_branch_commits(
        &self,
        request: Request<proto::ListBranchCommitsRequest>,
    ) -> Result<Response<proto::ListBranchCommitsResponse>, Status> {
        let workspace = self.workspace(&request)?;
        let request = request.into_inner();
        let deadline = tokio::time::Instant::now() + working_copy::LIST_DEADLINE;
        let listed = async {
            let repository = working_copy::open_repository(workspace.root.path(), deadline).await?;
            revision_comparison::list_branch_commits(&repository, &request.base_revision, deadline)
                .await
        }
        .await;
        Ok(Response::new(match listed {
            Ok(listed) => proto::ListBranchCommitsResponse {
                base: Some(revision_base_message(&listed.comparison)),
                commits: listed
                    .commits
                    .into_iter()
                    .map(|commit| proto::BranchCommit {
                        object: commit.object,
                        subject: commit.subject,
                        author_name: commit.author_name,
                        author_time_unix: commit.author_time_unix,
                        root: commit.root,
                    })
                    .collect(),
                truncated: listed.truncated,
            },
            Err(problem) => proto::ListBranchCommitsResponse {
                base: Some(unavailable_revisions(problem)),
                commits: Vec::new(),
                truncated: false,
            },
        }))
    }

    type ReadBaseFileStream = ResponseStream<ReadBaseFileResponse>;

    async fn read_base_file(
        &self,
        request: Request<proto::ReadBaseFileRequest>,
    ) -> Result<Response<Self::ReadBaseFileStream>, Status> {
        // One budget for the whole read, discovery included.
        let deadline = tokio::time::Instant::now() + working_copy::READ_DEADLINE;
        let workspace = self.workspace(&request)?;
        let request = request.into_inner();
        let path = normalize_relative(&request.path).map_err(status)?;
        if path.is_empty() {
            return Err(Status::invalid_argument("Name a file in the workspace"));
        }
        let maximum = match request.maximum_bytes {
            0 => host_reader::MAX_READ_BYTES,
            maximum => (maximum.min(host_reader::MAX_READ_BYTES as u64)) as usize,
        };
        let repository = working_copy::open_repository(workspace.root.path(), deadline)
            .await
            .map_err(|problem| read_status(ReadError::Git(problem)))?;
        let read = working_copy::read_base_blob(
            &repository,
            &request.commit,
            &path,
            &request.object,
            maximum,
            deadline,
        )
        .await;
        // A blob over the limit sends only metadata, with `truncated` set.
        let (bytes, truncated) = match read {
            Ok(text) if text.truncated => (Vec::new(), true),
            Ok(text) => (text.bytes, false),
            Err(ReadError::TooLarge) => (Vec::new(), true),
            Err(error) => return Err(read_status(error)),
        };
        let metadata = FileMetadata {
            size: bytes.len() as i64,
            truncated,
            encoding: encoding(std::str::from_utf8(&bytes).is_ok()) as i32,
            content_type: String::new(),
            modify_time: None,
        };
        let chunks = std::iter::once(ReadBaseFileResponse {
            metadata: Some(metadata),
            data: Vec::new(),
        })
        .chain(
            bytes
                .chunks(READ_CHUNK_BYTES)
                .map(|data| ReadBaseFileResponse {
                    metadata: None,
                    data: data.to_vec(),
                }),
        )
        .map(Ok)
        .collect::<Vec<_>>();
        Ok(Response::new(Box::pin(tokio_stream::iter(chunks))))
    }

    async fn read_link_text(
        &self,
        request: Request<proto::ReadLinkTextRequest>,
    ) -> Result<Response<proto::ReadLinkTextResponse>, Status> {
        let workspace = self.workspace(&request)?;
        let request = request.into_inner();
        let target = tokio::task::block_in_place(|| {
            working_copy::read_link_text(&workspace.root, &request.path)
        })
        .map_err(|error| match error {
            ReadLinkError::Path(error) => status(error),
            ReadLinkError::NotASymlink => Status::failed_precondition("Not a symlink"),
            ReadLinkError::Io(error) => io_status(&error),
        })?;
        Ok(Response::new(proto::ReadLinkTextResponse { target }))
    }
}

/// Long enough for two consistent reads of a large repository's configuration.
const REPOSITORY_CONTEXT_DEADLINE: std::time::Duration = std::time::Duration::from_secs(10);

fn repository_context_message(
    resolved: Result<RepositoryContext, ContextProblem>,
) -> proto::ResolveRepositoryContextResponse {
    let context = match resolved {
        Ok(context) => context,
        Err(problem) => {
            return proto::ResolveRepositoryContextResponse {
                problem: match problem {
                    ContextProblem::NotARepository => RepositoryContextProblem::NotARepository,
                    ContextProblem::UnsupportedConfig => {
                        RepositoryContextProblem::UnsupportedConfig
                    }
                    ContextProblem::RepositoryChanging => {
                        RepositoryContextProblem::RepositoryChanging
                    }
                    ContextProblem::Timeout => RepositoryContextProblem::Timeout,
                    ContextProblem::GitUnavailable => RepositoryContextProblem::GitUnavailable,
                    ContextProblem::GitFailed => RepositoryContextProblem::GitFailed,
                } as i32,
                ..proto::ResolveRepositoryContextResponse::default()
            };
        }
    };
    proto::ResolveRepositoryContextResponse {
        problem: RepositoryContextProblem::Unspecified as i32,
        branch: context.branch.unwrap_or_default(),
        head_sha: context.head_sha.unwrap_or_default(),
        push: context.push.map(|push| proto::PushTarget {
            remote_name: push.remote_name,
            branch: push.branch,
            host: push.identity.host,
            owner: push.identity.owner,
            repo: push.identity.repo,
            tracking_sha: push.tracking_sha.unwrap_or_default(),
        }),
        push_unresolved: match context.push_unresolved {
            None => proto::PushUnresolved::Unspecified,
            Some(PushUnresolved::Refspec) => proto::PushUnresolved::Refspec,
            Some(PushUnresolved::PushUrl) => proto::PushUnresolved::PushUrl,
            Some(PushUnresolved::PushDefault) => proto::PushUnresolved::PushDefault,
        } as i32,
        upstream_ref: context.upstream_ref.unwrap_or_default(),
        remotes: context
            .remotes
            .into_iter()
            .map(|remote| proto::RepositoryRemote {
                name: remote.name,
                host: remote.identity.host,
                owner: remote.identity.owner,
                repo: remote.identity.repo,
            })
            .collect(),
        unsupported_remote_count: context.unsupported_remote_count,
        truncated_remotes: context.truncated_remotes,
    }
}

fn comparison_kind(kind: i32) -> Result<ComparisonKind, Status> {
    match proto::ComparisonKind::try_from(kind) {
        Ok(proto::ComparisonKind::LastCommit) => Ok(ComparisonKind::LastCommit),
        Ok(proto::ComparisonKind::BranchPoint) => Ok(ComparisonKind::BranchPoint),
        _ => Err(Status::invalid_argument("Choose what to compare with")),
    }
}

fn kind_message(kind: ComparisonKind) -> i32 {
    let kind = match kind {
        ComparisonKind::LastCommit => proto::ComparisonKind::LastCommit,
        ComparisonKind::BranchPoint => proto::ComparisonKind::BranchPoint,
        ComparisonKind::Revisions => proto::ComparisonKind::Revisions,
    };
    kind as i32
}

fn is_revisions(kind: i32) -> bool {
    proto::ComparisonKind::try_from(kind) == Ok(proto::ComparisonKind::Revisions)
}

fn revisions_of(request: &proto::Comparison) -> RevisionRequest<'_> {
    RevisionRequest {
        base: &request.base_revision,
        head: &request.head_revision,
    }
}

fn revision_base_message(comparison: &RevisionComparison) -> proto::WorkingCopyBase {
    proto::WorkingCopyBase {
        merge_base: comparison.base.base.clone(),
        base_display: comparison.base_display.clone(),
        head_display: comparison.head_display.clone(),
        head_is_working_copy: comparison.head_is_working_copy,
        ..base_message(&comparison.base)
    }
}

fn field_message(field: RevisionField) -> i32 {
    let field = match field {
        RevisionField::Base => proto::ComparisonField::Base,
        RevisionField::Head => proto::ComparisonField::Head,
        RevisionField::Both => proto::ComparisonField::Both,
    };
    field as i32
}

fn unavailable_revisions(problem: RevisionUnavailable) -> proto::WorkingCopyBase {
    use proto::ComparisonProblem as Problem;
    let (code, field) = match problem {
        RevisionUnavailable::Comparison(problem) => {
            return unavailable_base(ComparisonKind::Revisions, problem);
        }
        RevisionUnavailable::Invalid(field) => (Problem::InvalidRevision, field),
        RevisionUnavailable::NotFound(RevisionField::Base) => {
            (Problem::BaseNotFound, RevisionField::Base)
        }
        RevisionUnavailable::NotFound(RevisionField::Head) => {
            (Problem::HeadNotFound, RevisionField::Head)
        }
        RevisionUnavailable::NotFound(RevisionField::Both) => {
            (Problem::BaseNotFound, RevisionField::Both)
        }
        RevisionUnavailable::NoCommonHistory => (Problem::NoCommonHistory, RevisionField::Both),
    };
    proto::WorkingCopyBase {
        kind: kind_message(ComparisonKind::Revisions),
        unavailable: Some(proto::ComparisonUnavailable {
            problem: code as i32,
            field: field_message(field),
            ..proto::ComparisonUnavailable::default()
        }),
        ..proto::WorkingCopyBase::default()
    }
}

fn base_message(base: &working_copy::ComparisonBase) -> proto::WorkingCopyBase {
    proto::WorkingCopyBase {
        kind: kind_message(base.kind),
        head: base.head.clone(),
        base: base.base.clone(),
        reference: base.reference.clone().unwrap_or_default(),
        reference_commit: base.reference_commit.clone().unwrap_or_default(),
        unavailable: None,
        ..proto::WorkingCopyBase::default()
    }
}

fn unavailable_base(kind: ComparisonKind, problem: Unavailable) -> proto::WorkingCopyBase {
    use proto::ComparisonProblem as Problem;
    let mut unavailable = proto::ComparisonUnavailable::default();
    let code = match problem {
        Unavailable::NotARepository => Problem::NotARepository,
        Unavailable::NoCommits => Problem::NoCommits,
        Unavailable::NoDefaultBranch => Problem::NoDefaultBranch,
        Unavailable::NoBranchPoint { reference, shallow } => {
            unavailable.reference = reference;
            unavailable.shallow = shallow;
            Problem::NoBranchPoint
        }
        Unavailable::MissingObject => Problem::MissingObject,
        Unavailable::PartialCloneNeedsNewerGit => Problem::PartialCloneNeedsNewerGit,
        Unavailable::Timeout => Problem::Timeout,
        Unavailable::TooManyFiles => Problem::TooManyFiles,
        Unavailable::ListingTooLarge => Problem::ListingTooLarge,
        Unavailable::MultipleRepositories => Problem::MultipleRepositories,
        Unavailable::GitUnavailable => Problem::GitUnavailable,
        Unavailable::GitFailed(detail) => {
            unavailable.detail = detail;
            Problem::GitFailed
        }
    };
    unavailable.problem = code as i32;
    proto::WorkingCopyBase {
        kind: kind_message(kind),
        unavailable: Some(unavailable),
        ..proto::WorkingCopyBase::default()
    }
}

fn entry_kind_message(kind: Option<working_copy::EntryKind>) -> i32 {
    use working_copy::EntryKind;
    let kind = match kind {
        None => proto::GitEntryKind::Unspecified,
        Some(EntryKind::File) => proto::GitEntryKind::File,
        Some(EntryKind::Symlink) => proto::GitEntryKind::Symlink,
        Some(EntryKind::Submodule) => proto::GitEntryKind::Submodule,
    };
    kind as i32
}

fn changed_file_message(entry: &working_copy::ChangedEntry) -> proto::ChangedFile {
    use working_copy::{ChangeStatus, EntryProblem};
    proto::ChangedFile {
        path: entry.path.clone(),
        status: match entry.status {
            ChangeStatus::Added => proto::ChangeStatus::Added,
            ChangeStatus::Modified => proto::ChangeStatus::Modified,
            ChangeStatus::Deleted => proto::ChangeStatus::Deleted,
            ChangeStatus::TypeChanged => proto::ChangeStatus::TypeChanged,
            ChangeStatus::Conflicted => proto::ChangeStatus::Conflicted,
        } as i32,
        base_kind: entry_kind_message(entry.base_kind),
        working_kind: entry_kind_message(entry.working_kind),
        base_object: entry.base_object.clone().unwrap_or_default(),
        base_size: entry.base_size,
        lines_added: entry.lines_added,
        lines_removed: entry.lines_removed,
        binary: entry.binary,
        problem: match entry.problem {
            None => proto::ChangedFileProblem::Unspecified,
            Some(EntryProblem::Filtered) => proto::ChangedFileProblem::Filtered,
            Some(EntryProblem::MissingObject) => proto::ChangedFileProblem::MissingObject,
        } as i32,
        working: entry.working.map(|working| proto::WorkingFileMetadata {
            size: working.size,
            modified_unix_nanos: working.modified_unix_nanos,
            changed_unix_nanos: working.changed_unix_nanos,
            inode: working.inode,
            device: working.device,
        }),
        committed: entry.committed.as_ref().map(|blob| proto::CommittedFile {
            object: blob.object.clone(),
            size: blob.size,
        }),
    }
}

fn read_status(error: ReadError) -> Status {
    match error {
        ReadError::Invalid(message) => Status::invalid_argument(message),
        ReadError::NotInComparison => {
            Status::failed_precondition("The file isn't part of this comparison")
        }
        ReadError::TooLarge => Status::resource_exhausted("The file is too large"),
        ReadError::NotASymlink => Status::failed_precondition("Not a symlink"),
        ReadError::Git(Unavailable::MissingObject) => {
            Status::not_found("This file's base version isn't in this clone")
        }
        ReadError::Git(Unavailable::Timeout) => Status::deadline_exceeded("Git took too long"),
        ReadError::Git(Unavailable::NotARepository) => {
            Status::failed_precondition("The workspace isn't in a Git repository")
        }
        ReadError::Git(Unavailable::GitUnavailable) => {
            Status::unavailable("Git isn't installed or couldn't start")
        }
        ReadError::Git(problem) => Status::internal(format!("{problem:?}")),
    }
}

#[cfg(test)]
#[path = "server_test.rs"]
mod tests;

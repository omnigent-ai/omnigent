//! The working copy's changes against a base commit: the last commit (HEAD)
//! or the branch point with the default branch. Everything goes through
//! [`GitRepository`], so nothing here writes to the repository or fetches.

use std::{collections::HashMap, path::Path, time::Duration};

use tokio::time::Instant;

use crate::git_runner::{
    CallLimits, GitCommand, GitError, GitRepository, failure_of, is_object_name,
};

/// One listing's time budget. The diff and the untracked listing run side by
/// side; for a 1.6M-file monorepo the untracked listing takes about 6 s.
pub const LIST_DEADLINE: Duration = Duration::from_secs(20);
/// Raw Git output a listing may read, per command.
pub const LIST_OUTPUT_BYTES: usize = 8 * 1024 * 1024;
/// Changed files a listing may return.
pub const MAXIMUM_ENTRIES: usize = 10_000;
/// One file's base text or link text read.
pub const READ_DEADLINE: Duration = Duration::from_secs(5);

const SMALL_OUTPUT_BYTES: usize = 64 * 1024;
/// Paths per `git diff --numstat` call, by total length.
const ARGUMENT_BATCH_BYTES: usize = 64 * 1024;
const MODE_SYMLINK: u32 = 0o120000;
const MODE_SUBMODULE: u32 = 0o160000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ComparisonKind {
    LastCommit,
    BranchPoint,
    /// The merge base of two revisions a person named; see `revision_comparison`.
    Revisions,
}

/// Why a comparison couldn't be made.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Unavailable {
    NotARepository,
    NoCommits,
    /// No remote default branch or upstream to compare with.
    NoDefaultBranch,
    /// The default branch shares no history with HEAD; `shallow` says why it
    /// might.
    NoBranchPoint {
        reference: String,
        shallow: bool,
    },
    MissingObject,
    PartialCloneNeedsNewerGit,
    Timeout,
    TooManyFiles,
    ListingTooLarge,
    /// The workspace holds a repository nested inside its own, whose files a
    /// single comparison can't describe.
    MultipleRepositories,
    GitUnavailable,
    GitFailed(String),
}

impl From<GitError> for Unavailable {
    fn from(error: GitError) -> Self {
        match error {
            GitError::Unavailable => Self::GitUnavailable,
            GitError::Timeout => Self::Timeout,
            GitError::OutputTooLarge(_) => Self::ListingTooLarge,
            GitError::MissingObject => Self::MissingObject,
            GitError::PartialCloneNeedsNewerGit => Self::PartialCloneNeedsNewerGit,
            GitError::Interrupted { .. } => Self::GitFailed(error.to_string()),
            GitError::Failed(message) => Self::GitFailed(message),
        }
    }
}

/// What a comparison compares with, named by full object names.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ComparisonBase {
    pub kind: ComparisonKind,
    pub head: String,
    /// The commit the working copy is compared with.
    pub base: String,
    /// For the branch point: the ref compared with, such as `upstream/master`.
    pub reference: Option<String>,
    pub reference_commit: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChangeStatus {
    Added,
    Modified,
    Deleted,
    TypeChanged,
    Conflicted,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntryKind {
    File,
    Symlink,
    Submodule,
}

/// Why one file's diff can't be shown, while the rest of the comparison can.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntryProblem {
    /// A Git filter (such as LFS) changes this file's stored form.
    Filtered,
    /// Its base version isn't in this clone.
    MissingObject,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChangedEntry {
    /// Relative to the workspace, `/`-separated.
    pub path: String,
    pub status: ChangeStatus,
    pub base_kind: Option<EntryKind>,
    pub working_kind: Option<EntryKind>,
    /// The full base object name; absent for added files.
    pub base_object: Option<String>,
    pub base_size: Option<u64>,
    pub lines_added: Option<u64>,
    pub lines_removed: Option<u64>,
    pub binary: bool,
    pub problem: Option<EntryProblem>,
    /// The working file when listed; absent when there is none.
    pub working: Option<WorkingMetadata>,
    /// For a committed head: the file's blob there. Absent for the working copy.
    pub committed: Option<CommittedBlob>,
}

/// A file's blob at a committed head, read like a base file at that commit.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CommittedBlob {
    pub object: String,
    pub size: Option<u64>,
}

/// A working file's own metadata (a symlink's, not its target's), so a later
/// listing can tell the file changed even when its status and counts didn't.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct WorkingMetadata {
    pub size: u64,
    pub modified_unix_nanos: i64,
    pub changed_unix_nanos: i64,
    pub inode: u64,
    pub device: u64,
}

impl WorkingMetadata {
    pub fn of(path: &Path) -> Option<Self> {
        use std::os::unix::fs::MetadataExt;
        let metadata = std::fs::symlink_metadata(path).ok()?;
        Some(Self {
            size: metadata.size(),
            modified_unix_nanos: metadata.mtime() * 1_000_000_000 + metadata.mtime_nsec(),
            changed_unix_nanos: metadata.ctime() * 1_000_000_000 + metadata.ctime_nsec(),
            inode: metadata.ino(),
            device: metadata.dev(),
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkingCopyChanges {
    pub base: ComparisonBase,
    pub entries: Vec<ChangedEntry>,
    /// Changed files left out because their names aren't valid UTF-8.
    pub unrepresentable_names: u32,
}

pub(crate) fn limits(deadline: Instant) -> CallLimits {
    CallLimits {
        deadline,
        stdout_bytes: SMALL_OUTPUT_BYTES,
    }
}

pub(crate) fn first_line(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .lines()
        .next()
        .unwrap_or_default()
        .trim()
        .to_string()
}

/// The output of a command whose exit status 1 means "not there" (a missing
/// ref, a detached HEAD, no merge-base): `None` then. Any other failure,
/// including an outside signal, stays an error.
pub(crate) async fn run_allowing_absent(
    repository: &GitRepository,
    command: GitCommand,
    deadline: Instant,
) -> Result<Option<Vec<u8>>, GitError> {
    let output = repository
        .run_unchecked(&command, None, limits(deadline))
        .await?;
    if output.success {
        Ok(Some(output.stdout))
    } else if output.exit_code == Some(1) && output.stderr.trim().is_empty() {
        Ok(None)
    } else {
        Err(failure_of(&output, &command.name()))
    }
}

pub(crate) async fn verify_commit(
    repository: &GitRepository,
    revision: &str,
    deadline: Instant,
) -> Result<Option<String>, GitError> {
    let command = GitCommand::VerifyCommit {
        revision: revision.to_string(),
    };
    Ok(run_allowing_absent(repository, command, deadline)
        .await?
        .map(|stdout| first_line(&stdout))
        .filter(|commit| is_object_name(commit)))
}

/// The branch the working copy should be compared with, as a full ref and
/// its short name. The remote is the current branch's upstream remote, else
/// its push remote, else `origin`. That remote's default branch wins, then an
/// upstream that names a different branch, then `origin/HEAD`.
pub async fn default_branch(
    repository: &GitRepository,
    deadline: Instant,
) -> Result<Option<(String, String)>, GitError> {
    let branch_ref = run_allowing_absent(repository, GitCommand::SymbolicRefHead, deadline)
        .await?
        .map(|stdout| first_line(&stdout))
        .filter(|name| !name.is_empty());
    let mut remote = None;
    let mut upstream_candidate = None;
    if let Some(branch_ref) = &branch_ref {
        let tracking = repository
            .run(
                &GitCommand::BranchTracking {
                    branch_ref: branch_ref.clone(),
                },
                None,
                limits(deadline),
            )
            .await?;
        let text = String::from_utf8_lossy(&tracking);
        let fields: Vec<&str> = text.trim_end_matches('\n').split('\0').collect();
        if let [_, upstream, upstream_remote, _push, push_remote] = fields[..] {
            let usable = |name: &&str| !name.is_empty() && *name != ".";
            remote = [upstream_remote, push_remote]
                .into_iter()
                .find(usable)
                .map(str::to_string);
            let branch_name = branch_ref.strip_prefix("refs/heads/").unwrap_or(branch_ref);
            let upstream_branch = upstream
                .strip_prefix(&format!("refs/remotes/{upstream_remote}/"))
                .unwrap_or_default();
            if !upstream_branch.is_empty() && upstream_branch != branch_name {
                upstream_candidate = Some(upstream.to_string());
            }
        }
    }
    let remote = remote.unwrap_or_else(|| "origin".to_string());
    if let Some(found) = remote_default_ref(repository, &remote, deadline).await? {
        return Ok(Some(found));
    }
    if let Some(upstream) = upstream_candidate
        && verify_commit(repository, &upstream, deadline)
            .await?
            .is_some()
    {
        let short = upstream
            .strip_prefix("refs/remotes/")
            .unwrap_or(&upstream)
            .to_string();
        return Ok(Some((upstream, short)));
    }
    if remote == "origin" {
        return Ok(None);
    }
    remote_default_ref(repository, "origin", deadline).await
}

async fn remote_default_ref(
    repository: &GitRepository,
    remote: &str,
    deadline: Instant,
) -> Result<Option<(String, String)>, GitError> {
    let command = GitCommand::RemoteDefaultRef {
        remote: remote.to_string(),
    };
    let Some(stdout) = run_allowing_absent(repository, command, deadline).await? else {
        return Ok(None);
    };
    let target = first_line(&stdout);
    if !target.starts_with("refs/remotes/")
        || verify_commit(repository, &target, deadline)
            .await?
            .is_none()
    {
        return Ok(None);
    }
    let short = target
        .strip_prefix("refs/remotes/")
        .unwrap_or(&target)
        .to_string();
    Ok(Some((target, short)))
}

/// Resolves what a comparison compares with, cheaply enough to poll.
pub async fn resolve_base(
    repository: &GitRepository,
    kind: ComparisonKind,
    deadline: Instant,
) -> Result<ComparisonBase, Unavailable> {
    let Some(head) = verify_commit(repository, "HEAD", deadline).await? else {
        return Err(Unavailable::NoCommits);
    };
    if kind == ComparisonKind::LastCommit {
        return Ok(ComparisonBase {
            kind,
            base: head.clone(),
            head,
            reference: None,
            reference_commit: None,
        });
    }
    let Some((full_ref, short_ref)) = default_branch(repository, deadline).await? else {
        return Err(Unavailable::NoDefaultBranch);
    };
    let Some(reference_commit) = verify_commit(repository, &full_ref, deadline).await? else {
        return Err(Unavailable::NoDefaultBranch);
    };
    let merge_base = GitCommand::MergeBase {
        left: head.clone(),
        right: reference_commit.clone(),
    };
    let Some(stdout) = run_allowing_absent(repository, merge_base, deadline).await? else {
        let shallow = repository
            .run(&GitCommand::IsShallow, None, limits(deadline))
            .await
            .map(|out| first_line(&out) == "true")?;
        return Err(Unavailable::NoBranchPoint {
            reference: short_ref,
            shallow,
        });
    };
    let base = first_line(&stdout);
    if !is_object_name(&base) {
        return Err(Unavailable::GitFailed(format!(
            "git merge-base printed an unexpected result: {base}"
        )));
    }
    Ok(ComparisonBase {
        kind,
        head,
        base,
        reference: Some(short_ref),
        reference_commit: Some(reference_commit),
    })
}

fn kind_of_mode(mode: u32) -> Option<EntryKind> {
    match mode {
        0 => None,
        MODE_SYMLINK => Some(EntryKind::Symlink),
        MODE_SUBMODULE => Some(EntryKind::Submodule),
        _ => Some(EntryKind::File),
    }
}

/// Parses `diff --raw -z` records: `:old new oldobj newobj status\0path\0`.
/// With `committed_head`, each record's new object is the head's blob.
pub(crate) fn parse_raw_diff(
    output: &[u8],
    unrepresentable: &mut u32,
    committed_head: bool,
) -> Result<Vec<ChangedEntry>, Unavailable> {
    let mut fields = output.split(|&byte| byte == 0);
    let mut entries = Vec::new();
    while let Some(header) = fields.next() {
        if header.is_empty() {
            continue;
        }
        let Some(path) = fields.next() else {
            break;
        };
        let header = String::from_utf8_lossy(header);
        let parts: Vec<&str> = header.trim_start_matches(':').split(' ').collect();
        let [old_mode, new_mode, old_object, new_object, status] = parts[..] else {
            continue;
        };
        let Ok(path) = std::str::from_utf8(path) else {
            *unrepresentable += 1;
            continue;
        };
        let old_mode = u32::from_str_radix(old_mode, 8).unwrap_or(0);
        let new_mode = u32::from_str_radix(new_mode, 8).unwrap_or(0);
        let status = match status.chars().next() {
            Some('A') => ChangeStatus::Added,
            Some('D') => ChangeStatus::Deleted,
            Some('T') => ChangeStatus::TypeChanged,
            Some('U') => ChangeStatus::Conflicted,
            _ => ChangeStatus::Modified,
        };
        let base_object = (is_object_name(old_object)
            && old_object.bytes().any(|byte| byte != b'0'))
        .then(|| old_object.to_string());
        let committed = (committed_head
            && is_object_name(new_object)
            && new_object.bytes().any(|byte| byte != b'0'))
        .then(|| CommittedBlob {
            object: new_object.to_string(),
            size: None,
        });
        entries.push(ChangedEntry {
            path: path.to_string(),
            status,
            base_kind: kind_of_mode(old_mode),
            working_kind: kind_of_mode(new_mode),
            base_object,
            base_size: None,
            lines_added: None,
            lines_removed: None,
            binary: false,
            problem: None,
            working: None,
            committed,
        });
        if entries.len() > MAXIMUM_ENTRIES {
            return Err(Unavailable::TooManyFiles);
        }
    }
    // An unmerged path appears once per stage; keep one entry.
    entries.dedup_by(|later, earlier| later.path == earlier.path);
    Ok(entries)
}

fn nul_joined<'a>(paths: impl Iterator<Item = &'a str>) -> Vec<u8> {
    let mut bytes = Vec::new();
    for path in paths {
        bytes.extend_from_slice(path.as_bytes());
        bytes.push(0);
    }
    bytes
}

/// Lists the working copy's changes against `base`, beneath the workspace:
/// tracked changes from `git diff`, plus untracked files exactly as Git's
/// ignore rules see them. Both listings share `deadline` and the output cap.
pub async fn list_changes(
    repository: &GitRepository,
    base: ComparisonBase,
    deadline: Instant,
) -> Result<WorkingCopyChanges, Unavailable> {
    let listing = CallLimits {
        deadline,
        stdout_bytes: LIST_OUTPUT_BYTES,
    };
    let diff = GitCommand::DiffRaw {
        base: base.base.clone(),
    };
    let (changed, untracked, unmerged) = tokio::join!(
        repository.run(&diff, None, listing),
        repository.run(&GitCommand::ListUntracked, None, listing),
        repository.run(&GitCommand::ListUnmerged, None, listing),
    );
    let mut unrepresentable = 0;
    let mut entries = parse_raw_diff(&changed?, &mut unrepresentable, false)?;
    // `<mode> <object> <stage>\t<path>\0` per stage of each unmerged path.
    let conflicted: std::collections::HashSet<String> = unmerged?
        .split(|&byte| byte == 0)
        .filter_map(|record| record.splitn(2, |&byte| byte == b'\t').nth(1))
        .filter_map(|path| std::str::from_utf8(path).ok().map(str::to_string))
        .collect();
    for entry in entries.iter_mut() {
        if conflicted.contains(&entry.path) {
            entry.status = ChangeStatus::Conflicted;
        }
    }
    for path in untracked?.split(|&byte| byte == 0) {
        if path.is_empty() {
            continue;
        }
        if path.ends_with(b"/") {
            return Err(Unavailable::MultipleRepositories);
        }
        let Ok(path) = std::str::from_utf8(path) else {
            unrepresentable += 1;
            continue;
        };
        let symlink = std::fs::symlink_metadata(repository.working_directory.join(path))
            .is_ok_and(|metadata| metadata.file_type().is_symlink());
        entries.push(ChangedEntry {
            path: path.to_string(),
            status: ChangeStatus::Added,
            base_kind: None,
            working_kind: Some(if symlink {
                EntryKind::Symlink
            } else {
                EntryKind::File
            }),
            base_object: None,
            base_size: None,
            lines_added: None,
            lines_removed: None,
            binary: false,
            problem: None,
            working: None,
            committed: None,
        });
        if entries.len() > MAXIMUM_ENTRIES {
            return Err(Unavailable::TooManyFiles);
        }
    }

    mark_base_objects(repository, &mut entries, deadline).await?;
    mark_filtered(repository, &mut entries, deadline).await?;
    let compared = base.base.clone();
    count_lines(
        repository,
        |paths| GitCommand::DiffNumstat {
            base: compared.clone(),
            paths,
        },
        &mut entries,
        deadline,
    )
    .await?;
    for entry in entries.iter_mut() {
        if entry.working_kind.is_some() {
            entry.working = WorkingMetadata::of(&repository.working_directory.join(&entry.path));
        }
    }
    entries.sort_by(|left, right| left.path.cmp(&right.path));
    Ok(WorkingCopyChanges {
        base,
        entries,
        unrepresentable_names: unrepresentable,
    })
}

/// Base sizes, and which base objects this clone doesn't have.
pub(crate) async fn mark_base_objects(
    repository: &GitRepository,
    entries: &mut [ChangedEntry],
    deadline: Instant,
) -> Result<(), Unavailable> {
    let objects: Vec<String> = entries
        .iter()
        .filter(|entry| entry.base_kind != Some(EntryKind::Submodule))
        .filter_map(|entry| entry.base_object.clone())
        .collect();
    if objects.is_empty() {
        return Ok(());
    }
    let mut input = objects.join("\n").into_bytes();
    input.push(b'\n');
    let output = repository
        .run(
            &GitCommand::ObjectSizes,
            Some(input),
            CallLimits {
                deadline,
                stdout_bytes: LIST_OUTPUT_BYTES,
            },
        )
        .await?;
    let mut sizes: HashMap<String, Option<u64>> = HashMap::new();
    for line in String::from_utf8_lossy(&output).lines() {
        let parts: Vec<&str> = line.split(' ').collect();
        match parts[..] {
            [object, "missing"] => {
                sizes.insert(object.to_string(), None);
            }
            [object, _kind, size] => {
                sizes.insert(object.to_string(), size.parse().ok());
            }
            _ => {}
        }
    }
    for entry in entries.iter_mut() {
        if let Some(object) = &entry.base_object {
            match sizes.get(object) {
                Some(Some(size)) => entry.base_size = Some(*size),
                Some(None) => entry.problem = Some(EntryProblem::MissingObject),
                None => {}
            }
        }
    }
    Ok(())
}

pub(crate) async fn mark_filtered(
    repository: &GitRepository,
    entries: &mut [ChangedEntry],
    deadline: Instant,
) -> Result<(), Unavailable> {
    if entries.is_empty() {
        return Ok(());
    }
    let input = nul_joined(entries.iter().map(|entry| entry.path.as_str()));
    let output = repository
        .run(
            &GitCommand::FilterAttributes,
            Some(input),
            CallLimits {
                deadline,
                stdout_bytes: LIST_OUTPUT_BYTES,
            },
        )
        .await?;
    let mut filtered = std::collections::HashSet::new();
    let fields: Vec<&[u8]> = output.split(|&byte| byte == 0).collect();
    for record in fields.chunks(3) {
        if let [path, _attribute, value] = record
            && !matches!(*value, b"unspecified" | b"unset" | b"")
        {
            filtered.insert(String::from_utf8_lossy(path).into_owned());
        }
    }
    for entry in entries.iter_mut() {
        if filtered.contains(&entry.path) {
            entry.problem = Some(EntryProblem::Filtered);
        }
    }
    Ok(())
}

/// Line counts for tracked files whose base is present and not filtered, from
/// the `numstat` command for each batch of paths.
pub(crate) async fn count_lines(
    repository: &GitRepository,
    numstat: impl Fn(Vec<String>) -> GitCommand,
    entries: &mut [ChangedEntry],
    deadline: Instant,
) -> Result<(), Unavailable> {
    let countable: Vec<&str> = entries
        .iter()
        .filter(|entry| entry.base_object.is_some() && entry.problem.is_none())
        .filter(|entry| entry.base_kind != Some(EntryKind::Submodule))
        .map(|entry| entry.path.as_str())
        .collect();
    if countable.is_empty() {
        return Ok(());
    }
    let mut output = Vec::new();
    for batch in argument_batches(&countable) {
        let counted = repository
            .run(
                &numstat(batch),
                None,
                CallLimits {
                    deadline,
                    stdout_bytes: LIST_OUTPUT_BYTES.saturating_sub(output.len()),
                },
            )
            .await?;
        output.extend(counted);
    }
    let mut counts: HashMap<String, Option<(u64, u64)>> = HashMap::new();
    for record in output.split(|&byte| byte == 0) {
        let record = String::from_utf8_lossy(record);
        let mut parts = record.splitn(3, '\t');
        let (Some(added), Some(removed), Some(path)) = (parts.next(), parts.next(), parts.next())
        else {
            continue;
        };
        counts.insert(
            path.to_string(),
            added.parse().ok().zip(removed.parse().ok()),
        );
    }
    for entry in entries.iter_mut() {
        match counts.get(&entry.path) {
            Some(Some((added, removed))) => {
                entry.lines_added = Some(*added);
                entry.lines_removed = Some(*removed);
            }
            Some(None) => entry.binary = true,
            None => {}
        }
    }
    Ok(())
}

/// Splits paths into argument lists of at most [`ARGUMENT_BATCH_BYTES`], so a
/// long list never exceeds the system's argument limit.
fn argument_batches(paths: &[&str]) -> Vec<Vec<String>> {
    let mut batches = Vec::new();
    let mut batch: Vec<String> = Vec::new();
    let mut size = 0;
    for path in paths {
        if !batch.is_empty() && size + path.len() + 1 > ARGUMENT_BATCH_BYTES {
            batches.push(std::mem::take(&mut batch));
            size = 0;
        }
        size += path.len() + 1;
        batch.push(path.to_string());
    }
    if !batch.is_empty() {
        batches.push(batch);
    }
    batches
}

/// The outcome of a bounded read.
#[derive(Debug, PartialEq, Eq)]
pub struct BoundedText {
    pub bytes: Vec<u8>,
    /// The content is larger than the allowance; `bytes` holds only a prefix.
    pub truncated: bool,
}

/// One file's content at `commit`, never more than `maximum_bytes`. The path
/// is relative to the workspace, so a read can't leave its subtree; the
/// object must match what the listing reported. `deadline` covers the whole
/// request, including opening the repository.
pub async fn read_base_blob(
    repository: &GitRepository,
    commit: &str,
    path: &str,
    expected_object: &str,
    maximum_bytes: usize,
    deadline: Instant,
) -> Result<BoundedText, ReadError> {
    if !is_object_name(commit) || !is_object_name(expected_object) {
        return Err(ReadError::Invalid("Use full object names"));
    }
    let top_path = format!("{}{}", repository.prefix, path);
    let listing = repository
        .run(
            &GitCommand::TreeEntry {
                commit: commit.to_string(),
                top_path,
            },
            None,
            limits(deadline),
        )
        .await?;
    // `<mode> <type> <object>\t<path>\0`
    let record = listing.split(|&byte| byte == 0).next().unwrap_or_default();
    let header = String::from_utf8_lossy(record);
    let mut parts = header.split(['\t', ' ']);
    let (_mode, kind, object) = (parts.next(), parts.next(), parts.next());
    if kind != Some("blob") || object != Some(expected_object) {
        return Err(ReadError::NotInComparison);
    }
    let mut query = expected_object.as_bytes().to_vec();
    query.push(b'\n');
    let present = repository
        .run(&GitCommand::ObjectSizes, Some(query), limits(deadline))
        .await?;
    if String::from_utf8_lossy(&present)
        .trim_end()
        .ends_with(" missing")
    {
        return Err(ReadError::Git(Unavailable::MissingObject));
    }
    let output = repository
        .run(
            &GitCommand::BlobContents {
                object: expected_object.to_string(),
            },
            None,
            CallLimits {
                deadline,
                stdout_bytes: maximum_bytes.saturating_add(1),
            },
        )
        .await;
    match output {
        Ok(mut bytes) => {
            let truncated = bytes.len() > maximum_bytes;
            bytes.truncate(maximum_bytes);
            Ok(BoundedText { bytes, truncated })
        }
        Err(GitError::OutputTooLarge(_)) => Err(ReadError::TooLarge),
        Err(error) => Err(error.into()),
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum ReadError {
    Invalid(&'static str),
    NotInComparison,
    TooLarge,
    NotASymlink,
    Git(Unavailable),
}

impl From<GitError> for ReadError {
    fn from(error: GitError) -> Self {
        Self::Git(error.into())
    }
}

/// A symlink's own text, read without following it. The parent directory
/// must resolve inside `root`; the link may point anywhere, even nowhere.
pub fn read_link_text(
    root: &crate::workspace_path::WorkspaceRoot,
    path: &str,
) -> Result<Vec<u8>, ReadLinkError> {
    let relative = crate::workspace_path::normalize_relative(path).map_err(ReadLinkError::Path)?;
    let (parent, name) = match relative.rsplit_once('/') {
        Some((parent, name)) => (parent, name),
        None if relative.is_empty() => return Err(ReadLinkError::NotASymlink),
        None => ("", relative.as_str()),
    };
    let (_, parent) = root.resolve(parent).map_err(ReadLinkError::Path)?;
    let link = parent.join(name);
    let metadata = std::fs::symlink_metadata(&link).map_err(ReadLinkError::Io)?;
    if !metadata.file_type().is_symlink() {
        return Err(ReadLinkError::NotASymlink);
    }
    let target = std::fs::read_link(&link).map_err(ReadLinkError::Io)?;
    Ok(std::os::unix::ffi::OsStrExt::as_bytes(target.as_os_str()).to_vec())
}

#[derive(Debug)]
pub enum ReadLinkError {
    Path(crate::workspace_path::PathError),
    NotASymlink,
    Io(std::io::Error),
}

/// Opens the repository for a workspace directory, under one deadline.
pub async fn open_repository(
    workspace_directory: &Path,
    deadline: Instant,
) -> Result<GitRepository, Unavailable> {
    GitRepository::discover(workspace_directory, deadline)
        .await?
        .ok_or(Unavailable::NotARepository)
}

#[cfg(test)]
#[path = "working_copy_test.rs"]
mod tests;

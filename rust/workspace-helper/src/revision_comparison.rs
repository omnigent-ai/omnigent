//! Comparing revisions a person typed: the merge base of a base and a head,
//! compared with that head, which is either the working copy or a commit.
//! Every revision is checked before Git sees it, and Git only reads: names are
//! resolved with `rev-parse --verify --end-of-options`, and listings and reads
//! use the same commands, limits and safeguards as the working-copy comparison.

use std::collections::HashMap;

use tokio::time::Instant;

use crate::git_runner::{CallLimits, GitCommand, GitError, GitRepository, is_object_name};
use crate::working_copy::{
    self, ChangedEntry, ComparisonBase, ComparisonKind, EntryProblem, LIST_OUTPUT_BYTES,
    Unavailable, WorkingCopyChanges, count_lines, first_line, mark_base_objects, mark_filtered,
    parse_raw_diff, run_allowing_absent, verify_commit,
};

/// Longer revisions are refused before Git runs.
pub const MAXIMUM_REVISION_BYTES: usize = 1_024;
/// How many branch and tag names a listing offers.
pub const MAXIMUM_REFS: usize = 1_000;
/// Git's empty tree, in SHA-1 and SHA-256 repositories. As a base it shows a
/// root commit's whole tree as added, since a root commit has no parent.
pub const EMPTY_TREES: [&str; 2] = [
    "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",
];

/// How many of a branch's commits a listing offers.
pub const MAXIMUM_BRANCH_COMMITS: usize = 200;

/// Which requested revision a problem is about.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RevisionField {
    Base,
    Head,
    Both,
}

/// Why a revision comparison couldn't be made.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RevisionUnavailable {
    /// A revision Git must not be asked about: too long, starting with `-`, or
    /// holding control characters.
    Invalid(RevisionField),
    /// A revision that doesn't name a commit here.
    NotFound(RevisionField),
    /// The base and head share no commit.
    NoCommonHistory,
    /// The comparison failed for a reason any comparison can have.
    Comparison(Unavailable),
}

impl From<Unavailable> for RevisionUnavailable {
    fn from(problem: Unavailable) -> Self {
        Self::Comparison(problem)
    }
}

impl From<GitError> for RevisionUnavailable {
    fn from(error: GitError) -> Self {
        Self::Comparison(error.into())
    }
}

/// The revisions as asked for. An empty base means the default branch, else
/// HEAD; an empty head means the working copy.
#[derive(Clone, Copy, Debug)]
pub struct RevisionRequest<'a> {
    pub base: &'a str,
    pub head: &'a str,
}

/// A resolved revision comparison.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RevisionComparison {
    /// `base` is the merge base; `head` the head commit (HEAD for the working
    /// copy); `reference` and `reference_commit` the base revision as named and resolved.
    pub base: ComparisonBase,
    pub base_display: String,
    /// Empty for the working copy.
    pub head_display: String,
    pub head_is_working_copy: bool,
}

/// `text` trimmed, if it is a revision Git may be asked about; `None` when empty.
pub fn checked_revision(
    text: &str,
    field: RevisionField,
) -> Result<Option<String>, RevisionUnavailable> {
    let revision = text.trim();
    if revision.is_empty() {
        return Ok(None);
    }
    if revision.len() > MAXIMUM_REVISION_BYTES
        || revision.starts_with('-')
        || revision.chars().any(char::is_control)
    {
        return Err(RevisionUnavailable::Invalid(field));
    }
    Ok(Some(revision.to_string()))
}

/// Resolves the base and head, and their merge base, as the reference app does.
pub async fn resolve_revisions(
    repository: &GitRepository,
    request: RevisionRequest<'_>,
    deadline: Instant,
) -> Result<RevisionComparison, RevisionUnavailable> {
    let base_text = checked_revision(request.base, RevisionField::Base)?;
    let head_text = checked_revision(request.head, RevisionField::Head)?;
    if let (Some(base), Some(head)) = (&base_text, &head_text)
        && EMPTY_TREES.contains(&base.as_str())
    {
        return against_empty_tree(repository, base, head, deadline).await;
    }
    // The base as resolved, and as shown. A detected default branch resolves by its
    // full ref, so a tag or branch sharing its short name can't stand in for it.
    let (base_revision, base_display) = match base_text {
        Some(text) => (text.clone(), text),
        None => match working_copy::default_branch(repository, deadline).await? {
            Some((full, short)) => (full, short),
            None => ("HEAD".to_string(), "HEAD".to_string()),
        },
    };
    let head_commit = match &head_text {
        None => Some(
            verify_commit(repository, "HEAD", deadline)
                .await?
                .ok_or(Unavailable::NoCommits)?,
        ),
        Some(text) => verify_commit(repository, text, deadline).await?,
    };
    let base_commit = verify_commit(repository, &base_revision, deadline).await?;
    let (base_commit, head_commit) = match (base_commit, head_commit) {
        (Some(base), Some(head)) => (base, head),
        (None, None) => return Err(RevisionUnavailable::NotFound(RevisionField::Both)),
        (None, Some(_)) => return Err(RevisionUnavailable::NotFound(RevisionField::Base)),
        (Some(_), None) => return Err(RevisionUnavailable::NotFound(RevisionField::Head)),
    };
    let merge_base = GitCommand::MergeBase {
        left: base_commit.clone(),
        right: head_commit.clone(),
    };
    let Some(stdout) = run_allowing_absent(repository, merge_base, deadline).await? else {
        return Err(RevisionUnavailable::NoCommonHistory);
    };
    let merge_base = first_line(&stdout);
    if !is_object_name(&merge_base) {
        return Err(Unavailable::GitFailed(format!(
            "git merge-base printed an unexpected result: {merge_base}"
        ))
        .into());
    }
    Ok(RevisionComparison {
        base: ComparisonBase {
            kind: ComparisonKind::Revisions,
            head: head_commit,
            base: merge_base,
            reference: Some(base_display.clone()),
            reference_commit: Some(base_commit),
        },
        base_display,
        head_display: head_text.clone().unwrap_or_default(),
        head_is_working_copy: head_text.is_none(),
    })
}

/// A root commit compared with the empty tree: everything in it is added.
async fn against_empty_tree(
    repository: &GitRepository,
    empty_tree: &str,
    head: &str,
    deadline: Instant,
) -> Result<RevisionComparison, RevisionUnavailable> {
    let head_commit = verify_commit(repository, head, deadline)
        .await?
        .ok_or(RevisionUnavailable::NotFound(RevisionField::Head))?;
    // The other hash format's empty tree can't be compared with this repository's commits.
    if head_commit.len() != empty_tree.len() {
        return Err(RevisionUnavailable::NotFound(RevisionField::Base));
    }
    // Only a root commit compares with the empty tree; any other head would show
    // its whole tree as added.
    if !is_root_commit(repository, &head_commit, deadline).await? {
        return Err(RevisionUnavailable::NotFound(RevisionField::Base));
    }
    Ok(RevisionComparison {
        base: ComparisonBase {
            kind: ComparisonKind::Revisions,
            head: head_commit,
            base: empty_tree.to_string(),
            reference: Some(empty_tree.to_string()),
            reference_commit: Some(empty_tree.to_string()),
        },
        base_display: empty_tree.to_string(),
        head_display: head.to_string(),
        head_is_working_copy: false,
    })
}

/// Whether `commit`, a full object name, has no parents. Read from its raw
/// object, since a shallow clone hides the parents of its boundary commits.
async fn is_root_commit(
    repository: &GitRepository,
    commit: &str,
    deadline: Instant,
) -> Result<bool, GitError> {
    let object = repository
        .run(
            &GitCommand::CommitObject {
                commit: commit.to_string(),
            },
            None,
            CallLimits {
                deadline,
                stdout_bytes: LIST_OUTPUT_BYTES,
            },
        )
        .await?;
    Ok(commit_header_has_no_parents(&object))
}

/// Whether a raw commit object's header, before its first blank line, names no parent.
fn commit_header_has_no_parents(object: &[u8]) -> bool {
    !String::from_utf8_lossy(object)
        .lines()
        .take_while(|line| !line.is_empty())
        .any(|line| line.starts_with("parent "))
}

/// The files that differ from the merge base. For the working copy, exactly as
/// the working-copy comparison lists them; for a committed head, the tracked
/// files that differ between the two commits, with each file's blob at the head.
pub async fn list_revision_changes(
    repository: &GitRepository,
    comparison: &RevisionComparison,
    deadline: Instant,
) -> Result<WorkingCopyChanges, RevisionUnavailable> {
    if comparison.head_is_working_copy {
        return Ok(
            working_copy::list_changes(repository, comparison.base.clone(), deadline).await?,
        );
    }
    let base = comparison.base.base.clone();
    let head = comparison.base.head.clone();
    let listing = CallLimits {
        deadline,
        stdout_bytes: LIST_OUTPUT_BYTES,
    };
    let output = repository
        .run(
            &GitCommand::DiffCommitsRaw {
                base: base.clone(),
                head: head.clone(),
            },
            None,
            listing,
        )
        .await?;
    let mut unrepresentable = 0;
    let mut entries = parse_raw_diff(&output, &mut unrepresentable, true)?;
    mark_base_objects(repository, &mut entries, deadline).await?;
    mark_head_objects(repository, &mut entries, deadline).await?;
    mark_filtered(repository, &mut entries, deadline).await?;
    count_lines(
        repository,
        |paths| GitCommand::DiffCommitsNumstat {
            base: base.clone(),
            head: head.clone(),
            paths,
        },
        &mut entries,
        deadline,
    )
    .await?;
    entries.sort_by(|left, right| left.path.cmp(&right.path));
    Ok(WorkingCopyChanges {
        base: comparison.base.clone(),
        entries,
        unrepresentable_names: unrepresentable,
    })
}

/// Head blob sizes, and which head blobs this clone doesn't have.
async fn mark_head_objects(
    repository: &GitRepository,
    entries: &mut [ChangedEntry],
    deadline: Instant,
) -> Result<(), Unavailable> {
    let objects: Vec<String> = entries
        .iter()
        .filter(|entry| entry.working_kind != Some(working_copy::EntryKind::Submodule))
        .filter_map(|entry| entry.committed.as_ref().map(|blob| blob.object.clone()))
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
        match line.split(' ').collect::<Vec<_>>()[..] {
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
        let Some(blob) = entry.committed.as_mut() else {
            continue;
        };
        match sizes.get(&blob.object) {
            Some(Some(size)) => blob.size = Some(*size),
            Some(None) => entry.problem = Some(EntryProblem::MissingObject),
            None => {}
        }
    }
    Ok(())
}

/// Branch and tag names to suggest, and the default branch's short name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RepositoryRefs {
    pub refs: Vec<String>,
    pub truncated: bool,
    pub default_branch: Option<String>,
}

/// Remote HEADs the ref listing may skip while still filling its limit; one per remote.
const REMOTE_HEAD_ALLOWANCE: usize = 64;

/// One ref from `for-each-ref`: `refname\0short\0symref` per line.
struct ListedRef<'a> {
    full: &'a str,
    short: &'a str,
    symref: &'a str,
}

/// Whether a listed ref is a remote's HEAD: a symbolic ref directly under a
/// remote, which names no branch of its own. Branches and tags merely ending in
/// `/HEAD`, such as `topic/HEAD`, are ordinary refs.
fn is_remote_head(listed: &ListedRef<'_>) -> bool {
    let Some(rest) = listed.full.strip_prefix("refs/remotes/") else {
        return false;
    };
    !listed.symref.is_empty() && rest.split_once('/').is_some_and(|(_, name)| name == "HEAD")
}

/// The usable short names from a listing that asked Git for `requested` refs.
fn usable_refs(output: &str, requested: usize) -> (Vec<String>, bool) {
    let listed: Vec<ListedRef<'_>> = output
        .lines()
        .filter_map(|line| {
            let mut fields = line.split('\0');
            Some(ListedRef {
                full: fields.next()?,
                short: fields.next()?,
                symref: fields.next().unwrap_or_default(),
            })
        })
        .collect();
    // Git stopped at the count asked for, so there may be more than were listed.
    let limited = listed.len() >= requested;
    let mut refs: Vec<String> = listed
        .iter()
        .filter(|listed| !listed.short.is_empty() && !is_remote_head(listed))
        .map(|listed| listed.short.to_string())
        .collect();
    let truncated = refs.len() > MAXIMUM_REFS || limited;
    refs.truncate(MAXIMUM_REFS);
    (refs, truncated)
}

/// At most [`MAXIMUM_REFS`] short names: local branches, remote branches, then
/// tags, without remotes' symbolic HEADs.
pub async fn list_refs(
    repository: &GitRepository,
    deadline: Instant,
) -> Result<RepositoryRefs, Unavailable> {
    let requested = MAXIMUM_REFS + 1 + REMOTE_HEAD_ALLOWANCE;
    let output = repository
        .run(
            &GitCommand::ListRefs { count: requested },
            None,
            CallLimits {
                deadline,
                stdout_bytes: LIST_OUTPUT_BYTES,
            },
        )
        .await?;
    let (refs, truncated) = usable_refs(&String::from_utf8_lossy(&output), requested);
    let default_branch = working_copy::default_branch(repository, deadline)
        .await
        .map_err(Unavailable::from)?
        .map(|(_full, short)| short);
    Ok(RepositoryRefs {
        refs,
        truncated,
        default_branch,
    })
}

/// One commit of a branch, as `git log` describes it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BranchCommit {
    pub object: String,
    /// It has no parent, so it is shown against the empty tree.
    pub root: bool,
    pub subject: String,
    pub author_name: String,
    pub author_time_unix: i64,
}

/// The commits HEAD has beyond its merge base with a base, newest first.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BranchCommits {
    pub comparison: RevisionComparison,
    pub commits: Vec<BranchCommit>,
    pub truncated: bool,
}

/// The commits from `log -z` with the [`GitCommand::ListCommits`] format;
/// records that don't start with a full object name are skipped.
fn parse_commits(output: &[u8]) -> Vec<BranchCommit> {
    String::from_utf8_lossy(output)
        .split('\0')
        .filter_map(|record| {
            let mut fields = record.trim_start_matches('\n').splitn(5, '\x1f');
            let object = fields.next()?;
            if !is_object_name(object) {
                return None;
            }
            let parents = fields.next()?;
            Some(BranchCommit {
                object: object.to_string(),
                root: parents.trim().is_empty(),
                author_time_unix: fields.next()?.parse().unwrap_or_default(),
                author_name: fields.next()?.to_string(),
                subject: fields.next().unwrap_or_default().to_string(),
            })
        })
        .collect()
}

/// At most [`MAXIMUM_BRANCH_COMMITS`] commits reachable from HEAD but not from
/// its merge base with `base` (empty: the default branch, else HEAD).
pub async fn list_branch_commits(
    repository: &GitRepository,
    base: &str,
    deadline: Instant,
) -> Result<BranchCommits, RevisionUnavailable> {
    let comparison =
        resolve_revisions(repository, RevisionRequest { base, head: "HEAD" }, deadline).await?;
    let output = repository
        .run(
            &GitCommand::ListCommits {
                base: comparison.base.base.clone(),
                head: comparison.base.head.clone(),
                count: MAXIMUM_BRANCH_COMMITS + 1,
            },
            None,
            CallLimits {
                deadline,
                stdout_bytes: LIST_OUTPUT_BYTES,
            },
        )
        .await?;
    let mut commits = parse_commits(&output);
    let truncated = commits.len() > MAXIMUM_BRANCH_COMMITS;
    commits.truncate(MAXIMUM_BRANCH_COMMITS);
    // A shallow clone's boundary commit lists no parents but isn't a root.
    for commit in commits.iter_mut().filter(|commit| commit.root) {
        commit.root = is_root_commit(repository, &commit.object, deadline).await?;
    }
    Ok(BranchCommits {
        comparison,
        commits,
        truncated,
    })
}

#[cfg(test)]
#[path = "revision_comparison_test.rs"]
mod tests;

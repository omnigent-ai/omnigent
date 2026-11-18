//! Runs a fixed set of read-only Git commands for a workspace. Every call gets
//! a clean environment and configuration overrides that keep Git from writing
//! to the repository, fetching, prompting, or running configured helpers, and
//! is bounded by a deadline, output caps, and concurrency limits.

use std::{
    collections::{BTreeSet, HashMap},
    path::{Path, PathBuf},
    process::Stdio,
    sync::{Arc, LazyLock, Mutex, Weak},
    time::Duration,
};

use thiserror::Error;
use tokio::{
    sync::{OwnedSemaphorePermit, Semaphore},
    time::Instant,
};

/// Git children running at once for the whole helper.
const HELPER_CONCURRENCY: usize = 4;
/// Git children running at once for one repository.
const REPOSITORY_CONCURRENCY: usize = 2;
/// Stderr kept for a failure's summary; the rest is drained and dropped.
const STDERR_BYTES: usize = 64 * 1024;
/// `GIT_NO_LAZY_FETCH` arrived in this release; older Git may fetch missing
/// objects in a partial clone, so those repositories are refused.
const NO_LAZY_FETCH_VERSION: (u32, u32) = (2, 44);

static HELPER_SLOTS: LazyLock<Arc<Semaphore>> =
    LazyLock::new(|| Arc::new(Semaphore::new(HELPER_CONCURRENCY)));

/// Background Git children, such as untracked-file discovery, running at
/// once for the whole helper. They hold none of the slots above, so a long
/// scan never delays an interactive request.
const BACKGROUND_CONCURRENCY: usize = 2;

static BACKGROUND_SLOTS: LazyLock<Arc<Semaphore>> =
    LazyLock::new(|| Arc::new(Semaphore::new(BACKGROUND_CONCURRENCY)));

/// The helper's slots for a Git child started from this thread.
fn helper_slots() -> Arc<Semaphore> {
    #[cfg(test)]
    if let Some((helper, _)) = test_slots() {
        return helper;
    }
    HELPER_SLOTS.clone()
}

/// The background slots for a Git child started from this thread.
fn background_slots() -> Arc<Semaphore> {
    #[cfg(test)]
    if let Some((_, background)) = test_slots() {
        return background;
    }
    BACKGROUND_SLOTS.clone()
}

/// Which slots a Git child waits for before it starts.
#[derive(Clone, Copy)]
enum Admission<'a> {
    /// The helper's slots, as before a repository is known.
    Helper,
    /// The repository's slots, then the helper's.
    Repository(&'a Arc<Semaphore>),
    /// The background slots only.
    Background,
}

/// One admission limit per repository, by common directory, shared by every
/// request that opens it.
static REPOSITORY_SLOTS: LazyLock<Mutex<HashMap<PathBuf, Weak<Semaphore>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

fn repository_slots(common_directory: &Path) -> Arc<Semaphore> {
    let key = std::fs::canonicalize(common_directory).unwrap_or_else(|_| common_directory.into());
    let mut registry = REPOSITORY_SLOTS
        .lock()
        .unwrap_or_else(|poison| poison.into_inner());
    registry.retain(|_, slots| slots.strong_count() > 0);
    if let Some(slots) = registry.get(&key).and_then(Weak::upgrade) {
        return slots;
    }
    let slots = Arc::new(Semaphore::new(REPOSITORY_CONCURRENCY));
    registry.insert(key, Arc::downgrade(&slots));
    slots
}

/// Variables kept from the helper's environment. Everything else, including
/// every `GIT_*` variable, is dropped so nothing can redirect the repository,
/// its configuration, or its object store.
const INHERITED_VARIABLES: &[&str] = &["PATH", "HOME", "XDG_CONFIG_HOME", "TMPDIR"];

const FIXED_VARIABLES: &[(&str, &str)] = &[
    ("GIT_OPTIONAL_LOCKS", "0"),
    ("GIT_NO_LAZY_FETCH", "1"),
    ("GIT_TERMINAL_PROMPT", "0"),
    // Empty: no transport may run, even if something asks for a fetch.
    ("GIT_ALLOW_PROTOCOL", ""),
    ("GIT_LITERAL_PATHSPECS", "1"),
    ("GIT_NO_REPLACE_OBJECTS", "1"),
    ("GIT_PAGER", "cat"),
    ("LC_ALL", "C"),
];

/// Overrides on every call: no fsmonitor hook, no index or untracked-cache
/// writes, no hooks, credential helpers, external diff, or transports.
const FIXED_CONFIGURATION: &[&str] = &[
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "core.hooksPath=/dev/null",
    "core.pager=cat",
    "credential.helper=",
    "diff.external=",
    "protocol.allow=never",
    "fetch.negotiationAlgorithm=noop",
];

#[derive(Debug, Error, PartialEq, Eq)]
pub enum GitError {
    #[error("Git isn't installed or couldn't start")]
    Unavailable,
    #[error("Git took too long")]
    Timeout,
    #[error("Git's output was larger than {0} bytes")]
    OutputTooLarge(usize),
    #[error("An object this comparison needs isn't in the repository")]
    MissingObject,
    #[error("This partial clone needs Git 2.44 or newer to read without fetching")]
    PartialCloneNeedsNewerGit,
    /// Something other than this runner stopped Git with a signal.
    #[error("git {command} was stopped by signal {signal} after {ran_for_ms} ms")]
    Interrupted {
        command: String,
        signal: i32,
        ran_for_ms: u128,
    },
    #[error("Git failed: {0}")]
    Failed(String),
}

/// A fixed, read-only Git command. Arguments that come from requests are only
/// ever object names checked by [`is_object_name`], ref names this module
/// produced, or paths passed after `--` or on stdin.
#[derive(Clone, Debug)]
pub enum GitCommand {
    Version,
    /// `--git-dir`, `--git-common-dir`, `--show-toplevel`, `--show-prefix`.
    Discover,
    /// The configuration that changes how reads behave: filter drivers and
    /// partial-clone settings, as `key\nvalue\0` records.
    ReadBehaviorConfiguration,
    SymbolicRefHead,
    /// `%(refname)\0%(upstream)\0%(upstream:remotename)\0%(push)\0%(push:remotename)`
    /// for one local branch ref.
    BranchTracking {
        branch_ref: String,
    },
    /// The ref `refs/remotes/<remote>/HEAD` points to.
    RemoteDefaultRef {
        remote: String,
    },
    VerifyCommit {
        revision: String,
    },
    IsShallow,
    MergeBase {
        left: String,
        right: String,
    },
    /// Tracked changes against `base` beneath the working directory, as
    /// `--raw -z` records with full object names.
    DiffRaw {
        base: String,
    },
    /// Line counts against `base` for `paths`, given after `--`.
    DiffNumstat {
        base: String,
        paths: Vec<String>,
    },
    /// Tracked changes from commit `base` to commit `head` beneath the working
    /// directory, as `--raw -z` records with full object names.
    DiffCommitsRaw {
        base: String,
        head: String,
    },
    /// Line counts from commit `base` to commit `head` for `paths`, after `--`.
    DiffCommitsNumstat {
        base: String,
        head: String,
        paths: Vec<String>,
    },
    /// Local branches, remote branches and tags, at most `count`, as
    /// `refname\0short name\0symref target` lines.
    ListRefs {
        count: usize,
    },
    /// At most `count` commits reachable from commit `head` but not from commit
    /// `base`, newest first, as `object\x1fparents\x1ftime\x1fauthor\x1fsubject` records
    /// ended by NUL.
    ListCommits {
        base: String,
        head: String,
        count: usize,
    },
    /// Every tracked path beneath the working directory, from the index.
    ListTracked,
    /// Untracked, unignored files beneath the working directory, as
    /// `git status` finds them; a nested repository is one `path/` record.
    ListUntracked,
    /// Paths with unmerged index entries beneath the working directory.
    ListUnmerged,
    /// `<oid> <type> <size>` or `<oid> missing` for each oid on stdin.
    ObjectSizes,
    /// The `filter` attribute of each NUL-separated path on stdin.
    FilterAttributes,
    /// Mode, type, and object of one top-level path in a commit's tree.
    TreeEntry {
        commit: String,
        top_path: String,
    },
    BlobContents {
        object: String,
    },
    /// A commit's raw object, whose `parent` lines a shallow clone keeps.
    CommitObject {
        commit: String,
    },
    /// Every `remote.*`, `branch.*`, `url.*` and `push.*` key with its value and
    /// origin, as `origin\0key\nvalue\0` records. Values stay in the helper.
    ConfigSnapshot,
    /// `%(push)\0%(push:remotename)\0%(upstream)` for one local branch ref.
    BranchPush {
        branch_ref: String,
    },
    /// The commit a ref this module produced points to, if any.
    RefCommit {
        reference: String,
    },
}

impl GitCommand {
    /// The Git subcommand, for error messages.
    pub fn name(&self) -> String {
        self.arguments().into_iter().next().unwrap_or_default()
    }

    fn arguments(&self) -> Vec<String> {
        let owned = |values: &[&str]| values.iter().map(|value| value.to_string()).collect();
        match self {
            Self::Version => owned(&["version"]),
            Self::Discover => owned(&[
                "rev-parse",
                "--absolute-git-dir",
                "--git-common-dir",
                "--show-toplevel",
                "--show-prefix",
            ]),
            Self::ReadBehaviorConfiguration => owned(&[
                "config",
                "--null",
                "--get-regexp",
                r"^(filter\..*\.(clean|process|smudge|required)|extensions\.partialclone|remote\..*\.promisor)$",
            ]),
            Self::SymbolicRefHead => owned(&["symbolic-ref", "-q", "HEAD"]),
            Self::BranchTracking { branch_ref } => vec![
                "for-each-ref".into(),
                "--format=%(refname)%00%(upstream)%00%(upstream:remotename)%00%(push)%00%(push:remotename)".into(),
                "--".into(),
                branch_ref.clone(),
            ],
            Self::RemoteDefaultRef { remote } => vec![
                "symbolic-ref".into(),
                "-q".into(),
                format!("refs/remotes/{remote}/HEAD"),
            ],
            Self::VerifyCommit { revision } => vec![
                "rev-parse".into(),
                "--verify".into(),
                "-q".into(),
                "--end-of-options".into(),
                format!("{revision}^{{commit}}"),
            ],
            Self::IsShallow => owned(&["rev-parse", "--is-shallow-repository"]),
            Self::MergeBase { left, right } => vec![
                "merge-base".into(),
                "--end-of-options".into(),
                left.clone(),
                right.clone(),
            ],
            Self::DiffRaw { base } => vec![
                "diff".into(),
                "--raw".into(),
                "-z".into(),
                "--no-abbrev".into(),
                "--no-renames".into(),
                "--no-ext-diff".into(),
                "--no-textconv".into(),
                "--ignore-submodules=none".into(),
                "--relative".into(),
                base.clone(),
                "--".into(),
                ".".into(),
            ],
            Self::DiffNumstat { base, paths } => {
                let mut arguments: Vec<String> = vec![
                    "diff".into(),
                    "--numstat".into(),
                    "-z".into(),
                    "--no-renames".into(),
                    "--no-ext-diff".into(),
                    "--no-textconv".into(),
                    "--ignore-submodules=none".into(),
                    "--relative".into(),
                    base.clone(),
                    "--".into(),
                ];
                arguments.extend(paths.iter().cloned());
                arguments
            }
            Self::DiffCommitsRaw { base, head } => vec![
                "diff".into(),
                "--raw".into(),
                "-z".into(),
                "--no-abbrev".into(),
                "--no-renames".into(),
                "--no-ext-diff".into(),
                "--no-textconv".into(),
                "--ignore-submodules=none".into(),
                "--relative".into(),
                "--end-of-options".into(),
                base.clone(),
                head.clone(),
                "--".into(),
                ".".into(),
            ],
            Self::DiffCommitsNumstat { base, head, paths } => {
                let mut arguments: Vec<String> = vec![
                    "diff".into(),
                    "--numstat".into(),
                    "-z".into(),
                    "--no-renames".into(),
                    "--no-ext-diff".into(),
                    "--no-textconv".into(),
                    "--ignore-submodules=none".into(),
                    "--relative".into(),
                    "--end-of-options".into(),
                    base.clone(),
                    head.clone(),
                    "--".into(),
                ];
                arguments.extend(paths.iter().cloned());
                arguments
            }
            Self::ListRefs { count } => vec![
                "for-each-ref".into(),
                format!("--count={count}"),
                "--format=%(refname)%00%(refname:short)%00%(symref)".into(),
                "--".into(),
                "refs/heads".into(),
                "refs/remotes".into(),
                "refs/tags".into(),
            ],
            Self::ListCommits { base, head, count } => vec![
                "log".into(),
                "-z".into(),
                "--no-color".into(),
                "--no-show-signature".into(),
                format!("--max-count={count}"),
                "--format=%H%x1f%P%x1f%at%x1f%an%x1f%s".into(),
                "--end-of-options".into(),
                head.clone(),
                format!("^{base}"),
                "--".into(),
            ],
            Self::ListTracked => owned(&["ls-files", "--cached", "-z", "--", "."]),
            Self::ListUntracked => owned(&["ls-files", "--others", "--exclude-standard", "-z", "--", "."]),
            Self::ListUnmerged => owned(&["ls-files", "--unmerged", "-z", "--", "."]),
            Self::ObjectSizes => owned(&["cat-file", "--batch-check"]),
            Self::FilterAttributes => owned(&["check-attr", "-z", "--stdin", "filter"]),
            Self::TreeEntry { commit, top_path } => vec![
                "ls-tree".into(),
                "-z".into(),
                "--full-tree".into(),
                "--no-abbrev".into(),
                commit.clone(),
                "--".into(),
                top_path.clone(),
            ],
            Self::BlobContents { object } => {
                vec!["cat-file".into(), "blob".into(), object.clone()]
            }
            Self::CommitObject { commit } => {
                vec!["cat-file".into(), "commit".into(), commit.clone()]
            }
            Self::ConfigSnapshot => owned(&[
                "config",
                "-z",
                "--show-origin",
                "--get-regexp",
                r"^(remote|branch|url|push)\.",
            ]),
            Self::BranchPush { branch_ref } => vec![
                "for-each-ref".into(),
                "--format=%(push)%00%(push:remotename)%00%(upstream)".into(),
                "--".into(),
                branch_ref.clone(),
            ],
            Self::RefCommit { reference } => vec![
                "rev-parse".into(),
                "--verify".into(),
                "-q".into(),
                "--end-of-options".into(),
                format!("{reference}^{{commit}}"),
            ],
        }
    }
}

/// A full SHA-1 or SHA-256 object name.
pub fn is_object_name(text: &str) -> bool {
    matches!(text.len(), 40 | 64) && text.bytes().all(|byte| byte.is_ascii_hexdigit())
}

/// Limits for one call. `deadline` is shared by every call of one request.
#[derive(Clone, Copy, Debug)]
pub struct CallLimits {
    pub deadline: Instant,
    pub stdout_bytes: usize,
}

/// A command's output: stdout, and whether it exited successfully.
#[derive(Debug)]
pub struct GitOutput {
    pub stdout: Vec<u8>,
    pub success: bool,
    pub exit_code: Option<i32>,
    /// The signal that stopped Git, when it didn't exit on its own.
    pub signal: Option<i32>,
    pub stderr: String,
    /// How long Git ran, from just before spawn until its exit was seen, so it includes spawn and
    /// observation overhead: never less than Git's own run time, and not an exact measure of it.
    pub ran_for: Duration,
}

/// A repository located from a workspace directory, with everything later
/// calls need to stay on it.
#[derive(Debug)]
pub struct GitRepository {
    /// Where commands run; `--relative` and `-- .` scope them to it.
    pub working_directory: PathBuf,
    pub git_directory: PathBuf,
    pub common_directory: PathBuf,
    pub top_level: PathBuf,
    /// The working directory relative to the top level, `/`-separated, with a
    /// trailing `/` unless empty.
    pub prefix: String,
    filter_drivers: BTreeSet<String>,
    partial_clone: bool,
    slots: Arc<Semaphore>,
}

impl GitRepository {
    /// Locates the repository that contains `working_directory`, or `None`
    /// outside one. Also reads the configuration later calls must neutralize.
    pub async fn discover(
        working_directory: &Path,
        deadline: Instant,
    ) -> Result<Option<Self>, GitError> {
        let limits = CallLimits {
            deadline,
            stdout_bytes: 64 * 1024,
        };
        let output = run_git(
            working_directory,
            None,
            Admission::Helper,
            &[],
            &GitCommand::Discover,
            None,
            limits,
        )
        .await?;
        if !output.success {
            if output.stderr.contains("not a git repository") {
                return Ok(None);
            }
            return Err(failure_of(&output, "rev-parse"));
        }
        let text = String::from_utf8(output.stdout)
            .map_err(|_| GitError::Failed("The repository path isn't valid UTF-8".into()))?;
        let mut lines = text.split('\n');
        let mut next = || lines.next().map(str::to_string).unwrap_or_default();
        let git_directory = PathBuf::from(next());
        let common = next();
        let top_level = PathBuf::from(next());
        let prefix = next();
        if top_level.as_os_str().is_empty() {
            // A bare repository or the inside of `.git`: nothing to compare.
            return Ok(None);
        }
        let common_directory = if Path::new(&common).is_absolute() {
            PathBuf::from(common)
        } else {
            working_directory.join(common)
        };
        let mut repository = Self {
            working_directory: working_directory.to_path_buf(),
            git_directory,
            common_directory,
            top_level,
            prefix,
            filter_drivers: BTreeSet::new(),
            partial_clone: false,
            slots: Arc::new(Semaphore::new(REPOSITORY_CONCURRENCY)),
        };
        repository.slots = repository_slots(&repository.common_directory);
        repository.read_behavior_configuration(deadline).await?;
        Ok(Some(repository))
    }

    async fn read_behavior_configuration(&mut self, deadline: Instant) -> Result<(), GitError> {
        let output = self
            .run_unchecked(
                &GitCommand::ReadBehaviorConfiguration,
                None,
                CallLimits {
                    deadline,
                    stdout_bytes: 256 * 1024,
                },
            )
            .await?;
        // Exit 1 means no matching keys.
        if !output.success && output.exit_code != Some(1) {
            return Err(failure_of(&output, "config"));
        }
        // Refused before any other command runs: an unreadable key could hide a filter.
        let configuration = BehaviorConfiguration::parse(&output.stdout)?;
        self.filter_drivers = configuration.filter_drivers;
        self.partial_clone = configuration.partial_clone;
        if self.partial_clone && !git_supports_no_lazy_fetch(deadline).await? {
            return Err(GitError::PartialCloneNeedsNewerGit);
        }
        Ok(())
    }

    /// Filter drivers configured for this repository, by name.
    pub fn filter_drivers(&self) -> impl Iterator<Item = &str> {
        self.filter_drivers.iter().map(String::as_str)
    }

    /// Runs `command`, failing unless Git exits successfully.
    pub async fn run(
        &self,
        command: &GitCommand,
        stdin: Option<Vec<u8>>,
        limits: CallLimits,
    ) -> Result<Vec<u8>, GitError> {
        let output = self.run_unchecked(command, stdin, limits).await?;
        if output.success {
            Ok(output.stdout)
        } else {
            Err(failure_of(&output, &command.name()))
        }
    }

    /// Runs `command` and returns its output whatever its exit status.
    pub async fn run_unchecked(
        &self,
        command: &GitCommand,
        stdin: Option<Vec<u8>>,
        limits: CallLimits,
    ) -> Result<GitOutput, GitError> {
        let configuration = filter_overrides(&self.filter_drivers);
        let repository = [
            format!("--git-dir={}", self.git_directory.display()),
            format!("--work-tree={}", self.top_level.display()),
        ];
        run_git(
            &self.working_directory,
            Some(&repository),
            Admission::Repository(&self.slots),
            &configuration,
            command,
            stdin,
            limits,
        )
        .await
    }

    /// Runs `command` in the background slots, failing unless Git exits
    /// successfully. For long scans no request waits on.
    pub async fn run_in_background(
        &self,
        command: &GitCommand,
        limits: CallLimits,
    ) -> Result<Vec<u8>, GitError> {
        let configuration = filter_overrides(&self.filter_drivers);
        let repository = [
            format!("--git-dir={}", self.git_directory.display()),
            format!("--work-tree={}", self.top_level.display()),
        ];
        let output = run_git(
            &self.working_directory,
            Some(&repository),
            Admission::Background,
            &configuration,
            command,
            None,
            limits,
        )
        .await?;
        if output.success {
            Ok(output.stdout)
        } else {
            Err(failure_of(&output, &command.name()))
        }
    }
}

pub(crate) fn failure_of(output: &GitOutput, command: &str) -> GitError {
    if let Some(signal) = output.signal {
        return GitError::Interrupted {
            command: command.to_string(),
            signal,
            ran_for_ms: output.ran_for.as_millis(),
        };
    }
    let stderr = output.stderr.trim();
    if stderr.contains("missing blob object")
        || stderr.contains("unable to read")
        || stderr.contains("bad object")
        || stderr.contains("could not fetch")
        || stderr.contains("lazy fetching disabled")
    {
        return GitError::MissingObject;
    }
    let summary: String = stderr
        .lines()
        .last()
        .unwrap_or("")
        .chars()
        .take(300)
        .collect();
    if summary.is_empty() {
        let status = output.exit_code.unwrap_or_default();
        return GitError::Failed(format!("git {command} exited with {status}"));
    }
    GitError::Failed(summary)
}

/// Configuration that changes how reads behave, from
/// `git config --null --get-regexp` records (`key\nvalue\0`).
#[derive(Debug, Default, PartialEq, Eq)]
struct BehaviorConfiguration {
    /// Filter driver names, spelled as configured: subsections are case-sensitive.
    filter_drivers: BTreeSet<String>,
    partial_clone: bool,
}

impl BehaviorConfiguration {
    /// Values are opaque bytes; keys must be readable, and filter driver names
    /// must survive a `-c` override, or the repository is refused.
    fn parse(records: &[u8]) -> Result<Self, GitError> {
        let unsafe_key = || {
            GitError::Failed(
                "This repository's Git configuration has a filter or remote name that can't be read safely"
                    .into(),
            )
        };
        let mut configuration = Self::default();
        for record in records.split(|&byte| byte == 0) {
            if record.is_empty() {
                continue;
            }
            let (key, value) = match record.iter().position(|&byte| byte == b'\n') {
                Some(end) => (&record[..end], &record[end + 1..]),
                None => (record, &[][..]),
            };
            let key = std::str::from_utf8(key).map_err(|_| unsafe_key())?;
            // Section and variable names are case-insensitive; the subsection between them isn't.
            let Some((section, rest)) = key.split_once('.') else {
                continue;
            };
            let (subsection, variable) = rest.rsplit_once('.').unwrap_or(("", rest));
            match (
                section.to_ascii_lowercase().as_str(),
                variable.to_ascii_lowercase().as_str(),
            ) {
                ("filter", "clean" | "process" | "smudge" | "required")
                    if !subsection.is_empty() =>
                {
                    // `-c filter.<name>.clean=` splits at the first `=`.
                    if subsection.contains(['=', '\n']) {
                        return Err(unsafe_key());
                    }
                    configuration.filter_drivers.insert(subsection.to_string());
                }
                ("extensions", "partialclone") if subsection.is_empty() => {
                    configuration.partial_clone = true;
                }
                ("remote", "promisor") if value.eq_ignore_ascii_case(b"true") => {
                    configuration.partial_clone = true;
                }
                _ => {}
            }
        }
        Ok(configuration)
    }
}

/// `-c` overrides that turn off each configured filter driver.
fn filter_overrides(drivers: &BTreeSet<String>) -> Vec<String> {
    let mut overrides = Vec::new();
    for driver in drivers {
        for key in ["clean", "process", "smudge"] {
            overrides.push(format!("filter.{driver}.{key}="));
        }
        overrides.push(format!("filter.{driver}.required=false"));
    }
    overrides
}

async fn git_supports_no_lazy_fetch(deadline: Instant) -> Result<bool, GitError> {
    let output = run_git(
        Path::new("/"),
        None,
        Admission::Helper,
        &[],
        &GitCommand::Version,
        None,
        CallLimits {
            deadline,
            stdout_bytes: 1024,
        },
    )
    .await?;
    Ok(parse_version(&String::from_utf8_lossy(&output.stdout))
        .is_some_and(|version| version >= NO_LAZY_FETCH_VERSION))
}

/// `git version 2.54.0 (Apple Git-157)` → `(2, 54)`.
pub fn parse_version(text: &str) -> Option<(u32, u32)> {
    let version = text.trim().strip_prefix("git version ")?;
    let mut parts = version.split(|character: char| !character.is_ascii_digit());
    Some((parts.next()?.parse().ok()?, parts.next()?.parse().ok()?))
}

/// Admission for one child: the repository's slot first (when there is a
/// repository), then the helper's, always in that order; or a background slot.
async fn acquire_slots(
    admission: Admission<'_>,
    deadline: Instant,
) -> Result<Vec<OwnedSemaphorePermit>, GitError> {
    #[cfg(test)]
    if tests::EXIT_OBSERVER.get().is_some() {
        // Failed-watch tests hold a call until its deadline; they stay off the shared slots.
        return Ok(Vec::new());
    }
    let acquire = async {
        let mut permits = Vec::with_capacity(2);
        match admission {
            Admission::Helper => permits.push(helper_slots().acquire_owned().await?),
            Admission::Repository(slots) => {
                permits.push(slots.clone().acquire_owned().await?);
                permits.push(helper_slots().acquire_owned().await?);
            }
            Admission::Background => {
                permits.push(background_slots().acquire_owned().await?);
            }
        }
        Ok::<_, tokio::sync::AcquireError>(permits)
    };
    match tokio::time::timeout_at(deadline, acquire).await {
        Ok(Ok(permits)) => Ok(permits),
        Ok(Err(_)) => Err(GitError::Unavailable),
        Err(_) => Err(GitError::Timeout),
    }
}

/// A shell script standing in for Git, used by calls on this thread. It
/// runs through the system shell, because a freshly written executable can
/// wait on a security scan longer than the deadlines under test.
#[cfg(test)]
pub(crate) fn fake_git(body: &str) {
    TEST_SCRIPT.with(|script| *script.borrow_mut() = Some(body.to_string()));
}

/// The start of a fake Git script that answers discovery for a repository at
/// `root` with no behavior configuration; other commands fall through.
#[cfg(test)]
pub(crate) fn fake_discovery(root: &Path) -> String {
    format!(
        r#"case "$*" in
*rev-parse\ --absolute-git-dir*) printf '%s\n%s\n%s\n\n' '{root}/.git' '{root}/.git' '{root}'; exit 0;;
*config\ --null*) exit 1;;
esac
"#,
        root = root.display()
    )
}

/// The program to run, and arguments that go before Git's own.
#[cfg(not(test))]
fn git_program() -> (std::ffi::OsString, Vec<std::ffi::OsString>) {
    ("git".into(), Vec::new())
}

// Tests substitute a shell script for Git, per thread.
#[cfg(test)]
thread_local! {
    static TEST_SCRIPT: std::cell::RefCell<Option<String>> = const { std::cell::RefCell::new(None) };
    /// The helper and background slots of a thread with a script for Git, of the
    /// real sizes: a test that holds slots on purpose never holds another test's.
    static TEST_SLOTS: std::cell::OnceCell<(Arc<Semaphore>, Arc<Semaphore>)> =
        const { std::cell::OnceCell::new() };
}

/// This thread's own slots, once it stands a script in for Git.
#[cfg(test)]
fn test_slots() -> Option<(Arc<Semaphore>, Arc<Semaphore>)> {
    if TEST_SCRIPT.with(|script| script.borrow().is_none()) {
        return None;
    }
    Some(TEST_SLOTS.with(|slots| {
        slots
            .get_or_init(|| {
                (
                    Arc::new(Semaphore::new(HELPER_CONCURRENCY)),
                    Arc::new(Semaphore::new(BACKGROUND_CONCURRENCY)),
                )
            })
            .clone()
    }))
}

#[cfg(test)]
fn git_program() -> (std::ffi::OsString, Vec<std::ffi::OsString>) {
    match TEST_SCRIPT.with(|script| script.borrow().clone()) {
        // `git` becomes `$0`, so `$@` holds exactly Git's arguments.
        Some(script) => (
            "/bin/sh".into(),
            vec!["-c".into(), script.into(), "git".into()],
        ),
        None => ("git".into(), Vec::new()),
    }
}

/// A running Git child and its process group. A waiter thread owns the
/// `Child`: it observes the exit without reaping, then reaps only after the
/// guard has killed the group. The leader stays unreaped until then, so its
/// ID, and so the group ID, can't be reused, and descendants that outlive
/// the leader are killed too. Dropping the guard, as on a timeout or a
/// cancelled request, does the same.
/// Watches a process for its exit without reaping it; false when it can't.
type ExitObserver = fn(libc::pid_t) -> bool;

struct ChildGuard {
    process_group: libc::pid_t,
    release: Option<std::sync::mpsc::Sender<()>>,
    status: Option<tokio::sync::oneshot::Receiver<std::process::ExitStatus>>,
}

impl ChildGuard {
    /// Takes ownership of `child`; the receiver fires once it has exited.
    fn watch(child: std::process::Child) -> (Self, tokio::sync::oneshot::Receiver<()>) {
        #[cfg(test)]
        if let Some(observer) = tests::EXIT_OBSERVER.get() {
            return Self::watch_with(child, observer);
        }
        Self::watch_with(child, wait_without_reaping)
    }

    /// Like `watch`, with the exit observer given; it returns false when it
    /// couldn't observe the exit. The leader is reaped only after the guard
    /// has killed the group, whichever way the observer ends.
    fn watch_with(
        mut child: std::process::Child,
        observe_exit: ExitObserver,
    ) -> (Self, tokio::sync::oneshot::Receiver<()>) {
        let process_group = child.id() as libc::pid_t;
        let (send_exited, exited) = tokio::sync::oneshot::channel();
        let (release, released) = std::sync::mpsc::channel::<()>();
        let (send_status, status) = tokio::sync::oneshot::channel();
        std::thread::spawn(move || {
            if observe_exit(process_group) {
                let _ = send_exited.send(());
            } else {
                // The exit can't be seen, so it is never reported: the call
                // ends at its deadline or cancellation, never by killing a live Git.
                eprintln!(
                    "workspace-helper: couldn't watch git (pid {process_group}) exit: {}",
                    std::io::Error::last_os_error()
                );
                drop(send_exited);
            }
            // Ends when the guard has killed the group and dropped `release`.
            let _ = released.recv();
            if let Ok(exit) = child.wait() {
                let _ = send_status.send(exit);
            }
        });
        let guard = Self {
            process_group,
            release: Some(release),
            status: Some(status),
        };
        (guard, exited)
    }

    /// Kills whatever is left of the group, then lets the waiter reap.
    fn terminate(&mut self) {
        if let Some(release) = self.release.take() {
            // SAFETY: the leader is unreaped until `release` drops, so the group ID is ours.
            unsafe { libc::killpg(self.process_group, libc::SIGKILL) };
            drop(release);
        }
    }

    /// Cleans up an exited child and returns its exit status.
    async fn finish(mut self) -> Option<std::process::ExitStatus> {
        self.terminate();
        self.status.take()?.await.ok()
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        self.terminate();
    }
}

/// Blocks until `process_id` has exited, leaving it unreaped. Returns false
/// when waitid fails, so the exit wasn't observed.
fn wait_without_reaping(process_id: libc::pid_t) -> bool {
    loop {
        // SAFETY: `info` is a plain output struct for waitid.
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                process_id as libc::id_t,
                &mut info,
                libc::WEXITED | libc::WNOWAIT,
            )
        };
        if result == 0 {
            return true;
        }
        if std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted {
            return false;
        }
    }
}

/// Reads at most `limit` bytes; reports whether more was available.
fn read_capped(mut reader: impl std::io::Read, limit: usize) -> std::io::Result<(Vec<u8>, bool)> {
    let mut kept = Vec::new();
    let mut buffer = vec![0u8; 64 * 1024];
    loop {
        let read = match reader.read(&mut buffer) {
            Ok(read) => read,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        if read == 0 {
            return Ok((kept, false));
        }
        let room = limit.saturating_sub(kept.len());
        if read > room {
            kept.extend_from_slice(&buffer[..room]);
            return Ok((kept, true));
        }
        kept.extend_from_slice(&buffer[..read]);
    }
}

/// Reads to the end, keeping at most `limit` bytes.
fn drain_capped(mut reader: impl std::io::Read, limit: usize) -> Vec<u8> {
    let mut kept = Vec::new();
    let mut buffer = vec![0u8; 8 * 1024];
    loop {
        match reader.read(&mut buffer) {
            Ok(0) => break,
            Ok(read) => {
                let room = limit.saturating_sub(kept.len());
                kept.extend_from_slice(&buffer[..read.min(room)]);
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
    kept
}

/// Spawns one Git child once its `admission` slots allow it, and cleans up
/// its whole process group.
async fn run_git(
    working_directory: &Path,
    repository: Option<&[String]>,
    admission: Admission<'_>,
    configuration: &[String],
    command: &GitCommand,
    stdin: Option<Vec<u8>>,
    limits: CallLimits,
) -> Result<GitOutput, GitError> {
    use std::os::unix::process::CommandExt;

    if Instant::now() >= limits.deadline {
        return Err(GitError::Timeout);
    }
    // Released after the guard below has cleaned up the child.
    let _permits = acquire_slots(admission, limits.deadline).await?;

    let (program, leading_arguments) = git_program();
    let mut process = std::process::Command::new(program);
    process.args(leading_arguments);
    process.env_clear();
    for name in INHERITED_VARIABLES {
        if let Some(value) = std::env::var_os(name) {
            process.env(name, value);
        }
    }
    for (name, value) in FIXED_VARIABLES {
        process.env(name, value);
    }
    for setting in FIXED_CONFIGURATION
        .iter()
        .copied()
        .map(str::to_string)
        .chain(configuration.iter().cloned())
    {
        process.arg("-c").arg(setting);
    }
    if let Some(repository) = repository {
        process.args(repository);
    }
    process
        .args(command.arguments())
        .current_dir(working_directory)
        .stdin(if stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    // Read before spawning: the child can be running before spawn() returns.
    let started = Instant::now();
    let mut child = process.spawn().map_err(|_| GitError::Unavailable)?;
    let process_id = child.id();
    let input = child.stdin.take();
    let stdout = child.stdout.take().expect("stdout is piped");
    let stderr = child.stderr.take().expect("stderr is piped");
    // Dropping the guard on any early return, timeout, or cancellation
    // kills the group.
    let (guard, exited) = ChildGuard::watch(child);

    if let (Some(mut input), Some(bytes)) = (input, stdin) {
        // A child that exits early closes its stdin; that isn't an error here.
        std::thread::spawn(move || {
            let _ = std::io::Write::write_all(&mut input, &bytes);
        });
    }
    let stdout_limit = limits.stdout_bytes;
    let reading = tokio::task::spawn_blocking(move || read_capped(stdout, stdout_limit));
    let draining = tokio::task::spawn_blocking(move || drain_capped(stderr, STDERR_BYTES));

    let captured = match tokio::time::timeout_at(limits.deadline, reading).await {
        Ok(Ok(captured)) => captured,
        Ok(Err(_)) => return Err(GitError::Unavailable),
        Err(_) => return Err(GitError::Timeout),
    };
    let (stdout, overflowed) = captured.map_err(|error| GitError::Failed(error.to_string()))?;
    if overflowed {
        return Err(GitError::OutputTooLarge(limits.stdout_bytes));
    }
    // A child can close its output and keep running; the deadline still
    // applies until its exit is observed.
    let finished = async {
        let stderr = draining.await.unwrap_or_default();
        if exited.await.is_err() {
            // No exit will be reported; only the deadline ends the call.
            std::future::pending::<()>().await;
        }
        (stderr, started.elapsed())
    };
    let remaining = limits
        .deadline
        .saturating_duration_since(Instant::now())
        .max(Duration::from_millis(1));
    let (stderr, ran_for) = tokio::time::timeout(remaining, finished)
        .await
        .map_err(|_| GitError::Timeout)?;
    let status = guard.finish().await.ok_or(GitError::Unavailable)?;
    let signal = std::os::unix::process::ExitStatusExt::signal(&status);
    if let Some(signal) = signal {
        // The runner only signals Git after its exit was seen, so this came
        // from outside; the pid and time let endpoint logs name the sender.
        let at_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |since| since.as_millis());
        eprintln!(
            "workspace-helper: git {} (pid {process_id}) was stopped by signal {signal} \
             after {} ms, at unix ms {at_ms}",
            command.name(),
            ran_for.as_millis(),
        );
    }
    Ok(GitOutput {
        stdout,
        success: status.success(),
        exit_code: status.code(),
        signal,
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
        ran_for,
    })
}

#[cfg(test)]
#[path = "git_runner_test.rs"]
mod tests;

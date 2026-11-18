//! What a client needs to find a session's pull request: the checked-out
//! branch, its commit, where it pushes, and the repository's remotes, as
//! host/owner/repo. Git answers through the fixed commands in `git_runner`.
//! Configuration values and remote URLs never leave this module whole: URLs
//! are reduced to host, owner and repository, and anything this module can't
//! model exactly gives no evidence rather than a guess.

use std::path::{Path, PathBuf};

use tokio::time::Instant;

use crate::git_runner::{CallLimits, GitCommand, GitError, GitOutput, GitRepository};

/// Remotes reported at most; more are counted, not listed.
pub const MAXIMUM_REMOTES: usize = 8;
const CONFIG_BYTES: usize = 256 * 1024;
const SMALL_OUTPUT_BYTES: usize = 64 * 1024;

/// A repository on a code host, from a remote URL.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RepositoryIdentity {
    pub host: String,
    pub owner: String,
    pub repo: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Remote {
    pub name: String,
    pub identity: RepositoryIdentity,
}

/// Where the branch pushes: the remote, the branch on it, and that repository.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PushTarget {
    pub remote_name: String,
    /// The destination branch on the remote, without `refs/heads/`.
    pub branch: String,
    pub identity: RepositoryIdentity,
    /// The local tracking ref for the destination, when one exists.
    pub tracking_ref: Option<String>,
    /// That ref's commit, when it describes the push repository.
    pub tracking_sha: Option<String>,
}

/// Why there is no push evidence although the branch may push somewhere.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PushUnresolved {
    /// Refspecs don't give one unambiguous destination, or Git disagrees.
    Refspec,
    /// The push URLs don't name one supported repository.
    PushUrl,
    /// `push.default=matching`, which can push many branches.
    PushDefault,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RepositoryContext {
    /// The checked-out branch, without `refs/heads/`; none when detached.
    pub branch: Option<String>,
    pub head_sha: Option<String>,
    pub push: Option<PushTarget>,
    pub push_unresolved: Option<PushUnresolved>,
    /// The upstream's local tracking ref, for telling contexts apart only,
    /// never as evidence.
    pub upstream_ref: Option<String>,
    pub remotes: Vec<Remote>,
    pub unsupported_remote_count: u32,
    pub truncated_remotes: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ContextProblem {
    NotARepository,
    /// Configuration this module defers: URL rewrites, or remotes and branches
    /// defined outside the repository's own configuration.
    UnsupportedConfig,
    /// The repository changed during both attempts to read it.
    RepositoryChanging,
    Timeout,
    GitUnavailable,
    GitFailed,
}

impl From<GitError> for ContextProblem {
    fn from(error: GitError) -> Self {
        match error {
            GitError::Timeout => Self::Timeout,
            GitError::Unavailable => Self::GitUnavailable,
            _ => Self::GitFailed,
        }
    }
}

// ---------------------------------------------------------------------------
// Remote URLs
// ---------------------------------------------------------------------------

/// The host, owner and repository a remote URL names, or `None` for any form
/// this module doesn't support: other schemes, local paths, non-default ports,
/// or paths that aren't exactly `owner/repo`.
pub fn sanitize_remote_url(url: &str) -> Option<RepositoryIdentity> {
    let url = url.trim();
    if url.is_empty()
        || url
            .chars()
            .any(|character| character.is_control() || character == ' ')
    {
        return None;
    }
    let (authority, path) = if let Some(rest) = url.strip_prefix("https://") {
        let (authority, path) = rest.split_once('/')?;
        (host_of_authority(authority, "443")?, path)
    } else if let Some(rest) = url.strip_prefix("ssh://") {
        let (authority, path) = rest.split_once('/')?;
        (host_of_authority(authority, "22")?, path)
    } else if url.contains("://") {
        return None;
    } else {
        // scp-like `[user@]host:owner/repo`; a `/` before the `:` makes it a path.
        let (authority, path) = url.split_once(':')?;
        if authority.contains('/') || path.starts_with('/') || path.starts_with("//") {
            return None;
        }
        let host = authority
            .rsplit_once('@')
            .map_or(authority, |(_, host)| host);
        (valid_host(host)?, path)
    };
    // Query strings and fragments are dropped, never echoed.
    let path = path.split(['?', '#']).next().unwrap_or_default();
    let path = path.strip_suffix('/').unwrap_or(path);
    let mut segments = path.split('/');
    let (Some(owner), Some(repo), None) = (segments.next(), segments.next(), segments.next())
    else {
        return None;
    };
    let repo = repo.strip_suffix(".git").unwrap_or(repo);
    if !valid_segment(owner) || !valid_segment(repo) {
        return None;
    }
    Some(RepositoryIdentity {
        host: authority,
        owner: owner.to_string(),
        repo: repo.to_string(),
    })
}

/// The lowercased host of `[userinfo@]host[:port]`, when the port is absent or
/// the scheme's default; any other port names a different service.
fn host_of_authority(authority: &str, default_port: &str) -> Option<String> {
    let host_port = authority
        .rsplit_once('@')
        .map_or(authority, |(_, rest)| rest);
    let host = match host_port.rsplit_once(':') {
        Some((host, port)) if port == default_port => host,
        Some(_) => return None,
        None => host_port,
    };
    valid_host(host)
}

fn valid_host(host: &str) -> Option<String> {
    let valid = !host.is_empty()
        && host.len() <= 253
        && host
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'.')
        && !host.starts_with(['-', '.'])
        && !host.ends_with('-');
    valid.then(|| host.trim_end_matches('.').to_ascii_lowercase())
}

fn valid_segment(segment: &str) -> bool {
    !segment.is_empty()
        && segment.len() <= 100
        && segment != "."
        && segment != ".."
        && segment
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
}

// ---------------------------------------------------------------------------
// Configuration snapshot
// ---------------------------------------------------------------------------

/// One configuration entry. `section` and `variable` are lowercase, as Git
/// prints them; `subsection` keeps its case.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigEntry {
    pub origin: String,
    pub section: String,
    pub subsection: Option<String>,
    pub variable: String,
    pub value: Option<String>,
}

/// The `remote.*`, `branch.*`, `url.*` and `push.*` configuration, read once.
#[derive(Clone, Debug, Default)]
pub struct ConfigSnapshot {
    entries: Vec<ConfigEntry>,
}

impl ConfigSnapshot {
    /// Parses `config -z --show-origin` output: `origin\0key[\nvalue]\0` records.
    pub fn parse(output: &[u8]) -> Option<Self> {
        let text = std::str::from_utf8(output).ok()?;
        let mut fields = text.split('\0');
        let mut entries = Vec::new();
        while let Some(origin) = fields.next() {
            if origin.is_empty() {
                break;
            }
            let record = fields.next()?;
            let (key, value) = match record.split_once('\n') {
                Some((key, value)) => (key, Some(value.to_string())),
                None => (record, None),
            };
            let (section, rest) = key.split_once('.')?;
            let (subsection, variable) = match rest.rsplit_once('.') {
                Some((subsection, variable)) => (Some(subsection.to_string()), variable),
                None => (None, rest),
            };
            entries.push(ConfigEntry {
                origin: origin.to_string(),
                section: section.to_ascii_lowercase(),
                subsection,
                variable: variable.to_ascii_lowercase(),
                value,
            });
        }
        Some(Self { entries })
    }

    fn values(&self, section: &str, subsection: Option<&str>, variable: &str) -> Vec<&str> {
        self.entries
            .iter()
            .filter(|entry| {
                entry.section == section
                    && entry.subsection.as_deref() == subsection
                    && entry.variable == variable
            })
            .filter_map(|entry| entry.value.as_deref())
            .collect()
    }

    /// The last value, which is the one Git uses for single-valued keys.
    fn last(&self, section: &str, subsection: Option<&str>, variable: &str) -> Option<&str> {
        self.values(section, subsection, variable).pop()
    }

    /// Remote names with a URL, in configuration order.
    fn remote_names(&self) -> Vec<&str> {
        let mut names: Vec<&str> = Vec::new();
        for entry in &self.entries {
            if entry.section == "remote"
                && entry.variable == "url"
                && let Some(name) = entry.subsection.as_deref()
                && !names.contains(&name)
            {
                names.push(name);
            }
        }
        names
    }

    /// Whether this configuration is outside what this module models exactly:
    /// any URL rewrite, or a `remote.*`/`branch.*` key from a file other than the
    /// repository's own configuration. `remote.pushDefault` may come from
    /// anywhere: it only names a remote, which must be the repository's own.
    pub fn is_deferred(&self, repository_files: &[PathBuf], working_directory: &Path) -> bool {
        self.entries.iter().any(|entry| {
            if entry.section == "url"
                && matches!(entry.variable.as_str(), "insteadof" | "pushinsteadof")
            {
                return true;
            }
            if entry.section == "remote" && entry.subsection.is_none() {
                return false;
            }
            matches!(entry.section.as_str(), "remote" | "branch")
                && !origin_is_one_of(&entry.origin, repository_files, working_directory)
        })
    }
}

fn origin_is_one_of(origin: &str, files: &[PathBuf], working_directory: &Path) -> bool {
    let Some(path) = origin.strip_prefix("file:") else {
        return false;
    };
    let path = Path::new(path);
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        working_directory.join(path)
    };
    let path = std::fs::canonicalize(&path).unwrap_or(path);
    files.contains(&path)
}

// ---------------------------------------------------------------------------
// Refspecs
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct Refspec<'a> {
    negative: bool,
    source: &'a str,
    destination: Option<&'a str>,
}

fn parse_refspec(text: &str) -> Refspec<'_> {
    let text = text.strip_prefix('+').unwrap_or(text);
    if let Some(negative) = text.strip_prefix('^') {
        return Refspec {
            negative: true,
            source: negative,
            destination: None,
        };
    }
    match text.split_once(':') {
        Some((source, destination)) => Refspec {
            negative: false,
            source,
            destination: Some(destination),
        },
        None => Refspec {
            negative: false,
            source: text,
            destination: None,
        },
    }
}

/// `name` matched against `pattern`, which has at most one `*`; the part the
/// `*` stood for, or the empty string for a literal match.
fn match_pattern<'a>(pattern: &str, name: &'a str) -> Option<&'a str> {
    match pattern.split_once('*') {
        None => (pattern == name).then_some(""),
        Some((prefix, suffix)) => {
            if suffix.contains('*') {
                return None;
            }
            let middle = name.strip_prefix(prefix)?.strip_suffix(suffix)?;
            (!middle.is_empty()).then_some(middle)
        }
    }
}

fn substitute(pattern: &str, middle: &str) -> String {
    pattern.replacen('*', middle, 1)
}

/// Where the push refspecs send `refs/heads/<branch>`: `Ok(None)` when no
/// refspec pushes it, an error when more than one does or any can't be modeled.
/// Only full `refs/heads/` refspecs are modeled; Git itself doesn't report a
/// push target for short or `HEAD` sources, so those withhold evidence.
fn push_refspec_destination(refspecs: &[&str], branch: &str) -> Result<Option<String>, ()> {
    let branch_ref = format!("refs/heads/{branch}");
    let mut found = None;
    for text in refspecs {
        let refspec = parse_refspec(text);
        if refspec.negative {
            return Err(());
        }
        let source = refspec.source;
        // A short source (`feature`, `heads/feature`, `HEAD`) may name this
        // branch in a way Git doesn't report, so any one withholds evidence.
        if !source.starts_with("refs/") {
            return Err(());
        }
        let Some(middle) = match_pattern(source, &branch_ref) else {
            continue;
        };
        let destination = match refspec.destination {
            // `src` alone pushes to the same name.
            None => branch_ref.clone(),
            Some(destination) if destination.starts_with("refs/heads/") => {
                substitute(destination, middle)
            }
            // Deletions, other namespaces and short names aren't modeled.
            Some(_) => return Err(()),
        };
        if found.replace(destination).is_some() {
            return Err(());
        }
    }
    Ok(found)
}

/// The local tracking ref the fetch refspecs give a remote branch:
/// `Ok(None)` when none maps it, an error when several do or one is negative.
fn fetch_tracking_ref(refspecs: &[&str], remote_branch_ref: &str) -> Result<Option<String>, ()> {
    let mut found = None;
    for text in refspecs {
        let refspec = parse_refspec(text);
        if refspec.negative {
            return Err(());
        }
        let Some(destination) = refspec
            .destination
            .filter(|destination| !destination.is_empty())
        else {
            continue;
        };
        let Some(middle) = match_pattern(refspec.source, remote_branch_ref) else {
            continue;
        };
        if found.replace(substitute(destination, middle)).is_some() {
            return Err(());
        }
    }
    Ok(found)
}

// ---------------------------------------------------------------------------
// Push resolution
// ---------------------------------------------------------------------------

/// What Git itself reports for the branch's push.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct GitPushFields {
    /// `%(push)`: the local tracking ref Git would update, if any.
    pub push: Option<String>,
    pub push_remote: Option<String>,
    pub upstream: Option<String>,
}

impl GitPushFields {
    fn parse(output: &[u8]) -> Self {
        let text = String::from_utf8_lossy(output);
        let mut fields = text.trim_end_matches('\n').split('\0');
        let mut next = || {
            fields
                .next()
                .filter(|field| !field.is_empty())
                .map(str::to_string)
        };
        Self {
            push: next(),
            push_remote: next(),
            upstream: next(),
        }
    }
}

/// A push destination before its tracking ref's commit is read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PushResolution {
    pub remote_name: String,
    pub branch: String,
    pub identity: RepositoryIdentity,
    pub tracking_ref: Option<String>,
    /// The push repository is the one the remote fetches from.
    pub fetches_from_push_repository: bool,
}

/// Where `branch` pushes, from the configuration and Git's own report, by Git's
/// documented rules. `Ok(None)` means it doesn't push, or does so in a way this
/// module doesn't model; an error names why evidence was withheld.
pub fn resolve_push(
    config: &ConfigSnapshot,
    branch: &str,
    git: &GitPushFields,
) -> Result<Option<PushResolution>, PushUnresolved> {
    let remotes = config.remote_names();
    let sole_remote = (remotes.len() == 1).then(|| remotes[0]);
    let branch_remote = config.last("branch", Some(branch), "remote");
    let fetch_remote = branch_remote.or(sole_remote).unwrap_or("origin");
    let push_remote = config
        .last("branch", Some(branch), "pushremote")
        .or_else(|| config.last("remote", None, "pushdefault"))
        .unwrap_or(fetch_remote);
    if !remotes.contains(&push_remote) {
        return Ok(None);
    }
    if let Some(reported) = &git.push_remote
        && reported != push_remote
    {
        return Err(PushUnresolved::Refspec);
    }

    let merges: Vec<&str> = config.values("branch", Some(branch), "merge");
    let single_upstream = (merges.len() == 1).then(|| merges[0]);
    let push_refspecs: Vec<&str> = config.values("remote", Some(push_remote), "push");
    let destination_ref = if !push_refspecs.is_empty() {
        match push_refspec_destination(&push_refspecs, branch) {
            Ok(Some(destination)) => destination,
            Ok(None) => return Ok(None),
            Err(()) => return Err(PushUnresolved::Refspec),
        }
    } else {
        let branch_ref = format!("refs/heads/{branch}");
        let triangular = push_remote != fetch_remote;
        let mode = config
            .last("push", None, "default")
            .unwrap_or("simple")
            .to_ascii_lowercase();
        let auto_setup = config
            .last("push", None, "autosetupremote")
            .is_some_and(|value| {
                matches!(
                    value.to_ascii_lowercase().as_str(),
                    "true" | "yes" | "on" | "1"
                )
            });
        if auto_setup && merges.is_empty() {
            return Ok(None);
        }
        match mode.as_str() {
            "current" => branch_ref,
            "simple" if triangular => branch_ref,
            // Central: Git needs a configured upstream on this remote, named the same.
            "simple" => match (single_upstream, branch_remote) {
                (Some(upstream), Some(remote))
                    if upstream == branch_ref && remote == push_remote =>
                {
                    branch_ref
                }
                _ => return Ok(None),
            },
            "upstream" | "tracking" => match (single_upstream, branch_remote) {
                (Some(upstream), Some(remote))
                    if remote == push_remote && upstream.starts_with("refs/heads/") =>
                {
                    upstream.to_string()
                }
                _ => return Ok(None),
            },
            "matching" => return Err(PushUnresolved::PushDefault),
            _ => return Ok(None),
        }
    };
    let Some(destination) = destination_ref.strip_prefix("refs/heads/") else {
        return Err(PushUnresolved::Refspec);
    };

    // Git maps the destination to a local ref through the fetch refspecs; ours
    // must agree whenever Git reports one. Git reports none for a triangular
    // `simple` push, which still pushes to the same name.
    let fetch_refspecs: Vec<&str> = config.values("remote", Some(push_remote), "fetch");
    let tracking_ref = fetch_tracking_ref(&fetch_refspecs, &destination_ref)
        .map_err(|()| PushUnresolved::Refspec)?;
    if git.push.is_some() && tracking_ref != git.push {
        return Err(PushUnresolved::Refspec);
    }

    let push_urls: Vec<&str> = {
        let push_urls: Vec<&str> = config.values("remote", Some(push_remote), "pushurl");
        if push_urls.is_empty() {
            config.values("remote", Some(push_remote), "url")
        } else {
            push_urls
        }
    };
    let identity = single_identity(&push_urls).ok_or(PushUnresolved::PushUrl)?;
    let fetch_urls: Vec<&str> = config.values("remote", Some(push_remote), "url");
    let fetches_from_push_repository = single_identity(&fetch_urls).as_ref() == Some(&identity);
    Ok(Some(PushResolution {
        remote_name: push_remote.to_string(),
        branch: destination.to_string(),
        identity,
        tracking_ref,
        fetches_from_push_repository,
    }))
}

/// The one repository every URL names, or `None` when they differ or any is unsupported.
fn single_identity(urls: &[&str]) -> Option<RepositoryIdentity> {
    let mut identities = urls.iter().map(|url| sanitize_remote_url(url));
    let first = identities.next()??;
    identities
        .all(|identity| identity.as_ref() == Some(&first))
        .then_some(first)
}

/// The remotes to report, with how many were left out as unsupported, and
/// whether there were more than [`MAXIMUM_REMOTES`].
pub fn sanitized_remotes(config: &ConfigSnapshot) -> (Vec<Remote>, u32, bool) {
    let names = config.remote_names();
    let truncated = names.len() > MAXIMUM_REMOTES;
    let mut remotes = Vec::new();
    let mut unsupported = 0;
    for name in names.into_iter().take(MAXIMUM_REMOTES) {
        let urls: Vec<&str> = config.values("remote", Some(name), "url");
        // A remote with several fetch URLs fetches only the first; it's ambiguous here.
        match (urls.len() == 1)
            .then(|| sanitize_remote_url(urls[0]))
            .flatten()
        {
            Some(identity) => remotes.push(Remote {
                name: name.to_string(),
                identity,
            }),
            None => unsupported += 1,
        }
    }
    (remotes, unsupported, truncated)
}

// ---------------------------------------------------------------------------
// Reading the repository
// ---------------------------------------------------------------------------

/// Everything one read saw, compared with a second read to catch changes.
#[derive(Debug, PartialEq, Eq)]
struct Observation {
    config: Vec<u8>,
    head_ref: Option<String>,
    head_sha: Option<String>,
    push_fields: Option<GitPushFields>,
}

fn limits(deadline: Instant, stdout_bytes: usize) -> CallLimits {
    CallLimits {
        deadline,
        stdout_bytes,
    }
}

fn first_line(output: &GitOutput) -> Option<String> {
    let text = String::from_utf8_lossy(&output.stdout);
    let line = text.lines().next().unwrap_or_default().trim();
    (!line.is_empty()).then(|| line.to_string())
}

/// Runs a command whose "nothing there" answer is exit status 1.
async fn run_allowing_absent(
    repository: &GitRepository,
    command: &GitCommand,
    deadline: Instant,
    stdout_bytes: usize,
) -> Result<Option<GitOutput>, ContextProblem> {
    let output = repository
        .run_unchecked(command, None, limits(deadline, stdout_bytes))
        .await?;
    if output.success {
        Ok(Some(output))
    } else if output.exit_code == Some(1) && output.signal.is_none() {
        Ok(None)
    } else {
        Err(ContextProblem::GitFailed)
    }
}

async fn observe(
    repository: &GitRepository,
    deadline: Instant,
) -> Result<Observation, ContextProblem> {
    let config = run_allowing_absent(
        repository,
        &GitCommand::ConfigSnapshot,
        deadline,
        CONFIG_BYTES,
    )
    .await?
    .map(|output| output.stdout)
    .unwrap_or_default();
    let head_ref = run_allowing_absent(
        repository,
        &GitCommand::SymbolicRefHead,
        deadline,
        SMALL_OUTPUT_BYTES,
    )
    .await?
    .as_ref()
    .and_then(first_line);
    let head_sha = run_allowing_absent(
        repository,
        &GitCommand::RefCommit {
            reference: "HEAD".into(),
        },
        deadline,
        SMALL_OUTPUT_BYTES,
    )
    .await?
    .as_ref()
    .and_then(first_line)
    .filter(|sha| crate::git_runner::is_object_name(sha));
    let push_fields = match head_ref.as_deref() {
        Some(branch_ref) if branch_ref.starts_with("refs/heads/") => {
            let output = repository
                .run(
                    &GitCommand::BranchPush {
                        branch_ref: branch_ref.to_string(),
                    },
                    None,
                    limits(deadline, SMALL_OUTPUT_BYTES),
                )
                .await?;
            Some(GitPushFields::parse(&output))
        }
        _ => None,
    };
    Ok(Observation {
        config,
        head_ref,
        head_sha,
        push_fields,
    })
}

/// The files whose `remote.*` and `branch.*` keys this module trusts.
fn repository_files(repository: &GitRepository) -> Vec<PathBuf> {
    let canonical = |path: PathBuf| std::fs::canonicalize(&path).unwrap_or(path);
    vec![
        canonical(repository.common_directory.join("config")),
        canonical(repository.git_directory.join("config.worktree")),
    ]
}

/// The context from one observation; the tracking commit is read separately.
fn context_from(
    repository: &GitRepository,
    observation: &Observation,
) -> Result<(RepositoryContext, Option<PushResolution>), ContextProblem> {
    let config = ConfigSnapshot::parse(&observation.config).ok_or(ContextProblem::GitFailed)?;
    if config.is_deferred(&repository_files(repository), &repository.working_directory) {
        return Err(ContextProblem::UnsupportedConfig);
    }
    let branch = observation
        .head_ref
        .as_deref()
        .and_then(|reference| reference.strip_prefix("refs/heads/"))
        .map(str::to_string);
    let (remotes, unsupported_remote_count, truncated_remotes) = sanitized_remotes(&config);
    let mut context = RepositoryContext {
        branch: branch.clone(),
        head_sha: observation.head_sha.clone(),
        remotes,
        unsupported_remote_count,
        truncated_remotes,
        ..RepositoryContext::default()
    };
    let mut resolution = None;
    if let (Some(branch), Some(fields)) = (&branch, &observation.push_fields) {
        context.upstream_ref = fields.upstream.clone();
        match resolve_push(&config, branch, fields) {
            Ok(found) => resolution = found,
            Err(unresolved) => context.push_unresolved = Some(unresolved),
        }
    }
    Ok((context, resolution))
}

/// Reads the repository context of the repository containing `working_directory`.
pub async fn resolve(
    working_directory: &Path,
    deadline: Instant,
) -> Result<RepositoryContext, ContextProblem> {
    let repository = GitRepository::discover(working_directory, deadline)
        .await?
        .ok_or(ContextProblem::NotARepository)?;
    // A read that saw the repository change is retried once, then declined.
    for _ in 0..2 {
        let before = observe(&repository, deadline).await?;
        let (mut context, resolution) = context_from(&repository, &before)?;
        let tracking_sha = match resolution
            .as_ref()
            .and_then(|found| found.tracking_ref.clone())
        {
            Some(reference) => run_allowing_absent(
                &repository,
                &GitCommand::RefCommit { reference },
                deadline,
                SMALL_OUTPUT_BYTES,
            )
            .await?
            .as_ref()
            .and_then(first_line)
            .filter(|sha| crate::git_runner::is_object_name(sha)),
            None => None,
        };
        let after = observe(&repository, deadline).await?;
        if after != before {
            continue;
        }
        context.push = resolution.map(|found| PushTarget {
            // The tracking ref describes the fetch side, which may be another repository.
            tracking_sha: tracking_sha.filter(|_| found.fetches_from_push_repository),
            remote_name: found.remote_name,
            branch: found.branch,
            identity: found.identity,
            tracking_ref: found.tracking_ref,
        });
        return Ok(context);
    }
    Err(ContextProblem::RepositoryChanging)
}

#[cfg(test)]
#[path = "repository_context_test.rs"]
mod tests;

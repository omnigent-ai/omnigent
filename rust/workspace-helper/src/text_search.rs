//! Text search with ripgrep's libraries, producing exactly what the runner's
//! `rg`-based content search returns: the first match of each matching line,
//! in a 400-character preview window with UTF-16 offsets.

use std::{
    collections::HashSet,
    io::{self, Read},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};

use grep_matcher::Matcher;
use grep_regex::{RegexMatcher, RegexMatcherBuilder};
use grep_searcher::{BinaryDetection, SearcherBuilder, sinks::Bytes};
use ignore::{WalkState, overrides::OverrideBuilder};

use crate::{
    globs::split_glob_list,
    walk::{IgnoreMode, WalkOptions, walk_parallel},
};

pub const DEFAULT_LIMIT: usize = 500;
pub const TIME_LIMIT: Duration = Duration::from_secs(15);
/// Lines longer than this are left out, as the runner leaves them out.
const MAXIMUM_LINE_BYTES: usize = 1024 * 1024;
const PREVIEW_BEFORE: usize = 80;
const PREVIEW_LENGTH: usize = 400;

pub const LONG_LINES_WARNING: &str = "Some very long matching lines were omitted.";
pub const TIMEOUT_WARNING: &str =
    "Search stopped after 15 seconds. Narrow your query and try again.";
pub const UNREADABLE_WARNING: &str = "Some files or ignore rules could not be read. Results may be incomplete or include normally ignored files.";

#[derive(Clone, Debug, Default)]
pub struct TextQuery {
    pub query: String,
    pub case_sensitive: bool,
    pub regular_expression: bool,
    pub whole_word: bool,
    pub include: String,
    pub exclude: String,
    pub limit: usize,
    pub include_ignored: bool,
    /// Workspace-relative directories to search instead of the whole root.
    pub include_directories: Vec<String>,
    /// Workspace-relative directories whose subtrees are skipped.
    pub exclude_directories: Vec<String>,
}

/// The walk's starting points and pruned directories for directory scopes.
/// Starts are disjoint, so no file is searched twice: duplicates and starts
/// beneath another start are dropped, as is a start inside an excluded
/// directory.
pub fn directory_scopes(
    root: &Path,
    include_directories: &[String],
    exclude_directories: &[String],
) -> (Vec<PathBuf>, HashSet<PathBuf>) {
    let excluded: HashSet<PathBuf> = exclude_directories
        .iter()
        .map(|directory| root.join(directory))
        .collect();
    let mut starts: Vec<PathBuf> = if include_directories.is_empty() {
        vec![root.to_path_buf()]
    } else {
        include_directories
            .iter()
            .map(|directory| root.join(directory))
            .collect()
    };
    // Sorted, an ancestor comes before everything beneath it.
    starts.sort();
    let mut disjoint: Vec<PathBuf> = Vec::new();
    for start in starts {
        let covered = disjoint.last().is_some_and(|kept| start.starts_with(kept));
        let excluded_start = start
            .ancestors()
            .any(|ancestor| excluded.contains(ancestor));
        if !covered && !excluded_start {
            disjoint.push(start);
        }
    }
    (disjoint, excluded)
}

#[derive(Clone, Debug, PartialEq)]
pub struct TextMatch {
    pub path: String,
    pub line: u64,
    pub column: u32,
    pub text: String,
    pub match_start: u32,
    pub match_end: u32,
}

#[derive(Debug, Default, PartialEq)]
pub struct TextSearchSummary {
    pub truncated: bool,
    pub warning: Option<String>,
}

fn utf16_length(text: &str) -> u32 {
    text.encode_utf16().count() as u32
}

/// The runner's preview of one line: a window that starts up to 80
/// characters before the match and spans at most 400, with "…" at cuts.
/// Offsets are byte offsets of the match within `raw`.
pub fn preview(raw: &[u8], start: usize, end: usize) -> (u32, String, u32, u32) {
    let line_text = String::from_utf8_lossy(raw);
    let line: Vec<char> = line_text.trim_end_matches(['\r', '\n']).chars().collect();
    let start_chars = String::from_utf8_lossy(&raw[..start]).chars().count();
    let end_chars = String::from_utf8_lossy(&raw[..end]).chars().count();
    let left = start_chars.saturating_sub(PREVIEW_BEFORE);
    let right = line.len().min(left + PREVIEW_LENGTH);
    let prefix = if left > 0 { "…" } else { "" };
    let slice = |from: usize, to: usize| -> String {
        let to = to.min(line.len());
        if from >= to {
            String::new()
        } else {
            line[from..to].iter().collect()
        }
    };
    let window = slice(left, right);
    let text = format!(
        "{prefix}{window}{}",
        if right < line.len() { "…" } else { "" }
    );
    let column = utf16_length(&slice(0, start_chars)) + 1;
    let match_start = utf16_length(prefix) + utf16_length(&slice(left, start_chars));
    let match_end = utf16_length(prefix) + utf16_length(&slice(left, end_chars.min(right)));
    (column, text, match_start, match_end)
}

/// Rejects queries the runner rejects, with the runner's messages.
pub fn validate(query: &TextQuery) -> Result<(), String> {
    let text = &query.query;
    if text.trim().is_empty() || text.chars().count() > 4096 || text.contains(['\0', '\r', '\n']) {
        return Err("Enter a single-line search query of 1–4096 characters.".into());
    }
    if !(1..=DEFAULT_LIMIT).contains(&query.limit) {
        return Err("Search limit must be between 1 and 500.".into());
    }
    for filter in [&query.include, &query.exclude] {
        if filter.chars().count() > 4096 || filter.contains(['\0', '\r', '\n']) {
            return Err("Enter single-line file filters of at most 4096 characters.".into());
        }
        if split_glob_list(filter)
            .iter()
            .any(|pattern| pattern.starts_with('!'))
        {
            return Err("Use the Include and Exclude fields instead of ! prefixes.".into());
        }
    }
    Ok(())
}

fn matcher(query: &TextQuery) -> Result<RegexMatcher, String> {
    RegexMatcherBuilder::new()
        .case_insensitive(!query.case_sensitive)
        .fixed_strings(!query.regular_expression)
        .word(query.whole_word)
        .line_terminator(Some(b'\n'))
        .build(&query.query)
        .map_err(|error| format!("The search pattern is not valid: {error}"))
}

fn overrides(root: &Path, query: &TextQuery) -> Result<ignore::overrides::Override, String> {
    let mut builder = OverrideBuilder::new(root);
    for (filter, negate) in [(&query.include, false), (&query.exclude, true)] {
        for mut pattern in split_glob_list(filter) {
            if pattern.ends_with('/') {
                pattern.push_str("**");
            }
            let glob = if negate {
                format!("!{pattern}")
            } else {
                pattern
            };
            builder
                .add(&glob)
                .map_err(|error| format!("The file filter is not valid: {error}"))?;
        }
    }
    builder
        .build()
        .map_err(|error| format!("The file filter is not valid: {error}"))
}

/// What a priority search looks at before the rest of its scope.
#[derive(Clone, Debug, Default)]
pub struct Priorities {
    /// Absolute files beneath the root, resolved without symlinks, the most
    /// important first.
    pub focused_files: Vec<PathBuf>,
}

/// At most this many focused files are searched first.
pub const MAXIMUM_FOCUSED_FILES: usize = 16;
/// At most this many changed directories are searched before the rest.
pub const MAXIMUM_CHANGED_DIRECTORIES: usize = 32;
/// Matches are sent at most this many at a time...
pub const BATCH_SIZE: usize = 64;
/// ...and at most this long after the oldest one waiting was found.
pub const BATCH_INTERVAL: Duration = Duration::from_millis(40);

/// The error a file read ends with once its search has stopped.
const STOPPED_READING: &str = "the search stopped";

/// A file read that ends as soon as its search stops, whether or not the
/// file has matched, so a long file without matches can't outlast a cancel.
struct Interruptible<'r, R> {
    inner: R,
    stopped: &'r dyn Fn() -> bool,
}

impl<R: Read> Read for Interruptible<'_, R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if (self.stopped)() {
            return Err(io::Error::other(STOPPED_READING));
        }
        self.inner.read(buffer)
    }
}

/// When a search runs out of time.
pub struct Deadline {
    at: Instant,
    /// Set by a test to end the search when it chooses, whatever the clock says.
    #[cfg(test)]
    expired: Option<Arc<AtomicBool>>,
}

impl Deadline {
    pub fn at(at: Instant) -> Self {
        Self {
            at,
            #[cfg(test)]
            expired: None,
        }
    }

    /// A deadline that passes only once `expired` is set.
    #[cfg(test)]
    fn manual(expired: Arc<AtomicBool>) -> Self {
        Self {
            at: Instant::now(),
            expired: Some(expired),
        }
    }

    fn passed(&self) -> bool {
        #[cfg(test)]
        if let Some(expired) = &self.expired {
            return expired.load(Ordering::Relaxed);
        }
        Instant::now() >= self.at
    }
}

/// One search's shared state: its cap, deadline, cancel, and what it found.
struct Run<'a> {
    root: &'a Path,
    query: &'a TextQuery,
    matcher: RegexMatcher,
    overrides: ignore::overrides::Override,
    mode: IgnoreMode,
    global_excludes: Option<&'a Path>,
    deadline: Deadline,
    cancelled: &'a AtomicBool,
    found: AtomicUsize,
    truncated: AtomicBool,
    timed_out: AtomicBool,
    long_lines: AtomicBool,
    unreadable: AtomicBool,
    emit: &'a (dyn Fn(TextMatch) + Sync),
}

impl<'a> Run<'a> {
    fn new(
        root: &'a Path,
        query: &'a TextQuery,
        global_excludes: Option<&'a Path>,
        cancelled: &'a AtomicBool,
        emit: &'a (dyn Fn(TextMatch) + Sync),
        deadline: Deadline,
    ) -> Result<Self, String> {
        validate(query)?;
        Ok(Self {
            root,
            query,
            matcher: matcher(query)?,
            overrides: overrides(root, query)?,
            mode: if query.include_ignored {
                IgnoreMode::None
            } else {
                IgnoreMode::Ripgrep
            },
            global_excludes,
            deadline,
            cancelled,
            found: AtomicUsize::new(0),
            truncated: AtomicBool::new(false),
            timed_out: AtomicBool::new(false),
            long_lines: AtomicBool::new(false),
            unreadable: AtomicBool::new(false),
            emit,
        })
    }

    /// Whether the search should end: cancelled, at its cap, or out of time.
    fn stopped(&self) -> bool {
        if self.cancelled.load(Ordering::Relaxed)
            || self.truncated.load(Ordering::Relaxed)
            || self.timed_out.load(Ordering::Relaxed)
        {
            return true;
        }
        if self.deadline.passed() {
            self.timed_out.store(true, Ordering::Relaxed);
            return true;
        }
        false
    }

    fn walk_options(
        &self,
        excluded: HashSet<PathBuf>,
        only_paths: Option<HashSet<PathBuf>>,
    ) -> WalkOptions {
        WalkOptions {
            overrides: Some(self.overrides.clone()),
            excluded_directories: Arc::new(excluded),
            only_paths: only_paths.map(Arc::new),
            ..WalkOptions::new(self.mode, self.global_excludes)
        }
    }

    fn searcher() -> grep_searcher::Searcher {
        SearcherBuilder::new()
            .line_number(true)
            .binary_detection(BinaryDetection::quit(0))
            .build()
    }

    /// Searches one file, sending each matching line as soon as it's found.
    fn search_file(&self, searcher: &mut grep_searcher::Searcher, path: &Path) {
        let relative = path
            .strip_prefix(self.root)
            .unwrap_or(path)
            .to_string_lossy()
            .into_owned();
        match std::fs::File::open(path) {
            Ok(file) => self.search_reader(searcher, &relative, file),
            Err(_) => self.unreadable.store(true, Ordering::Relaxed),
        }
    }

    /// Searches `reader` as the file at `relative`, stopping mid-read when the search stops.
    fn search_reader(
        &self,
        searcher: &mut grep_searcher::Searcher,
        relative: &str,
        reader: impl Read,
    ) {
        let stopped = || self.stopped();
        let reader = Interruptible {
            inner: reader,
            stopped: &stopped,
        };
        let result = searcher.search_reader(
            &self.matcher,
            reader,
            Bytes(|line_number, line| {
                if self.stopped() {
                    return Ok(false);
                }
                if line.len() > MAXIMUM_LINE_BYTES {
                    self.long_lines.store(true, Ordering::Relaxed);
                    return Ok(true);
                }
                let Ok(Some(first)) = self.matcher.find(line) else {
                    return Ok(true);
                };
                if self.found.fetch_add(1, Ordering::Relaxed) >= self.query.limit {
                    self.truncated.store(true, Ordering::Relaxed);
                    return Ok(false);
                }
                let (column, text, match_start, match_end) =
                    preview(line, first.start(), first.end());
                (self.emit)(TextMatch {
                    path: relative.to_string(),
                    line: line_number,
                    column,
                    text,
                    match_start,
                    match_end,
                });
                Ok(true)
            }),
        );
        // A read the search itself ended isn't a file it couldn't read.
        if result.is_err() && !self.stopped() {
            self.unreadable.store(true, Ordering::Relaxed);
        }
    }

    /// Walks `starts` in parallel, searching every file not in `skipped`.
    fn walk(&self, starts: &[PathBuf], excluded: HashSet<PathBuf>, skipped: &HashSet<PathBuf>) {
        if starts.is_empty() || self.stopped() {
            return;
        }
        walk_parallel(starts, &self.walk_options(excluded, None), || {
            let mut searcher = Self::searcher();
            Box::new(move |entry| {
                if self.stopped() {
                    return WalkState::Quit;
                }
                let entry = match entry {
                    Ok(entry) => entry,
                    Err(_) => {
                        self.unreadable.store(true, Ordering::Relaxed);
                        return WalkState::Continue;
                    }
                };
                if !entry.file_type().is_some_and(|kind| kind.is_file())
                    || skipped.contains(entry.path())
                {
                    return WalkState::Continue;
                }
                self.search_file(&mut searcher, entry.path());
                if self.stopped() {
                    WalkState::Quit
                } else {
                    WalkState::Continue
                }
            })
        });
    }

    /// Which of `targets` a full walk of the scope would reach: files it
    /// would search, or directories it would enter. The walk starts where a
    /// full one does but enters only the targets' ancestors, so every ignore
    /// rule, filter, and include override applies exactly as in that walk.
    fn reachable(
        &self,
        targets: &[PathBuf],
        directories: bool,
        starts: &[PathBuf],
        excluded: &HashSet<PathBuf>,
    ) -> HashSet<PathBuf> {
        let reached = Mutex::new(HashSet::new());
        if targets.is_empty() || self.stopped() {
            return HashSet::new();
        }
        let wanted: HashSet<&Path> = targets.iter().map(PathBuf::as_path).collect();
        let mut along: HashSet<PathBuf> = HashSet::new();
        for target in targets {
            for ancestor in target.ancestors() {
                if ancestor == self.root || !along.insert(ancestor.to_path_buf()) {
                    break;
                }
            }
        }
        walk_parallel(
            starts,
            &self.walk_options(excluded.clone(), Some(along)),
            || {
                let (reached, wanted) = (&reached, &wanted);
                Box::new(move |entry| {
                    if self.stopped() {
                        return WalkState::Quit;
                    }
                    let entry = match entry {
                        Ok(entry) => entry,
                        Err(_) => {
                            self.unreadable.store(true, Ordering::Relaxed);
                            return WalkState::Continue;
                        }
                    };
                    let kind_matches = entry.file_type().is_some_and(|kind| {
                        if directories {
                            kind.is_dir()
                        } else {
                            kind.is_file()
                        }
                    });
                    if kind_matches && wanted.contains(entry.path()) {
                        reached.lock().unwrap().insert(entry.path().to_path_buf());
                    }
                    WalkState::Continue
                })
            },
        );
        reached.into_inner().unwrap()
    }

    fn summary(&self) -> TextSearchSummary {
        let mut warning = None::<String>;
        let mut truncated = self.truncated.load(Ordering::Relaxed);
        if self.long_lines.load(Ordering::Relaxed) {
            warning = Some(LONG_LINES_WARNING.into());
        }
        if self.timed_out.load(Ordering::Relaxed) {
            warning = Some(TIMEOUT_WARNING.into());
            truncated = true;
        }
        if self.unreadable.load(Ordering::Relaxed) {
            truncated = true;
            warning = Some(match warning {
                Some(previous) => format!("{previous} {UNREADABLE_WARNING}"),
                None => UNREADABLE_WARNING.into(),
            });
        }
        TextSearchSummary { truncated, warning }
    }
}

/// Searches `root` in one walk, calling `emit` with each match as it's found.
/// Stops early when `cancelled` is set or at `deadline`. Returns an error
/// message for an invalid query.
pub fn search(
    root: &Path,
    query: &TextQuery,
    global_excludes: Option<&Path>,
    cancelled: &AtomicBool,
    emit: &(dyn Fn(TextMatch) + Sync),
    deadline: Deadline,
) -> Result<TextSearchSummary, String> {
    let run = Run::new(root, query, global_excludes, cancelled, emit, deadline)?;
    let (starts, excluded) =
        directory_scopes(root, &query.include_directories, &query.exclude_directories);
    run.walk(&starts, excluded, &HashSet::new());
    Ok(run.summary())
}

/// Keeps `directories` in order, dropping the root and any at or beneath a
/// directory already kept or in `earlier`.
fn ordered_directories(
    root: &Path,
    directories: impl IntoIterator<Item = PathBuf>,
    earlier: &[PathBuf],
) -> Vec<PathBuf> {
    let mut kept: Vec<PathBuf> = Vec::new();
    for directory in directories {
        if directory == root
            || !directory.starts_with(root)
            || earlier
                .iter()
                .chain(kept.iter())
                .any(|listed| directory.starts_with(listed))
        {
            continue;
        }
        kept.push(directory);
    }
    kept
}

/// Searches the focused files, then their directories, then the changed
/// directories `changed_directories` names once it's ready (heaviest first),
/// then the rest of the query's scope; each pass skips what an earlier one
/// searched, and a file or directory counts only if a full walk of the scope
/// would reach it. The cap, `deadline`, and cancel are shared by every pass.
/// Paths are absolute, beneath `root`, and resolved without symlinks.
/// `changed_directories` is asked only after the first two passes, and may
/// answer `None` to skip the third.
#[allow(clippy::too_many_arguments)]
pub fn search_prioritized(
    root: &Path,
    query: &TextQuery,
    priorities: &Priorities,
    global_excludes: Option<&Path>,
    cancelled: &AtomicBool,
    changed_directories: &mut dyn FnMut() -> Option<Vec<PathBuf>>,
    emit: &(dyn Fn(TextMatch) + Sync),
    deadline: Deadline,
) -> Result<TextSearchSummary, String> {
    let run = Run::new(root, query, global_excludes, cancelled, emit, deadline)?;
    let (starts, excluded) =
        directory_scopes(root, &query.include_directories, &query.exclude_directories);

    // 1. The focused files a full walk would search, one at a time, in the order given.
    let mut hinted: Vec<PathBuf> = Vec::new();
    for file in priorities.focused_files.iter().take(MAXIMUM_FOCUSED_FILES) {
        if file.starts_with(root) && !hinted.contains(file) {
            hinted.push(file.clone());
        }
    }
    let reached = run.reachable(&hinted, false, &starts, &excluded);
    let focused: Vec<PathBuf> = hinted
        .into_iter()
        .filter(|file| reached.contains(file))
        .collect();
    let mut searcher = Run::searcher();
    for file in &focused {
        if run.stopped() {
            break;
        }
        run.search_file(&mut searcher, file);
    }
    let skipped: HashSet<PathBuf> = focused.iter().cloned().collect();

    // Each priority directory is its own walk, so the order holds, and later
    // walks leave out every earlier directory.
    let mut searched: Vec<PathBuf> = Vec::new();
    let walk_each = |directories: Vec<PathBuf>, searched: &mut Vec<PathBuf>| {
        for directory in directories {
            if run.stopped() {
                return;
            }
            let mut left_out = excluded.clone();
            left_out.extend(searched.iter().cloned());
            run.walk(std::slice::from_ref(&directory), left_out, &skipped);
            searched.push(directory);
        }
    };

    // 2. The focused files' directories, never the root itself. A full walk
    // reached each one on its way to the file.
    let parents = ordered_directories(
        root,
        focused
            .iter()
            .filter_map(|file| file.parent().map(Path::to_path_buf)),
        &[],
    );
    walk_each(parents, &mut searched);

    // 3. The directories with the most changes that a full walk would enter.
    if !run.stopped()
        && let Some(changed) = changed_directories()
    {
        let changed = ordered_directories(
            root,
            changed.into_iter().take(MAXIMUM_CHANGED_DIRECTORIES),
            &searched,
        );
        let reached = run.reachable(&changed, true, &starts, &excluded);
        let changed = changed
            .into_iter()
            .filter(|directory| reached.contains(directory))
            .collect();
        walk_each(changed, &mut searched);
    }

    // 4. The rest of the scope. A start already searched as a priority
    // directory is dropped: a walk's start isn't pruned by its exclusions.
    let rest: Vec<PathBuf> = starts
        .iter()
        .filter(|start| {
            !searched
                .iter()
                .any(|directory| start.starts_with(directory))
        })
        .cloned()
        .collect();
    let mut left_out = excluded.clone();
    left_out.extend(searched);
    run.walk(&rest, left_out, &skipped);
    Ok(run.summary())
}

/// The directories with the most changes, heaviest first: each changed file
/// weighs its added and removed lines plus one, on its parent directory. The
/// workspace root, and a directory beneath one already listed, are left out.
pub fn weighted_change_directories(changes: &[(String, u64)]) -> Vec<String> {
    let mut weights: std::collections::HashMap<String, u64> = std::collections::HashMap::new();
    for (path, lines) in changes {
        let Some((parent, _)) = path.rsplit_once('/') else {
            continue;
        };
        *weights.entry(parent.to_string()).or_default() += lines + 1;
    }
    let mut ordered: Vec<(String, u64)> = weights.into_iter().collect();
    ordered.sort_by(|(left, left_weight), (right, right_weight)| {
        right_weight.cmp(left_weight).then_with(|| left.cmp(right))
    });
    let mut kept: Vec<String> = Vec::new();
    for (directory, _) in ordered {
        let beneath = kept
            .iter()
            .any(|listed| directory.starts_with(&format!("{listed}/")));
        if !beneath {
            kept.push(directory);
        }
        if kept.len() == MAXIMUM_CHANGED_DIRECTORIES {
            break;
        }
    }
    kept
}

/// Gathers matches into batches: a batch goes to `send` once it holds
/// [`BATCH_SIZE`] matches or [`BATCH_INTERVAL`] after its oldest match
/// arrived, and the rest once every sender is dropped.
pub fn batch_matches(matches: mpsc::Receiver<TextMatch>, mut send: impl FnMut(Vec<TextMatch>)) {
    let mut pending: Vec<TextMatch> = Vec::new();
    let mut oldest: Option<Instant> = None;
    loop {
        let next = match oldest {
            None => matches
                .recv()
                .map_err(|_| mpsc::RecvTimeoutError::Disconnected),
            Some(since) => matches.recv_timeout(BATCH_INTERVAL.saturating_sub(since.elapsed())),
        };
        match next {
            Ok(found) => {
                if pending.is_empty() {
                    oldest = Some(Instant::now());
                }
                pending.push(found);
                if pending.len() >= BATCH_SIZE {
                    send(std::mem::take(&mut pending));
                    oldest = None;
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                send(std::mem::take(&mut pending));
                oldest = None;
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                if !pending.is_empty() {
                    send(pending);
                }
                return;
            }
        }
    }
}

/// Collects a whole search, for tests and callers that need no streaming.
pub fn search_all(
    root: &Path,
    query: &TextQuery,
    global_excludes: Option<&Path>,
) -> Result<(Vec<TextMatch>, TextSearchSummary), String> {
    let collected = Mutex::new(Vec::new());
    let summary = search(
        root,
        query,
        global_excludes,
        &AtomicBool::new(false),
        &|found| collected.lock().unwrap().push(found),
        Deadline::at(Instant::now() + TIME_LIMIT),
    )?;
    let mut matches = collected.into_inner().unwrap();
    matches.sort_by(|a, b| (&a.path, a.line).cmp(&(&b.path, b.line)));
    Ok((matches, summary))
}

#[cfg(test)]
#[path = "text_search_test.rs"]
mod tests;

//! The runner's file-filter glob syntax, so both routes filter identically.
//!
//! `*` matches within one path segment, `**` across segments, `?` one
//! character, and `{a,b}` literal alternatives. A pattern without `/` matches
//! a basename at any depth, and a trailing `/` matches the whole subtree.

use regex::{Regex, RegexBuilder};

/// Translates one glob into an anchored, case-insensitive regex over a
/// `/`-separated relative path.
pub fn glob_regex(pattern: &str) -> Regex {
    let mut pattern = pattern.to_string();
    if !pattern.contains('/') {
        pattern = format!("**/{pattern}");
    } else if pattern.ends_with('/') {
        pattern.push_str("**");
    }
    let characters: Vec<char> = pattern.chars().collect();
    let mut source = String::from("^");
    let mut index = 0;
    while index < characters.len() {
        match characters[index] {
            '*' if characters.get(index + 1) == Some(&'*') => {
                if characters.get(index + 2) == Some(&'/') {
                    source.push_str("(?:.*/)?");
                    index += 3;
                } else {
                    source.push_str(".*");
                    index += 2;
                }
            }
            '*' => {
                source.push_str("[^/]*");
                index += 1;
            }
            '?' => {
                source.push_str("[^/]");
                index += 1;
            }
            '{' => match characters[index..].iter().position(|&c| c == '}') {
                Some(offset) => {
                    let body: String = characters[index + 1..index + offset].iter().collect();
                    let alternatives: Vec<String> = body.split(',').map(regex::escape).collect();
                    source.push_str(&format!("(?:{})", alternatives.join("|")));
                    index += offset + 1;
                }
                None => {
                    source.push_str(&regex::escape("{"));
                    index += 1;
                }
            },
            character => {
                source.push_str(&regex::escape(&character.to_string()));
                index += 1;
            }
        }
    }
    source.push('$');
    RegexBuilder::new(&source)
        .case_insensitive(true)
        .build()
        .expect("escaped glob translations are valid regexes")
}

/// Splits a comma-separated glob list on commas outside `{...}`, dropping
/// blank entries.
pub fn split_glob_list(raw: &str) -> Vec<String> {
    let mut patterns = Vec::new();
    let mut current = String::new();
    let mut depth = 0usize;
    for character in raw.chars() {
        match character {
            '{' => {
                depth += 1;
                current.push(character);
            }
            '}' => {
                depth = depth.saturating_sub(1);
                current.push(character);
            }
            ',' if depth == 0 => patterns.push(std::mem::take(&mut current)),
            _ => current.push(character),
        }
    }
    patterns.push(current);
    patterns
        .into_iter()
        .map(|pattern| pattern.trim().to_string())
        .filter(|pattern| !pattern.is_empty())
        .collect()
}

/// Include and exclude globs applied to relative paths. An excluded
/// directory excludes everything beneath it.
#[derive(Default)]
pub struct PathFilter {
    include: Vec<Regex>,
    exclude: Vec<Regex>,
}

impl PathFilter {
    pub fn new(include: &[String], exclude: &[String]) -> Self {
        Self {
            include: include.iter().map(|glob| glob_regex(glob)).collect(),
            exclude: exclude.iter().map(|glob| glob_regex(glob)).collect(),
        }
    }

    pub fn keeps(&self, path: &str) -> bool {
        if !self.exclude.is_empty() {
            let mut end = 0;
            loop {
                let next = path[end..].find('/').map(|offset| end + offset);
                let ancestor = &path[..next.unwrap_or(path.len())];
                if self.exclude.iter().any(|regex| regex.is_match(ancestor)) {
                    return false;
                }
                match next {
                    Some(slash) => end = slash + 1,
                    None => break,
                }
            }
        }
        self.include.is_empty() || self.include.iter().any(|regex| regex.is_match(path))
    }
}

#[cfg(test)]
#[path = "globs_test.rs"]
mod tests;

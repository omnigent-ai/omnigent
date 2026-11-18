use super::*;

#[test]
fn an_empty_revision_means_the_default() {
    assert_eq!(checked_revision("", RevisionField::Base), Ok(None));
    assert_eq!(checked_revision("   ", RevisionField::Head), Ok(None));
}

#[test]
fn a_revision_is_trimmed_and_kept_as_typed() {
    for revision in [
        "main",
        "origin/main",
        "v1.2",
        "HEAD~3",
        "a1b2c3d",
        "feature/x^{commit}",
    ] {
        assert_eq!(
            checked_revision(&format!("  {revision}\t"), RevisionField::Base),
            Ok(Some(revision.to_string()))
        );
    }
}

#[test]
fn an_option_like_revision_never_reaches_git() {
    for revision in ["-n", "--output=/tmp/x", "--exec=sh", "-"] {
        assert_eq!(
            checked_revision(revision, RevisionField::Head),
            Err(RevisionUnavailable::Invalid(RevisionField::Head)),
            "{revision}"
        );
    }
}

#[test]
fn control_characters_and_overlong_revisions_are_refused() {
    for revision in ["main\nHEAD", "main\0", "a\u{1b}[31m"] {
        assert_eq!(
            checked_revision(revision, RevisionField::Base),
            Err(RevisionUnavailable::Invalid(RevisionField::Base)),
            "{revision:?}"
        );
    }
    let longest = "a".repeat(MAXIMUM_REVISION_BYTES);
    assert_eq!(
        checked_revision(&longest, RevisionField::Base),
        Ok(Some(longest.clone()))
    );
    assert_eq!(
        checked_revision(&format!("{longest}a"), RevisionField::Base),
        Err(RevisionUnavailable::Invalid(RevisionField::Base))
    );
}

fn listing(lines: &[(&str, &str, &str)]) -> String {
    lines
        .iter()
        .map(|(full, short, symref)| format!("{full}\0{short}\0{symref}\n"))
        .collect()
}

#[test]
fn only_a_remotes_symbolic_head_is_left_out() {
    let output = listing(&[
        ("refs/heads/main", "main", ""),
        ("refs/heads/topic/HEAD", "topic/HEAD", ""),
        (
            "refs/remotes/origin/HEAD",
            "origin",
            "refs/remotes/origin/main",
        ),
        ("refs/remotes/origin/main", "origin/main", ""),
        (
            "refs/remotes/origin/release/HEAD",
            "origin/release/HEAD",
            "",
        ),
        ("refs/tags/release/HEAD", "release/HEAD", ""),
    ]);
    let (refs, truncated) = usable_refs(&output, 100);
    assert_eq!(
        refs,
        [
            "main",
            "topic/HEAD",
            "origin/main",
            "origin/release/HEAD",
            "release/HEAD"
        ]
    );
    assert!(!truncated);
}

#[test]
fn a_listing_git_cut_short_is_truncated_even_after_leaving_heads_out() {
    let output = listing(&[
        ("refs/heads/a", "a", ""),
        (
            "refs/remotes/origin/HEAD",
            "origin",
            "refs/remotes/origin/a",
        ),
        ("refs/heads/b", "b", ""),
    ]);
    // Git returned every ref asked for, so there may be more.
    assert_eq!(
        usable_refs(&output, 3),
        (vec!["a".to_string(), "b".to_string()], true)
    );
    assert_eq!(
        usable_refs(&output, 4),
        (vec!["a".to_string(), "b".to_string()], false)
    );
}

#[test]
fn more_usable_refs_than_offered_are_truncated() {
    let lines: Vec<(String, String)> = (0..MAXIMUM_REFS + 1)
        .map(|index| (format!("refs/heads/b{index:04}"), format!("b{index:04}")))
        .collect();
    let output: String = lines
        .iter()
        .map(|(full, short)| format!("{full}\0{short}\0\n"))
        .collect();
    let (refs, truncated) = usable_refs(&output, MAXIMUM_REFS + 100);
    assert_eq!(refs.len(), MAXIMUM_REFS);
    assert!(truncated);
}

#[test]
fn commits_parse_from_log_records_and_keep_separators_in_subjects() {
    let first = "a".repeat(40);
    let second = "b".repeat(64);
    let output = format!(
        "{first}\x1f{second} {second}\x1f1700000000\x1fAda Example\x1fFix the parser\x1f again\0\n{second}\x1f\x1f1600000000\x1fBo\x1f\0not a commit\0"
    );
    assert_eq!(
        parse_commits(output.as_bytes()),
        [
            BranchCommit {
                object: first,
                root: false,
                subject: "Fix the parser\x1f again".to_string(),
                author_name: "Ada Example".to_string(),
                author_time_unix: 1_700_000_000,
            },
            BranchCommit {
                object: second,
                root: true,
                subject: String::new(),
                author_name: "Bo".to_string(),
                author_time_unix: 1_600_000_000,
            },
        ]
    );
}

#[test]
fn a_commit_is_a_root_only_when_its_header_names_no_parent() {
    let parent = "a".repeat(40);
    assert!(commit_header_has_no_parents(
        b"tree 1\nauthor A <a@example.com> 1 +0000\n\nparent in the message only\n"
    ));
    assert!(!commit_header_has_no_parents(
        format!("tree 1\nparent {parent}\nauthor A <a@example.com> 1 +0000\n\nmessage\n")
            .as_bytes()
    ));
    assert!(!commit_header_has_no_parents(
        format!("tree 1\nparent {parent}\nparent {parent}\n\nmerge\n").as_bytes()
    ));
}

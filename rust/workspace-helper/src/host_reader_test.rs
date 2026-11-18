use std::os::unix::fs::symlink;

use super::*;

#[test]
fn normpath_matches_posixpath() {
    for (path, normalized) in [
        ("", "."),
        (".", "."),
        ("./", "."),
        ("a//b/./c/", "a/b/c"),
        ("a/../b", "b"),
        ("a/..", "."),
        ("../a", "../a"),
        ("a/../../b", "../b"),
        ("..foo/bar", "..foo/bar"),
        ("/a/../..", "/"),
        ("/a/b", "/a/b"),
    ] {
        assert_eq!(normpath(path), normalized, "{path:?}");
    }
}

#[test]
fn validation_rejects_what_the_host_rejects() {
    assert_eq!(validate("").unwrap(), "");
    assert_eq!(validate("./src//main.rs").unwrap(), "src/main.rs");
    assert_eq!(validate("src/../README.md").unwrap(), "README.md");
    for path in ["/etc/hosts", "../outside", "a/../../b", "..hidden", "a\0b"] {
        assert_eq!(
            validate(path).unwrap_err().code(),
            tonic::Code::InvalidArgument,
            "{path:?}"
        );
    }
}

#[test]
fn realpath_follows_links_that_exist_and_keeps_missing_components() {
    let directory = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(directory.path()).unwrap();
    fs::create_dir(root.join("real")).unwrap();
    symlink("real", root.join("link")).unwrap();
    symlink("../real/../real", root.join("real/self")).unwrap();
    symlink("/nowhere/target", root.join("broken")).unwrap();
    symlink("loop", root.join("loop")).unwrap();

    assert_eq!(realpath(&root.join("link/file")), root.join("real/file"));
    assert_eq!(realpath(&root.join("real/self")), root.join("real"));
    assert_eq!(realpath(&root.join("missing/x")), root.join("missing/x"));
    // A broken link resolves to its target, as Python's does, even outside.
    assert_eq!(
        realpath(&root.join("broken")),
        PathBuf::from("/nowhere/target")
    );
    // A loop ends instead of spinning.
    assert!(realpath(&root.join("loop")).starts_with(&root));
}

#[test]
fn small_files_are_delivered_whole() {
    assert_eq!(
        cap_file_bytes(b"hello".to_vec(), MAX_READ_BYTES),
        CappedFile {
            data: b"hello".to_vec(),
            truncated: false,
            is_text: true,
        }
    );
    let binary = cap_file_bytes(vec![0xff, 0xfe, 0, 1], MAX_READ_BYTES);
    assert!(!binary.is_text && !binary.truncated);
    assert_eq!(binary.data.len(), 4);
    // Invalid UTF-8 at the very end of a file that fits is still binary.
    assert!(!cap_file_bytes(b"caf\xc3".to_vec(), MAX_READ_BYTES).is_text);
}

#[test]
fn the_cap_drops_a_split_codepoint_and_keeps_text() {
    let mut data = vec![b'x'; MAX_READ_BYTES - 1];
    data.extend("é and more".as_bytes());
    let capped = cap_file_bytes(data, MAX_READ_BYTES);
    assert!(capped.truncated && capped.is_text);
    assert_eq!(capped.data.len(), MAX_READ_BYTES - 1);

    // Earlier invalid bytes make an oversize file binary.
    let mut data = vec![b'x'; MAX_READ_BYTES + 10];
    data[100] = 0xff;
    let capped = cap_file_bytes(data, MAX_READ_BYTES);
    assert!(capped.truncated && !capped.is_text);
    assert_eq!(capped.data.len(), MAX_READ_BYTES);
}

#[test]
fn quoting_matches_python_repr_for_ordinary_paths() {
    assert_eq!(quoted("src/main.rs"), "'src/main.rs'");
    assert_eq!(quoted("it's"), "\"it's\"");
}

#[test]
fn a_smaller_maximum_caps_the_same_way() {
    let capped = cap_file_bytes("aé".as_bytes().to_vec(), 2);
    assert_eq!(capped.data, b"a");
    assert!(capped.truncated && capped.is_text);
}

fn entry(path: &str) -> Entry {
    Entry {
        name: path.rsplit('/').next().unwrap().into(),
        path: path.into(),
        ..Entry::default()
    }
}

#[test]
fn pages_resume_after_their_token_even_when_its_entry_is_gone() {
    let entries = || ["d/a", "d/b", "d/c", "d/e"].map(entry).to_vec();
    let paths = |page: &[Entry]| {
        page.iter()
            .map(|entry| entry.path.clone())
            .collect::<Vec<_>>()
    };
    let (first, token) = page(entries(), 2, "").unwrap();
    assert_eq!(
        (paths(&first), token.as_str()),
        (vec!["d/a".into(), "d/b".into()], "d/b")
    );
    let (rest, token) = page(entries(), 2, "d/b").unwrap();
    assert_eq!(
        (paths(&rest), token.as_str()),
        (vec!["d/c".into(), "d/e".into()], "")
    );
    let (after_gone, _) = page(entries(), 10, "d/d").unwrap();
    assert_eq!(paths(&after_gone), vec!["d/e".to_string()]);
    assert_eq!(page(entries(), 0, "").unwrap().0.len(), 4);
    assert_eq!(
        page(entries(), -1, "").unwrap_err().code(),
        tonic::Code::InvalidArgument
    );
}

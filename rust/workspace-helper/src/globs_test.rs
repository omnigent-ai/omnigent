use super::*;

#[test]
fn basename_globs_match_at_any_depth() {
    let regex = glob_regex("*.ts");
    assert!(regex.is_match("a.ts"));
    assert!(regex.is_match("src/app/A.TS"));
    assert!(!regex.is_match("a.tsx"));
}

#[test]
fn path_globs_are_anchored_and_double_star_crosses_segments() {
    let regex = glob_regex("src/**/*.rs");
    assert!(regex.is_match("src/main.rs"));
    assert!(regex.is_match("src/a/b/c.rs"));
    assert!(!regex.is_match("lib/src/main.rs"));
    assert!(!glob_regex("src/*.rs").is_match("src/a/b.rs"));
}

#[test]
fn trailing_slash_matches_the_subtree() {
    let regex = glob_regex("build/");
    assert!(regex.is_match("build/out/x.o"));
    assert!(!regex.is_match("rebuild/x"));
}

#[test]
fn braces_are_literal_alternatives_and_unbalanced_braces_are_literal() {
    let regex = glob_regex("*.{js,ts}");
    assert!(regex.is_match("a.js") && regex.is_match("b/c.ts"));
    assert!(!regex.is_match("a.jsx"));
    assert!(glob_regex("a{b").is_match("a{b"));
    assert!(glob_regex("a.(b)+").is_match("a.(b)+"));
}

#[test]
fn split_keeps_commas_inside_braces() {
    assert_eq!(
        split_glob_list("*.{js,ts}, src/** ,,"),
        vec!["*.{js,ts}".to_string(), "src/**".to_string()]
    );
    assert!(split_glob_list("").is_empty());
}

#[test]
fn excluded_directories_take_their_subtree() {
    let filter = PathFilter::new(&[], &["node_modules".to_string()]);
    assert!(!filter.keeps("node_modules/a/b.js"));
    assert!(!filter.keeps("web/node_modules/x"));
    assert!(filter.keeps("web/src/x"));
    let include = PathFilter::new(&["*.md".to_string()], &[]);
    assert!(include.keeps("docs/a.md"));
    assert!(!include.keeps("docs/a.txt"));
}

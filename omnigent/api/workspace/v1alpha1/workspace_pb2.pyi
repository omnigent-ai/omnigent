import datetime

from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EntryType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ENTRY_TYPE_UNSPECIFIED: _ClassVar[EntryType]
    ENTRY_TYPE_FILE: _ClassVar[EntryType]
    ENTRY_TYPE_DIRECTORY: _ClassVar[EntryType]

class ContentEncoding(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONTENT_ENCODING_UNSPECIFIED: _ClassVar[ContentEncoding]
    CONTENT_ENCODING_UTF8: _ClassVar[ContentEncoding]
    CONTENT_ENCODING_BINARY: _ClassVar[ContentEncoding]

class FileSearchMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FILE_SEARCH_MODE_UNSPECIFIED: _ClassVar[FileSearchMode]
    FILE_SEARCH_MODE_FUZZY: _ClassVar[FileSearchMode]
    FILE_SEARCH_MODE_SUBSTRING: _ClassVar[FileSearchMode]

class SearchOrder(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SEARCH_ORDER_UNSPECIFIED: _ClassVar[SearchOrder]
    SEARCH_ORDER_WALK: _ClassVar[SearchOrder]
    SEARCH_ORDER_PRIORITY: _ClassVar[SearchOrder]

class ComparisonKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COMPARISON_KIND_UNSPECIFIED: _ClassVar[ComparisonKind]
    COMPARISON_KIND_LAST_COMMIT: _ClassVar[ComparisonKind]
    COMPARISON_KIND_BRANCH_POINT: _ClassVar[ComparisonKind]
    COMPARISON_KIND_REVISIONS: _ClassVar[ComparisonKind]

class ComparisonProblem(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COMPARISON_PROBLEM_UNSPECIFIED: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_NOT_A_REPOSITORY: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_NO_COMMITS: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_NO_DEFAULT_BRANCH: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_NO_BRANCH_POINT: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_MISSING_OBJECT: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_PARTIAL_CLONE_NEEDS_NEWER_GIT: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_TIMEOUT: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_TOO_MANY_FILES: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_LISTING_TOO_LARGE: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_GIT_UNAVAILABLE: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_GIT_FAILED: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_MULTIPLE_REPOSITORIES: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_INVALID_REVISION: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_BASE_NOT_FOUND: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_HEAD_NOT_FOUND: _ClassVar[ComparisonProblem]
    COMPARISON_PROBLEM_NO_COMMON_HISTORY: _ClassVar[ComparisonProblem]

class ComparisonField(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COMPARISON_FIELD_UNSPECIFIED: _ClassVar[ComparisonField]
    COMPARISON_FIELD_BASE: _ClassVar[ComparisonField]
    COMPARISON_FIELD_HEAD: _ClassVar[ComparisonField]
    COMPARISON_FIELD_BOTH: _ClassVar[ComparisonField]

class ChangeStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CHANGE_STATUS_UNSPECIFIED: _ClassVar[ChangeStatus]
    CHANGE_STATUS_ADDED: _ClassVar[ChangeStatus]
    CHANGE_STATUS_MODIFIED: _ClassVar[ChangeStatus]
    CHANGE_STATUS_DELETED: _ClassVar[ChangeStatus]
    CHANGE_STATUS_TYPE_CHANGED: _ClassVar[ChangeStatus]
    CHANGE_STATUS_CONFLICTED: _ClassVar[ChangeStatus]

class GitEntryKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    GIT_ENTRY_KIND_UNSPECIFIED: _ClassVar[GitEntryKind]
    GIT_ENTRY_KIND_FILE: _ClassVar[GitEntryKind]
    GIT_ENTRY_KIND_SYMLINK: _ClassVar[GitEntryKind]
    GIT_ENTRY_KIND_SUBMODULE: _ClassVar[GitEntryKind]

class ChangedFileProblem(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CHANGED_FILE_PROBLEM_UNSPECIFIED: _ClassVar[ChangedFileProblem]
    CHANGED_FILE_PROBLEM_FILTERED: _ClassVar[ChangedFileProblem]
    CHANGED_FILE_PROBLEM_MISSING_OBJECT: _ClassVar[ChangedFileProblem]

class RepositoryContextProblem(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED: _ClassVar[RepositoryContextProblem]
    REPOSITORY_CONTEXT_PROBLEM_NOT_A_REPOSITORY: _ClassVar[RepositoryContextProblem]
    REPOSITORY_CONTEXT_PROBLEM_UNSUPPORTED_CONFIG: _ClassVar[RepositoryContextProblem]
    REPOSITORY_CONTEXT_PROBLEM_REPOSITORY_CHANGING: _ClassVar[RepositoryContextProblem]
    REPOSITORY_CONTEXT_PROBLEM_TIMEOUT: _ClassVar[RepositoryContextProblem]
    REPOSITORY_CONTEXT_PROBLEM_GIT_UNAVAILABLE: _ClassVar[RepositoryContextProblem]
    REPOSITORY_CONTEXT_PROBLEM_GIT_FAILED: _ClassVar[RepositoryContextProblem]

class PushUnresolved(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PUSH_UNRESOLVED_UNSPECIFIED: _ClassVar[PushUnresolved]
    PUSH_UNRESOLVED_REFSPEC: _ClassVar[PushUnresolved]
    PUSH_UNRESOLVED_PUSH_URL: _ClassVar[PushUnresolved]
    PUSH_UNRESOLVED_PUSH_DEFAULT: _ClassVar[PushUnresolved]

class CompletionKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COMPLETION_KIND_UNSPECIFIED: _ClassVar[CompletionKind]
    COMPLETION_KIND_MENTION: _ClassVar[CompletionKind]
    COMPLETION_KIND_REFERENCE: _ClassVar[CompletionKind]
ENTRY_TYPE_UNSPECIFIED: EntryType
ENTRY_TYPE_FILE: EntryType
ENTRY_TYPE_DIRECTORY: EntryType
CONTENT_ENCODING_UNSPECIFIED: ContentEncoding
CONTENT_ENCODING_UTF8: ContentEncoding
CONTENT_ENCODING_BINARY: ContentEncoding
FILE_SEARCH_MODE_UNSPECIFIED: FileSearchMode
FILE_SEARCH_MODE_FUZZY: FileSearchMode
FILE_SEARCH_MODE_SUBSTRING: FileSearchMode
SEARCH_ORDER_UNSPECIFIED: SearchOrder
SEARCH_ORDER_WALK: SearchOrder
SEARCH_ORDER_PRIORITY: SearchOrder
COMPARISON_KIND_UNSPECIFIED: ComparisonKind
COMPARISON_KIND_LAST_COMMIT: ComparisonKind
COMPARISON_KIND_BRANCH_POINT: ComparisonKind
COMPARISON_KIND_REVISIONS: ComparisonKind
COMPARISON_PROBLEM_UNSPECIFIED: ComparisonProblem
COMPARISON_PROBLEM_NOT_A_REPOSITORY: ComparisonProblem
COMPARISON_PROBLEM_NO_COMMITS: ComparisonProblem
COMPARISON_PROBLEM_NO_DEFAULT_BRANCH: ComparisonProblem
COMPARISON_PROBLEM_NO_BRANCH_POINT: ComparisonProblem
COMPARISON_PROBLEM_MISSING_OBJECT: ComparisonProblem
COMPARISON_PROBLEM_PARTIAL_CLONE_NEEDS_NEWER_GIT: ComparisonProblem
COMPARISON_PROBLEM_TIMEOUT: ComparisonProblem
COMPARISON_PROBLEM_TOO_MANY_FILES: ComparisonProblem
COMPARISON_PROBLEM_LISTING_TOO_LARGE: ComparisonProblem
COMPARISON_PROBLEM_GIT_UNAVAILABLE: ComparisonProblem
COMPARISON_PROBLEM_GIT_FAILED: ComparisonProblem
COMPARISON_PROBLEM_MULTIPLE_REPOSITORIES: ComparisonProblem
COMPARISON_PROBLEM_INVALID_REVISION: ComparisonProblem
COMPARISON_PROBLEM_BASE_NOT_FOUND: ComparisonProblem
COMPARISON_PROBLEM_HEAD_NOT_FOUND: ComparisonProblem
COMPARISON_PROBLEM_NO_COMMON_HISTORY: ComparisonProblem
COMPARISON_FIELD_UNSPECIFIED: ComparisonField
COMPARISON_FIELD_BASE: ComparisonField
COMPARISON_FIELD_HEAD: ComparisonField
COMPARISON_FIELD_BOTH: ComparisonField
CHANGE_STATUS_UNSPECIFIED: ChangeStatus
CHANGE_STATUS_ADDED: ChangeStatus
CHANGE_STATUS_MODIFIED: ChangeStatus
CHANGE_STATUS_DELETED: ChangeStatus
CHANGE_STATUS_TYPE_CHANGED: ChangeStatus
CHANGE_STATUS_CONFLICTED: ChangeStatus
GIT_ENTRY_KIND_UNSPECIFIED: GitEntryKind
GIT_ENTRY_KIND_FILE: GitEntryKind
GIT_ENTRY_KIND_SYMLINK: GitEntryKind
GIT_ENTRY_KIND_SUBMODULE: GitEntryKind
CHANGED_FILE_PROBLEM_UNSPECIFIED: ChangedFileProblem
CHANGED_FILE_PROBLEM_FILTERED: ChangedFileProblem
CHANGED_FILE_PROBLEM_MISSING_OBJECT: ChangedFileProblem
REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED: RepositoryContextProblem
REPOSITORY_CONTEXT_PROBLEM_NOT_A_REPOSITORY: RepositoryContextProblem
REPOSITORY_CONTEXT_PROBLEM_UNSUPPORTED_CONFIG: RepositoryContextProblem
REPOSITORY_CONTEXT_PROBLEM_REPOSITORY_CHANGING: RepositoryContextProblem
REPOSITORY_CONTEXT_PROBLEM_TIMEOUT: RepositoryContextProblem
REPOSITORY_CONTEXT_PROBLEM_GIT_UNAVAILABLE: RepositoryContextProblem
REPOSITORY_CONTEXT_PROBLEM_GIT_FAILED: RepositoryContextProblem
PUSH_UNRESOLVED_UNSPECIFIED: PushUnresolved
PUSH_UNRESOLVED_REFSPEC: PushUnresolved
PUSH_UNRESOLVED_PUSH_URL: PushUnresolved
PUSH_UNRESOLVED_PUSH_DEFAULT: PushUnresolved
COMPLETION_KIND_UNSPECIFIED: CompletionKind
COMPLETION_KIND_MENTION: CompletionKind
COMPLETION_KIND_REFERENCE: CompletionKind

class GetCapabilitiesRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetCapabilitiesResponse(_message.Message):
    __slots__ = ("capabilities",)
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, capabilities: _Optional[_Iterable[str]] = ...) -> None: ...

class ListDirectoryRequest(_message.Message):
    __slots__ = ("path", "page_size", "page_token", "hide_ignored", "collapse_single_child_directories")
    PATH_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    HIDE_IGNORED_FIELD_NUMBER: _ClassVar[int]
    COLLAPSE_SINGLE_CHILD_DIRECTORIES_FIELD_NUMBER: _ClassVar[int]
    path: str
    page_size: int
    page_token: str
    hide_ignored: bool
    collapse_single_child_directories: bool
    def __init__(self, path: _Optional[str] = ..., page_size: _Optional[int] = ..., page_token: _Optional[str] = ..., hide_ignored: _Optional[bool] = ..., collapse_single_child_directories: _Optional[bool] = ...) -> None: ...

class ListDirectoryResponse(_message.Message):
    __slots__ = ("entries", "next_page_token")
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[Entry]
    next_page_token: str
    def __init__(self, entries: _Optional[_Iterable[_Union[Entry, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class Entry(_message.Message):
    __slots__ = ("name", "path", "type", "size", "modify_time", "is_symlink", "ignored", "leaf_directory_path")
    NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    MODIFY_TIME_FIELD_NUMBER: _ClassVar[int]
    IS_SYMLINK_FIELD_NUMBER: _ClassVar[int]
    IGNORED_FIELD_NUMBER: _ClassVar[int]
    LEAF_DIRECTORY_PATH_FIELD_NUMBER: _ClassVar[int]
    name: str
    path: str
    type: EntryType
    size: int
    modify_time: _timestamp_pb2.Timestamp
    is_symlink: bool
    ignored: bool
    leaf_directory_path: str
    def __init__(self, name: _Optional[str] = ..., path: _Optional[str] = ..., type: _Optional[_Union[EntryType, str]] = ..., size: _Optional[int] = ..., modify_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., is_symlink: _Optional[bool] = ..., ignored: _Optional[bool] = ..., leaf_directory_path: _Optional[str] = ...) -> None: ...

class ReadFileRequest(_message.Message):
    __slots__ = ("path", "maximum_bytes")
    PATH_FIELD_NUMBER: _ClassVar[int]
    MAXIMUM_BYTES_FIELD_NUMBER: _ClassVar[int]
    path: str
    maximum_bytes: int
    def __init__(self, path: _Optional[str] = ..., maximum_bytes: _Optional[int] = ...) -> None: ...

class ReadFileResponse(_message.Message):
    __slots__ = ("metadata", "data")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    metadata: FileMetadata
    data: bytes
    def __init__(self, metadata: _Optional[_Union[FileMetadata, _Mapping]] = ..., data: _Optional[bytes] = ...) -> None: ...

class FileMetadata(_message.Message):
    __slots__ = ("size", "truncated", "encoding", "content_type", "modify_time")
    SIZE_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    ENCODING_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    MODIFY_TIME_FIELD_NUMBER: _ClassVar[int]
    size: int
    truncated: bool
    encoding: ContentEncoding
    content_type: str
    modify_time: _timestamp_pb2.Timestamp
    def __init__(self, size: _Optional[int] = ..., truncated: _Optional[bool] = ..., encoding: _Optional[_Union[ContentEncoding, str]] = ..., content_type: _Optional[str] = ..., modify_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class StatPathRequest(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class StatPathResponse(_message.Message):
    __slots__ = ("entry",)
    ENTRY_FIELD_NUMBER: _ClassVar[int]
    entry: Entry
    def __init__(self, entry: _Optional[_Union[Entry, _Mapping]] = ...) -> None: ...

class SearchFilesRequest(_message.Message):
    __slots__ = ("query", "mode", "maximum_results", "path", "include", "exclude", "include_git_ignored", "include_directories", "exclude_directories", "retry_untracked")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    MAXIMUM_RESULTS_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_FIELD_NUMBER: _ClassVar[int]
    EXCLUDE_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_GIT_IGNORED_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_DIRECTORIES_FIELD_NUMBER: _ClassVar[int]
    EXCLUDE_DIRECTORIES_FIELD_NUMBER: _ClassVar[int]
    RETRY_UNTRACKED_FIELD_NUMBER: _ClassVar[int]
    query: str
    mode: FileSearchMode
    maximum_results: int
    path: str
    include: _containers.RepeatedScalarFieldContainer[str]
    exclude: _containers.RepeatedScalarFieldContainer[str]
    include_git_ignored: bool
    include_directories: _containers.RepeatedScalarFieldContainer[str]
    exclude_directories: _containers.RepeatedScalarFieldContainer[str]
    retry_untracked: bool
    def __init__(self, query: _Optional[str] = ..., mode: _Optional[_Union[FileSearchMode, str]] = ..., maximum_results: _Optional[int] = ..., path: _Optional[str] = ..., include: _Optional[_Iterable[str]] = ..., exclude: _Optional[_Iterable[str]] = ..., include_git_ignored: _Optional[bool] = ..., include_directories: _Optional[_Iterable[str]] = ..., exclude_directories: _Optional[_Iterable[str]] = ..., retry_untracked: _Optional[bool] = ...) -> None: ...

class FileSearchResult(_message.Message):
    __slots__ = ("entry", "score", "matched_indices")
    ENTRY_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    MATCHED_INDICES_FIELD_NUMBER: _ClassVar[int]
    entry: Entry
    score: int
    matched_indices: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, entry: _Optional[_Union[Entry, _Mapping]] = ..., score: _Optional[int] = ..., matched_indices: _Optional[_Iterable[int]] = ...) -> None: ...

class SearchFilesResponse(_message.Message):
    __slots__ = ("results", "truncated", "indexed_paths", "index_incomplete", "refreshing", "untracked_unavailable")
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    INDEXED_PATHS_FIELD_NUMBER: _ClassVar[int]
    INDEX_INCOMPLETE_FIELD_NUMBER: _ClassVar[int]
    REFRESHING_FIELD_NUMBER: _ClassVar[int]
    UNTRACKED_UNAVAILABLE_FIELD_NUMBER: _ClassVar[int]
    results: _containers.RepeatedCompositeFieldContainer[FileSearchResult]
    truncated: bool
    indexed_paths: int
    index_incomplete: bool
    refreshing: bool
    untracked_unavailable: bool
    def __init__(self, results: _Optional[_Iterable[_Union[FileSearchResult, _Mapping]]] = ..., truncated: _Optional[bool] = ..., indexed_paths: _Optional[int] = ..., index_incomplete: _Optional[bool] = ..., refreshing: _Optional[bool] = ..., untracked_unavailable: _Optional[bool] = ...) -> None: ...

class SearchTextRequest(_message.Message):
    __slots__ = ("query", "case_sensitive", "regular_expression", "whole_word", "include", "exclude", "maximum_results", "include_git_ignored", "include_directories", "exclude_directories", "order", "focused_paths", "changed_against")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    CASE_SENSITIVE_FIELD_NUMBER: _ClassVar[int]
    REGULAR_EXPRESSION_FIELD_NUMBER: _ClassVar[int]
    WHOLE_WORD_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_FIELD_NUMBER: _ClassVar[int]
    EXCLUDE_FIELD_NUMBER: _ClassVar[int]
    MAXIMUM_RESULTS_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_GIT_IGNORED_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_DIRECTORIES_FIELD_NUMBER: _ClassVar[int]
    EXCLUDE_DIRECTORIES_FIELD_NUMBER: _ClassVar[int]
    ORDER_FIELD_NUMBER: _ClassVar[int]
    FOCUSED_PATHS_FIELD_NUMBER: _ClassVar[int]
    CHANGED_AGAINST_FIELD_NUMBER: _ClassVar[int]
    query: str
    case_sensitive: bool
    regular_expression: bool
    whole_word: bool
    include: str
    exclude: str
    maximum_results: int
    include_git_ignored: bool
    include_directories: _containers.RepeatedScalarFieldContainer[str]
    exclude_directories: _containers.RepeatedScalarFieldContainer[str]
    order: SearchOrder
    focused_paths: _containers.RepeatedScalarFieldContainer[str]
    changed_against: ComparisonKind
    def __init__(self, query: _Optional[str] = ..., case_sensitive: _Optional[bool] = ..., regular_expression: _Optional[bool] = ..., whole_word: _Optional[bool] = ..., include: _Optional[str] = ..., exclude: _Optional[str] = ..., maximum_results: _Optional[int] = ..., include_git_ignored: _Optional[bool] = ..., include_directories: _Optional[_Iterable[str]] = ..., exclude_directories: _Optional[_Iterable[str]] = ..., order: _Optional[_Union[SearchOrder, str]] = ..., focused_paths: _Optional[_Iterable[str]] = ..., changed_against: _Optional[_Union[ComparisonKind, str]] = ...) -> None: ...

class TextSearchMatch(_message.Message):
    __slots__ = ("path", "line", "column", "text", "match_start", "match_end")
    PATH_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    COLUMN_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    MATCH_START_FIELD_NUMBER: _ClassVar[int]
    MATCH_END_FIELD_NUMBER: _ClassVar[int]
    path: str
    line: int
    column: int
    text: str
    match_start: int
    match_end: int
    def __init__(self, path: _Optional[str] = ..., line: _Optional[int] = ..., column: _Optional[int] = ..., text: _Optional[str] = ..., match_start: _Optional[int] = ..., match_end: _Optional[int] = ...) -> None: ...

class SearchTextResponse(_message.Message):
    __slots__ = ("matches", "done", "truncated", "warning")
    MATCHES_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    WARNING_FIELD_NUMBER: _ClassVar[int]
    matches: _containers.RepeatedCompositeFieldContainer[TextSearchMatch]
    done: bool
    truncated: bool
    warning: str
    def __init__(self, matches: _Optional[_Iterable[_Union[TextSearchMatch, _Mapping]]] = ..., done: _Optional[bool] = ..., truncated: _Optional[bool] = ..., warning: _Optional[str] = ...) -> None: ...

class Comparison(_message.Message):
    __slots__ = ("kind", "base_revision", "head_revision")
    KIND_FIELD_NUMBER: _ClassVar[int]
    BASE_REVISION_FIELD_NUMBER: _ClassVar[int]
    HEAD_REVISION_FIELD_NUMBER: _ClassVar[int]
    kind: ComparisonKind
    base_revision: str
    head_revision: str
    def __init__(self, kind: _Optional[_Union[ComparisonKind, str]] = ..., base_revision: _Optional[str] = ..., head_revision: _Optional[str] = ...) -> None: ...

class ComparisonUnavailable(_message.Message):
    __slots__ = ("problem", "reference", "shallow", "detail", "field")
    PROBLEM_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    SHALLOW_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    FIELD_FIELD_NUMBER: _ClassVar[int]
    problem: ComparisonProblem
    reference: str
    shallow: bool
    detail: str
    field: ComparisonField
    def __init__(self, problem: _Optional[_Union[ComparisonProblem, str]] = ..., reference: _Optional[str] = ..., shallow: _Optional[bool] = ..., detail: _Optional[str] = ..., field: _Optional[_Union[ComparisonField, str]] = ...) -> None: ...

class WorkingCopyBase(_message.Message):
    __slots__ = ("kind", "head", "base", "reference", "reference_commit", "unavailable", "merge_base", "base_display", "head_display", "head_is_working_copy")
    KIND_FIELD_NUMBER: _ClassVar[int]
    HEAD_FIELD_NUMBER: _ClassVar[int]
    BASE_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_COMMIT_FIELD_NUMBER: _ClassVar[int]
    UNAVAILABLE_FIELD_NUMBER: _ClassVar[int]
    MERGE_BASE_FIELD_NUMBER: _ClassVar[int]
    BASE_DISPLAY_FIELD_NUMBER: _ClassVar[int]
    HEAD_DISPLAY_FIELD_NUMBER: _ClassVar[int]
    HEAD_IS_WORKING_COPY_FIELD_NUMBER: _ClassVar[int]
    kind: ComparisonKind
    head: str
    base: str
    reference: str
    reference_commit: str
    unavailable: ComparisonUnavailable
    merge_base: str
    base_display: str
    head_display: str
    head_is_working_copy: bool
    def __init__(self, kind: _Optional[_Union[ComparisonKind, str]] = ..., head: _Optional[str] = ..., base: _Optional[str] = ..., reference: _Optional[str] = ..., reference_commit: _Optional[str] = ..., unavailable: _Optional[_Union[ComparisonUnavailable, _Mapping]] = ..., merge_base: _Optional[str] = ..., base_display: _Optional[str] = ..., head_display: _Optional[str] = ..., head_is_working_copy: _Optional[bool] = ...) -> None: ...

class ResolveWorkingCopyBaseRequest(_message.Message):
    __slots__ = ("comparison", "repository")
    COMPARISON_FIELD_NUMBER: _ClassVar[int]
    REPOSITORY_FIELD_NUMBER: _ClassVar[int]
    comparison: Comparison
    repository: str
    def __init__(self, comparison: _Optional[_Union[Comparison, _Mapping]] = ..., repository: _Optional[str] = ...) -> None: ...

class ResolveWorkingCopyBaseResponse(_message.Message):
    __slots__ = ("base",)
    BASE_FIELD_NUMBER: _ClassVar[int]
    base: WorkingCopyBase
    def __init__(self, base: _Optional[_Union[WorkingCopyBase, _Mapping]] = ...) -> None: ...

class ChangedFile(_message.Message):
    __slots__ = ("path", "status", "base_kind", "working_kind", "base_object", "base_size", "lines_added", "lines_removed", "binary", "problem", "working", "committed")
    PATH_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    BASE_KIND_FIELD_NUMBER: _ClassVar[int]
    WORKING_KIND_FIELD_NUMBER: _ClassVar[int]
    BASE_OBJECT_FIELD_NUMBER: _ClassVar[int]
    BASE_SIZE_FIELD_NUMBER: _ClassVar[int]
    LINES_ADDED_FIELD_NUMBER: _ClassVar[int]
    LINES_REMOVED_FIELD_NUMBER: _ClassVar[int]
    BINARY_FIELD_NUMBER: _ClassVar[int]
    PROBLEM_FIELD_NUMBER: _ClassVar[int]
    WORKING_FIELD_NUMBER: _ClassVar[int]
    COMMITTED_FIELD_NUMBER: _ClassVar[int]
    path: str
    status: ChangeStatus
    base_kind: GitEntryKind
    working_kind: GitEntryKind
    base_object: str
    base_size: int
    lines_added: int
    lines_removed: int
    binary: bool
    problem: ChangedFileProblem
    working: WorkingFileMetadata
    committed: CommittedFile
    def __init__(self, path: _Optional[str] = ..., status: _Optional[_Union[ChangeStatus, str]] = ..., base_kind: _Optional[_Union[GitEntryKind, str]] = ..., working_kind: _Optional[_Union[GitEntryKind, str]] = ..., base_object: _Optional[str] = ..., base_size: _Optional[int] = ..., lines_added: _Optional[int] = ..., lines_removed: _Optional[int] = ..., binary: _Optional[bool] = ..., problem: _Optional[_Union[ChangedFileProblem, str]] = ..., working: _Optional[_Union[WorkingFileMetadata, _Mapping]] = ..., committed: _Optional[_Union[CommittedFile, _Mapping]] = ...) -> None: ...

class CommittedFile(_message.Message):
    __slots__ = ("object", "size")
    OBJECT_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    object: str
    size: int
    def __init__(self, object: _Optional[str] = ..., size: _Optional[int] = ...) -> None: ...

class WorkingFileMetadata(_message.Message):
    __slots__ = ("size", "modified_unix_nanos", "changed_unix_nanos", "inode", "device")
    SIZE_FIELD_NUMBER: _ClassVar[int]
    MODIFIED_UNIX_NANOS_FIELD_NUMBER: _ClassVar[int]
    CHANGED_UNIX_NANOS_FIELD_NUMBER: _ClassVar[int]
    INODE_FIELD_NUMBER: _ClassVar[int]
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    size: int
    modified_unix_nanos: int
    changed_unix_nanos: int
    inode: int
    device: int
    def __init__(self, size: _Optional[int] = ..., modified_unix_nanos: _Optional[int] = ..., changed_unix_nanos: _Optional[int] = ..., inode: _Optional[int] = ..., device: _Optional[int] = ...) -> None: ...

class ListWorkingCopyChangesRequest(_message.Message):
    __slots__ = ("comparison", "repository")
    COMPARISON_FIELD_NUMBER: _ClassVar[int]
    REPOSITORY_FIELD_NUMBER: _ClassVar[int]
    comparison: Comparison
    repository: str
    def __init__(self, comparison: _Optional[_Union[Comparison, _Mapping]] = ..., repository: _Optional[str] = ...) -> None: ...

class ListWorkingCopyChangesResponse(_message.Message):
    __slots__ = ("base", "files", "unrepresentable_names")
    BASE_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    UNREPRESENTABLE_NAMES_FIELD_NUMBER: _ClassVar[int]
    base: WorkingCopyBase
    files: _containers.RepeatedCompositeFieldContainer[ChangedFile]
    unrepresentable_names: int
    def __init__(self, base: _Optional[_Union[WorkingCopyBase, _Mapping]] = ..., files: _Optional[_Iterable[_Union[ChangedFile, _Mapping]]] = ..., unrepresentable_names: _Optional[int] = ...) -> None: ...

class ReadBaseFileRequest(_message.Message):
    __slots__ = ("commit", "path", "object", "maximum_bytes", "repository")
    COMMIT_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    OBJECT_FIELD_NUMBER: _ClassVar[int]
    MAXIMUM_BYTES_FIELD_NUMBER: _ClassVar[int]
    REPOSITORY_FIELD_NUMBER: _ClassVar[int]
    commit: str
    path: str
    object: str
    maximum_bytes: int
    repository: str
    def __init__(self, commit: _Optional[str] = ..., path: _Optional[str] = ..., object: _Optional[str] = ..., maximum_bytes: _Optional[int] = ..., repository: _Optional[str] = ...) -> None: ...

class ReadBaseFileResponse(_message.Message):
    __slots__ = ("metadata", "data")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    metadata: FileMetadata
    data: bytes
    def __init__(self, metadata: _Optional[_Union[FileMetadata, _Mapping]] = ..., data: _Optional[bytes] = ...) -> None: ...

class ReadLinkTextRequest(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class ReadLinkTextResponse(_message.Message):
    __slots__ = ("target",)
    TARGET_FIELD_NUMBER: _ClassVar[int]
    target: bytes
    def __init__(self, target: _Optional[bytes] = ...) -> None: ...

class ListRepositoryRefsRequest(_message.Message):
    __slots__ = ("repository",)
    REPOSITORY_FIELD_NUMBER: _ClassVar[int]
    repository: str
    def __init__(self, repository: _Optional[str] = ...) -> None: ...

class ListRepositoryRefsResponse(_message.Message):
    __slots__ = ("refs", "truncated", "default_branch_ref")
    REFS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_BRANCH_REF_FIELD_NUMBER: _ClassVar[int]
    refs: _containers.RepeatedScalarFieldContainer[str]
    truncated: bool
    default_branch_ref: str
    def __init__(self, refs: _Optional[_Iterable[str]] = ..., truncated: _Optional[bool] = ..., default_branch_ref: _Optional[str] = ...) -> None: ...

class ListBranchCommitsRequest(_message.Message):
    __slots__ = ("base_revision", "head_revision", "repository")
    BASE_REVISION_FIELD_NUMBER: _ClassVar[int]
    HEAD_REVISION_FIELD_NUMBER: _ClassVar[int]
    REPOSITORY_FIELD_NUMBER: _ClassVar[int]
    base_revision: str
    head_revision: str
    repository: str
    def __init__(self, base_revision: _Optional[str] = ..., head_revision: _Optional[str] = ..., repository: _Optional[str] = ...) -> None: ...

class BranchCommit(_message.Message):
    __slots__ = ("object", "subject", "author_name", "author_time_unix", "root", "body")
    OBJECT_FIELD_NUMBER: _ClassVar[int]
    SUBJECT_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_NAME_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_TIME_UNIX_FIELD_NUMBER: _ClassVar[int]
    ROOT_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    object: str
    subject: str
    author_name: str
    author_time_unix: int
    root: bool
    body: str
    def __init__(self, object: _Optional[str] = ..., subject: _Optional[str] = ..., author_name: _Optional[str] = ..., author_time_unix: _Optional[int] = ..., root: _Optional[bool] = ..., body: _Optional[str] = ...) -> None: ...

class ListBranchCommitsResponse(_message.Message):
    __slots__ = ("base", "commits", "truncated")
    BASE_FIELD_NUMBER: _ClassVar[int]
    COMMITS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    base: WorkingCopyBase
    commits: _containers.RepeatedCompositeFieldContainer[BranchCommit]
    truncated: bool
    def __init__(self, base: _Optional[_Union[WorkingCopyBase, _Mapping]] = ..., commits: _Optional[_Iterable[_Union[BranchCommit, _Mapping]]] = ..., truncated: _Optional[bool] = ...) -> None: ...

class ResolveRepositoryContextRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class RepositoryRemote(_message.Message):
    __slots__ = ("name", "host", "owner", "repo")
    NAME_FIELD_NUMBER: _ClassVar[int]
    HOST_FIELD_NUMBER: _ClassVar[int]
    OWNER_FIELD_NUMBER: _ClassVar[int]
    REPO_FIELD_NUMBER: _ClassVar[int]
    name: str
    host: str
    owner: str
    repo: str
    def __init__(self, name: _Optional[str] = ..., host: _Optional[str] = ..., owner: _Optional[str] = ..., repo: _Optional[str] = ...) -> None: ...

class PushTarget(_message.Message):
    __slots__ = ("remote_name", "branch", "host", "owner", "repo", "tracking_sha")
    REMOTE_NAME_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    HOST_FIELD_NUMBER: _ClassVar[int]
    OWNER_FIELD_NUMBER: _ClassVar[int]
    REPO_FIELD_NUMBER: _ClassVar[int]
    TRACKING_SHA_FIELD_NUMBER: _ClassVar[int]
    remote_name: str
    branch: str
    host: str
    owner: str
    repo: str
    tracking_sha: str
    def __init__(self, remote_name: _Optional[str] = ..., branch: _Optional[str] = ..., host: _Optional[str] = ..., owner: _Optional[str] = ..., repo: _Optional[str] = ..., tracking_sha: _Optional[str] = ...) -> None: ...

class ResolveRepositoryContextResponse(_message.Message):
    __slots__ = ("problem", "branch", "head_sha", "push", "push_unresolved", "upstream_ref", "remotes", "unsupported_remote_count", "truncated_remotes", "workspace_prefix")
    PROBLEM_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    HEAD_SHA_FIELD_NUMBER: _ClassVar[int]
    PUSH_FIELD_NUMBER: _ClassVar[int]
    PUSH_UNRESOLVED_FIELD_NUMBER: _ClassVar[int]
    UPSTREAM_REF_FIELD_NUMBER: _ClassVar[int]
    REMOTES_FIELD_NUMBER: _ClassVar[int]
    UNSUPPORTED_REMOTE_COUNT_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_REMOTES_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_PREFIX_FIELD_NUMBER: _ClassVar[int]
    problem: RepositoryContextProblem
    branch: str
    head_sha: str
    push: PushTarget
    push_unresolved: PushUnresolved
    upstream_ref: str
    remotes: _containers.RepeatedCompositeFieldContainer[RepositoryRemote]
    unsupported_remote_count: int
    truncated_remotes: bool
    workspace_prefix: str
    def __init__(self, problem: _Optional[_Union[RepositoryContextProblem, str]] = ..., branch: _Optional[str] = ..., head_sha: _Optional[str] = ..., push: _Optional[_Union[PushTarget, _Mapping]] = ..., push_unresolved: _Optional[_Union[PushUnresolved, str]] = ..., upstream_ref: _Optional[str] = ..., remotes: _Optional[_Iterable[_Union[RepositoryRemote, _Mapping]]] = ..., unsupported_remote_count: _Optional[int] = ..., truncated_remotes: _Optional[bool] = ..., workspace_prefix: _Optional[str] = ...) -> None: ...

class GetPullRequestRequest(_message.Message):
    __slots__ = ("pr_url",)
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    def __init__(self, pr_url: _Optional[str] = ...) -> None: ...

class GetPullRequestResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ListPullRequestFilesRequest(_message.Message):
    __slots__ = ("pr_url", "head_sha", "base_sha")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    HEAD_SHA_FIELD_NUMBER: _ClassVar[int]
    BASE_SHA_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    head_sha: str
    base_sha: str
    def __init__(self, pr_url: _Optional[str] = ..., head_sha: _Optional[str] = ..., base_sha: _Optional[str] = ...) -> None: ...

class ListPullRequestFilesResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class GetPullRequestDiffRequest(_message.Message):
    __slots__ = ("pr_url", "head_sha", "base_sha", "path", "previous_path", "base")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    HEAD_SHA_FIELD_NUMBER: _ClassVar[int]
    BASE_SHA_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_PATH_FIELD_NUMBER: _ClassVar[int]
    BASE_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    head_sha: str
    base_sha: str
    path: str
    previous_path: str
    base: str
    def __init__(self, pr_url: _Optional[str] = ..., head_sha: _Optional[str] = ..., base_sha: _Optional[str] = ..., path: _Optional[str] = ..., previous_path: _Optional[str] = ..., base: _Optional[str] = ...) -> None: ...

class GetPullRequestDiffResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ListPullRequestCommitsRequest(_message.Message):
    __slots__ = ("pr_url", "head_sha")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    HEAD_SHA_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    head_sha: str
    def __init__(self, pr_url: _Optional[str] = ..., head_sha: _Optional[str] = ...) -> None: ...

class ListPullRequestCommitsResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class GetCommitDiffRequest(_message.Message):
    __slots__ = ("pr_url", "commit_sha")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    COMMIT_SHA_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    commit_sha: str
    def __init__(self, pr_url: _Optional[str] = ..., commit_sha: _Optional[str] = ...) -> None: ...

class GetCommitDiffResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ComparePullRequestRangeRequest(_message.Message):
    __slots__ = ("pr_url", "head_sha", "from_sha")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    HEAD_SHA_FIELD_NUMBER: _ClassVar[int]
    FROM_SHA_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    head_sha: str
    from_sha: str
    def __init__(self, pr_url: _Optional[str] = ..., head_sha: _Optional[str] = ..., from_sha: _Optional[str] = ...) -> None: ...

class ComparePullRequestRangeResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ListReviewThreadsRequest(_message.Message):
    __slots__ = ("pr_url", "page_token", "head_sha", "base_sha")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    HEAD_SHA_FIELD_NUMBER: _ClassVar[int]
    BASE_SHA_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    page_token: str
    head_sha: str
    base_sha: str
    def __init__(self, pr_url: _Optional[str] = ..., page_token: _Optional[str] = ..., head_sha: _Optional[str] = ..., base_sha: _Optional[str] = ...) -> None: ...

class ListReviewThreadsResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ListReviewThreadCommentsRequest(_message.Message):
    __slots__ = ("pr_url", "thread_id", "page_token")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    thread_id: str
    page_token: str
    def __init__(self, pr_url: _Optional[str] = ..., thread_id: _Optional[str] = ..., page_token: _Optional[str] = ...) -> None: ...

class ListReviewThreadCommentsResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ListLabelsRequest(_message.Message):
    __slots__ = ("pr_url", "text")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    text: str
    def __init__(self, pr_url: _Optional[str] = ..., text: _Optional[str] = ...) -> None: ...

class ListLabelsResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ListReviewerCandidatesRequest(_message.Message):
    __slots__ = ("pr_url",)
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    def __init__(self, pr_url: _Optional[str] = ...) -> None: ...

class ListReviewerCandidatesResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class GetMergeOptionsRequest(_message.Message):
    __slots__ = ("pr_url",)
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    def __init__(self, pr_url: _Optional[str] = ...) -> None: ...

class GetMergeOptionsResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class CreateCommentRequest(_message.Message):
    __slots__ = ("pr_url", "body")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    body: str
    def __init__(self, pr_url: _Optional[str] = ..., body: _Optional[str] = ...) -> None: ...

class CreateCommentResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ReviewThreadAnchor(_message.Message):
    __slots__ = ("path", "side", "line", "start_line", "head_sha", "base_sha")
    PATH_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    START_LINE_FIELD_NUMBER: _ClassVar[int]
    HEAD_SHA_FIELD_NUMBER: _ClassVar[int]
    BASE_SHA_FIELD_NUMBER: _ClassVar[int]
    path: str
    side: str
    line: int
    start_line: int
    head_sha: str
    base_sha: str
    def __init__(self, path: _Optional[str] = ..., side: _Optional[str] = ..., line: _Optional[int] = ..., start_line: _Optional[int] = ..., head_sha: _Optional[str] = ..., base_sha: _Optional[str] = ...) -> None: ...

class CreateReviewThreadRequest(_message.Message):
    __slots__ = ("pr_url", "body", "anchor")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    body: str
    anchor: ReviewThreadAnchor
    def __init__(self, pr_url: _Optional[str] = ..., body: _Optional[str] = ..., anchor: _Optional[_Union[ReviewThreadAnchor, _Mapping]] = ...) -> None: ...

class CreateReviewThreadResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ReplyToReviewThreadRequest(_message.Message):
    __slots__ = ("pr_url", "thread_id", "body")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    thread_id: str
    body: str
    def __init__(self, pr_url: _Optional[str] = ..., thread_id: _Optional[str] = ..., body: _Optional[str] = ...) -> None: ...

class ReplyToReviewThreadResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class UpdatePullRequestDetailsRequest(_message.Message):
    __slots__ = ("pr_url", "title", "body")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    title: str
    body: str
    def __init__(self, pr_url: _Optional[str] = ..., title: _Optional[str] = ..., body: _Optional[str] = ...) -> None: ...

class UpdatePullRequestDetailsResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class UpdateLabelsRequest(_message.Message):
    __slots__ = ("pr_url", "add", "remove")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    ADD_FIELD_NUMBER: _ClassVar[int]
    REMOVE_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    add: _containers.RepeatedScalarFieldContainer[str]
    remove: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, pr_url: _Optional[str] = ..., add: _Optional[_Iterable[str]] = ..., remove: _Optional[_Iterable[str]] = ...) -> None: ...

class UpdateLabelsResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class Reviewer(_message.Message):
    __slots__ = ("login", "kind")
    LOGIN_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    login: str
    kind: str
    def __init__(self, login: _Optional[str] = ..., kind: _Optional[str] = ...) -> None: ...

class UpdateReviewersRequest(_message.Message):
    __slots__ = ("pr_url", "add", "remove")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    ADD_FIELD_NUMBER: _ClassVar[int]
    REMOVE_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    add: _containers.RepeatedCompositeFieldContainer[Reviewer]
    remove: _containers.RepeatedCompositeFieldContainer[Reviewer]
    def __init__(self, pr_url: _Optional[str] = ..., add: _Optional[_Iterable[_Union[Reviewer, _Mapping]]] = ..., remove: _Optional[_Iterable[_Union[Reviewer, _Mapping]]] = ...) -> None: ...

class UpdateReviewersResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SetPullRequestAccountRequest(_message.Message):
    __slots__ = ("pr_url", "account", "remote")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    REMOTE_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    account: str
    remote: str
    def __init__(self, pr_url: _Optional[str] = ..., account: _Optional[str] = ..., remote: _Optional[str] = ...) -> None: ...

class SetPullRequestAccountResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class MergePullRequestRequest(_message.Message):
    __slots__ = ("pr_url", "merge_method", "expected_head_oid")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    MERGE_METHOD_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_HEAD_OID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    merge_method: str
    expected_head_oid: str
    def __init__(self, pr_url: _Optional[str] = ..., merge_method: _Optional[str] = ..., expected_head_oid: _Optional[str] = ...) -> None: ...

class MergePullRequestResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class MergePullRequestAsAccountRequest(_message.Message):
    __slots__ = ("pr_url", "merge_method", "expected_head_oid", "account")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    MERGE_METHOD_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_HEAD_OID_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    merge_method: str
    expected_head_oid: str
    account: str
    def __init__(self, pr_url: _Optional[str] = ..., merge_method: _Optional[str] = ..., expected_head_oid: _Optional[str] = ..., account: _Optional[str] = ...) -> None: ...

class MergePullRequestAsAccountResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class DisableAutoMergeRequest(_message.Message):
    __slots__ = ("pr_url",)
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    def __init__(self, pr_url: _Optional[str] = ...) -> None: ...

class DisableAutoMergeResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class GetPullRequestListStatsRequest(_message.Message):
    __slots__ = ("pr_urls",)
    PR_URLS_FIELD_NUMBER: _ClassVar[int]
    pr_urls: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, pr_urls: _Optional[_Iterable[str]] = ...) -> None: ...

class GetPullRequestListStatsResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class LookupPullRequestRequest(_message.Message):
    __slots__ = ("pr_url",)
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    def __init__(self, pr_url: _Optional[str] = ...) -> None: ...

class LookupPullRequestResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ListBranchPullRequestsRequest(_message.Message):
    __slots__ = ("branch",)
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    branch: str
    def __init__(self, branch: _Optional[str] = ...) -> None: ...

class ListBranchPullRequestsResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SearchReviewersRequest(_message.Message):
    __slots__ = ("pr_url", "text", "page_size")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    text: str
    page_size: int
    def __init__(self, pr_url: _Optional[str] = ..., text: _Optional[str] = ..., page_size: _Optional[int] = ...) -> None: ...

class SearchReviewersResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SearchCompletionsRequest(_message.Message):
    __slots__ = ("pr_url", "kind", "text")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    kind: CompletionKind
    text: str
    def __init__(self, pr_url: _Optional[str] = ..., kind: _Optional[_Union[CompletionKind, str]] = ..., text: _Optional[str] = ...) -> None: ...

class SearchCompletionsResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class UploadAttachmentRequest(_message.Message):
    __slots__ = ("pr_url", "name", "content")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    name: str
    content: bytes
    def __init__(self, pr_url: _Optional[str] = ..., name: _Optional[str] = ..., content: _Optional[bytes] = ...) -> None: ...

class UploadAttachmentResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SubmitReviewRequest(_message.Message):
    __slots__ = ("pr_url", "event", "body", "commit_oid")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    COMMIT_OID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    event: str
    body: str
    commit_oid: str
    def __init__(self, pr_url: _Optional[str] = ..., event: _Optional[str] = ..., body: _Optional[str] = ..., commit_oid: _Optional[str] = ...) -> None: ...

class SubmitReviewResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SubmitReviewVerdictRequest(_message.Message):
    __slots__ = ("pr_url", "event", "body", "commit_oid")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    COMMIT_OID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    event: str
    body: str
    commit_oid: str
    def __init__(self, pr_url: _Optional[str] = ..., event: _Optional[str] = ..., body: _Optional[str] = ..., commit_oid: _Optional[str] = ...) -> None: ...

class SubmitReviewVerdictResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class GetPendingReviewRequest(_message.Message):
    __slots__ = ("pr_url",)
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    def __init__(self, pr_url: _Optional[str] = ...) -> None: ...

class GetPendingReviewResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class StartPendingReviewRequest(_message.Message):
    __slots__ = ("pr_url", "commit_oid")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    COMMIT_OID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    commit_oid: str
    def __init__(self, pr_url: _Optional[str] = ..., commit_oid: _Optional[str] = ...) -> None: ...

class StartPendingReviewResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class AddPendingReviewCommentRequest(_message.Message):
    __slots__ = ("pr_url", "body", "anchor", "in_reply_to_thread_id", "pending_review_id", "expected_head_oid")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_FIELD_NUMBER: _ClassVar[int]
    IN_REPLY_TO_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    PENDING_REVIEW_ID_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_HEAD_OID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    body: str
    anchor: ReviewThreadAnchor
    in_reply_to_thread_id: str
    pending_review_id: str
    expected_head_oid: str
    def __init__(self, pr_url: _Optional[str] = ..., body: _Optional[str] = ..., anchor: _Optional[_Union[ReviewThreadAnchor, _Mapping]] = ..., in_reply_to_thread_id: _Optional[str] = ..., pending_review_id: _Optional[str] = ..., expected_head_oid: _Optional[str] = ...) -> None: ...

class AddPendingReviewCommentResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class UpdatePendingReviewCommentRequest(_message.Message):
    __slots__ = ("pr_url", "comment_node_id", "body", "expected_body_sha256", "pending_review_id")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    COMMENT_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_BODY_SHA256_FIELD_NUMBER: _ClassVar[int]
    PENDING_REVIEW_ID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    comment_node_id: str
    body: str
    expected_body_sha256: str
    pending_review_id: str
    def __init__(self, pr_url: _Optional[str] = ..., comment_node_id: _Optional[str] = ..., body: _Optional[str] = ..., expected_body_sha256: _Optional[str] = ..., pending_review_id: _Optional[str] = ...) -> None: ...

class UpdatePendingReviewCommentResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class DeletePendingReviewCommentRequest(_message.Message):
    __slots__ = ("pr_url", "comment_node_id", "pending_review_id")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    COMMENT_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    PENDING_REVIEW_ID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    comment_node_id: str
    pending_review_id: str
    def __init__(self, pr_url: _Optional[str] = ..., comment_node_id: _Optional[str] = ..., pending_review_id: _Optional[str] = ...) -> None: ...

class DeletePendingReviewCommentResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SubmitPendingReviewRequest(_message.Message):
    __slots__ = ("pr_url", "event", "body", "commit_oid", "pending_review_id")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    COMMIT_OID_FIELD_NUMBER: _ClassVar[int]
    PENDING_REVIEW_ID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    event: str
    body: str
    commit_oid: str
    pending_review_id: str
    def __init__(self, pr_url: _Optional[str] = ..., event: _Optional[str] = ..., body: _Optional[str] = ..., commit_oid: _Optional[str] = ..., pending_review_id: _Optional[str] = ...) -> None: ...

class SubmitPendingReviewResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class DiscardPendingReviewRequest(_message.Message):
    __slots__ = ("pr_url", "pending_review_id")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    PENDING_REVIEW_ID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    pending_review_id: str
    def __init__(self, pr_url: _Optional[str] = ..., pending_review_id: _Optional[str] = ...) -> None: ...

class DiscardPendingReviewResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SetPullRequestStateRequest(_message.Message):
    __slots__ = ("pr_url", "state")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    state: str
    def __init__(self, pr_url: _Optional[str] = ..., state: _Optional[str] = ...) -> None: ...

class SetPullRequestStateResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SetPullRequestDraftRequest(_message.Message):
    __slots__ = ("pr_url", "draft")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    DRAFT_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    draft: bool
    def __init__(self, pr_url: _Optional[str] = ..., draft: _Optional[bool] = ...) -> None: ...

class SetPullRequestDraftResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class PullRequestAutoMerge(_message.Message):
    __slots__ = ("pr_url", "merge_method", "expected_head_oid")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    MERGE_METHOD_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_HEAD_OID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    merge_method: str
    expected_head_oid: str
    def __init__(self, pr_url: _Optional[str] = ..., merge_method: _Optional[str] = ..., expected_head_oid: _Optional[str] = ...) -> None: ...

class EnableAutoMergeRequest(_message.Message):
    __slots__ = ("pull_requests",)
    PULL_REQUESTS_FIELD_NUMBER: _ClassVar[int]
    pull_requests: _containers.RepeatedCompositeFieldContainer[PullRequestAutoMerge]
    def __init__(self, pull_requests: _Optional[_Iterable[_Union[PullRequestAutoMerge, _Mapping]]] = ...) -> None: ...

class EnableAutoMergeResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class EnqueuePullRequestRequest(_message.Message):
    __slots__ = ("pr_url", "expected_head_oid")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_HEAD_OID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    expected_head_oid: str
    def __init__(self, pr_url: _Optional[str] = ..., expected_head_oid: _Optional[str] = ...) -> None: ...

class EnqueuePullRequestResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class DequeuePullRequestRequest(_message.Message):
    __slots__ = ("pr_url",)
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    def __init__(self, pr_url: _Optional[str] = ...) -> None: ...

class DequeuePullRequestResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class GetMergeQueueRequest(_message.Message):
    __slots__ = ("pr_url",)
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    def __init__(self, pr_url: _Optional[str] = ...) -> None: ...

class GetMergeQueueResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SetReviewThreadResolvedRequest(_message.Message):
    __slots__ = ("pr_url", "thread_id", "resolved")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    RESOLVED_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    thread_id: str
    resolved: bool
    def __init__(self, pr_url: _Optional[str] = ..., thread_id: _Optional[str] = ..., resolved: _Optional[bool] = ...) -> None: ...

class SetReviewThreadResolvedResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class EditCommentRequest(_message.Message):
    __slots__ = ("pr_url", "kind", "comment_id", "body", "expected_body_sha256")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    COMMENT_ID_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_BODY_SHA256_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    kind: str
    comment_id: int
    body: str
    expected_body_sha256: str
    def __init__(self, pr_url: _Optional[str] = ..., kind: _Optional[str] = ..., comment_id: _Optional[int] = ..., body: _Optional[str] = ..., expected_body_sha256: _Optional[str] = ...) -> None: ...

class EditCommentResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class DeleteCommentRequest(_message.Message):
    __slots__ = ("pr_url", "kind", "comment_id")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    COMMENT_ID_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    kind: str
    comment_id: int
    def __init__(self, pr_url: _Optional[str] = ..., kind: _Optional[str] = ..., comment_id: _Optional[int] = ...) -> None: ...

class DeleteCommentResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class AddReactionRequest(_message.Message):
    __slots__ = ("pr_url", "kind", "comment_id", "content")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    COMMENT_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    kind: str
    comment_id: int
    content: str
    def __init__(self, pr_url: _Optional[str] = ..., kind: _Optional[str] = ..., comment_id: _Optional[int] = ..., content: _Optional[str] = ...) -> None: ...

class AddReactionResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class RemoveReactionRequest(_message.Message):
    __slots__ = ("pr_url", "kind", "comment_id", "content")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    COMMENT_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    kind: str
    comment_id: int
    content: str
    def __init__(self, pr_url: _Optional[str] = ..., kind: _Optional[str] = ..., comment_id: _Optional[int] = ..., content: _Optional[str] = ...) -> None: ...

class RemoveReactionResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class GetPullRequestStackRequest(_message.Message):
    __slots__ = ("pr_url",)
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    def __init__(self, pr_url: _Optional[str] = ...) -> None: ...

class GetPullRequestStackResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ReadPullRequestImageRequest(_message.Message):
    __slots__ = ("pr_url", "image_url")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    IMAGE_URL_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    image_url: str
    def __init__(self, pr_url: _Optional[str] = ..., image_url: _Optional[str] = ...) -> None: ...

class GetMergeReadinessRequest(_message.Message):
    __slots__ = ("pr_url", "head_sha")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    HEAD_SHA_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    head_sha: str
    def __init__(self, pr_url: _Optional[str] = ..., head_sha: _Optional[str] = ...) -> None: ...

class GetMergeReadinessResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class PreviewReviewerPingRequest(_message.Message):
    __slots__ = ("pr_url", "reviewer")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    REVIEWER_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    reviewer: str
    def __init__(self, pr_url: _Optional[str] = ..., reviewer: _Optional[str] = ...) -> None: ...

class PreviewReviewerPingResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SendReviewerPingRequest(_message.Message):
    __slots__ = ("pr_url", "preview_token")
    PR_URL_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_TOKEN_FIELD_NUMBER: _ClassVar[int]
    pr_url: str
    preview_token: str
    def __init__(self, pr_url: _Optional[str] = ..., preview_token: _Optional[str] = ...) -> None: ...

class SendReviewerPingResponse(_message.Message):
    __slots__ = ("payload",)
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    payload: _struct_pb2.Struct
    def __init__(self, payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class PullRequestImageMetadata(_message.Message):
    __slots__ = ("content_type", "size")
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    content_type: str
    size: int
    def __init__(self, content_type: _Optional[str] = ..., size: _Optional[int] = ...) -> None: ...

class ReadPullRequestImageResponse(_message.Message):
    __slots__ = ("metadata", "data")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    metadata: PullRequestImageMetadata
    data: bytes
    def __init__(self, metadata: _Optional[_Union[PullRequestImageMetadata, _Mapping]] = ..., data: _Optional[bytes] = ...) -> None: ...

import datetime

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
    __slots__ = ("comparison",)
    COMPARISON_FIELD_NUMBER: _ClassVar[int]
    comparison: Comparison
    def __init__(self, comparison: _Optional[_Union[Comparison, _Mapping]] = ...) -> None: ...

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
    __slots__ = ("comparison",)
    COMPARISON_FIELD_NUMBER: _ClassVar[int]
    comparison: Comparison
    def __init__(self, comparison: _Optional[_Union[Comparison, _Mapping]] = ...) -> None: ...

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
    __slots__ = ("commit", "path", "object", "maximum_bytes")
    COMMIT_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    OBJECT_FIELD_NUMBER: _ClassVar[int]
    MAXIMUM_BYTES_FIELD_NUMBER: _ClassVar[int]
    commit: str
    path: str
    object: str
    maximum_bytes: int
    def __init__(self, commit: _Optional[str] = ..., path: _Optional[str] = ..., object: _Optional[str] = ..., maximum_bytes: _Optional[int] = ...) -> None: ...

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
    __slots__ = ()
    def __init__(self) -> None: ...

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
    __slots__ = ("base_revision",)
    BASE_REVISION_FIELD_NUMBER: _ClassVar[int]
    base_revision: str
    def __init__(self, base_revision: _Optional[str] = ...) -> None: ...

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
    __slots__ = ("problem", "branch", "head_sha", "push", "push_unresolved", "upstream_ref", "remotes", "unsupported_remote_count", "truncated_remotes")
    PROBLEM_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    HEAD_SHA_FIELD_NUMBER: _ClassVar[int]
    PUSH_FIELD_NUMBER: _ClassVar[int]
    PUSH_UNRESOLVED_FIELD_NUMBER: _ClassVar[int]
    UPSTREAM_REF_FIELD_NUMBER: _ClassVar[int]
    REMOTES_FIELD_NUMBER: _ClassVar[int]
    UNSUPPORTED_REMOTE_COUNT_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_REMOTES_FIELD_NUMBER: _ClassVar[int]
    problem: RepositoryContextProblem
    branch: str
    head_sha: str
    push: PushTarget
    push_unresolved: PushUnresolved
    upstream_ref: str
    remotes: _containers.RepeatedCompositeFieldContainer[RepositoryRemote]
    unsupported_remote_count: int
    truncated_remotes: bool
    def __init__(self, problem: _Optional[_Union[RepositoryContextProblem, str]] = ..., branch: _Optional[str] = ..., head_sha: _Optional[str] = ..., push: _Optional[_Union[PushTarget, _Mapping]] = ..., push_unresolved: _Optional[_Union[PushUnresolved, str]] = ..., upstream_ref: _Optional[str] = ..., remotes: _Optional[_Iterable[_Union[RepositoryRemote, _Mapping]]] = ..., unsupported_remote_count: _Optional[int] = ..., truncated_remotes: _Optional[bool] = ...) -> None: ...

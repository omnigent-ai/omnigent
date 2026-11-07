"""Terminal-specific components: RichBlockFormatter and TerminalHost."""

from ._completer import FileMentionCompleter, extract_at_mentions, strip_at_mentions
from ._config import (
    DEFAULT_USER_CONFIG,
    UserConfig,
    UserConfigError,
    load_user_config,
    save_user_config,
    state_dir,
    update_user_config,
    user_config_path,
)
from ._formatter import RichBlockFormatter, StreamingText, StreamLive
from ._host import (
    Overlay,
    OverlayAction,
    OverlayTarget,
    PendingAttachment,
    TerminalHost,
)
from ._questions import QuestionSpec
from ._theme import DARK_THEME, LIGHT_THEME, THEMES, TerminalTheme, TerminalThemeName

__all__ = [
    "DARK_THEME",
    "DEFAULT_USER_CONFIG",
    "LIGHT_THEME",
    "THEMES",
    "FileMentionCompleter",
    "Overlay",
    "OverlayAction",
    "OverlayTarget",
    "PendingAttachment",
    "QuestionSpec",
    "RichBlockFormatter",
    "StreamLive",
    "StreamingText",
    "TerminalHost",
    "TerminalTheme",
    "TerminalThemeName",
    "UserConfig",
    "UserConfigError",
    "extract_at_mentions",
    "load_user_config",
    "save_user_config",
    "state_dir",
    "strip_at_mentions",
    "update_user_config",
    "user_config_path",
]

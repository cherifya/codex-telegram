"""Codex exception compatibility exports."""

from ..claude.exceptions import ClaudeError as CodexError  # noqa: F401
from ..claude.exceptions import ClaudeMCPError as CodexMCPError  # noqa: F401
from ..claude.exceptions import ClaudeParsingError as CodexParsingError  # noqa: F401
from ..claude.exceptions import ClaudeProcessError as CodexProcessError  # noqa: F401
from ..claude.exceptions import ClaudeSessionError as CodexSessionError  # noqa: F401
from ..claude.exceptions import ClaudeTimeoutError as CodexTimeoutError  # noqa: F401

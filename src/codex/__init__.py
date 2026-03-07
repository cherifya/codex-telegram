"""Codex compatibility package.

Re-exports the active integration implementation from src.claude while the
codebase transitions naming and interfaces.
"""

from ..claude import ClaudeError as CodexError  # noqa: F401
from ..claude import ClaudeIntegration as CodexIntegration
from ..claude import ClaudeMCPError as CodexMCPError
from ..claude import ClaudeParsingError as CodexParsingError
from ..claude import ClaudeProcessError as CodexProcessError
from ..claude import ClaudeResponse as CodexResponse
from ..claude import ClaudeSDKManager as CodexSDKManager
from ..claude import ClaudeSession as CodexSession
from ..claude import ClaudeSessionError as CodexSessionError
from ..claude import ClaudeTimeoutError as CodexTimeoutError
from ..claude import (
    SessionManager,
    StreamUpdate,
)

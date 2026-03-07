"""Legacy Claude SDK tests.

The runtime backend is now Codex CLI. These Claude-agent-sdk specific tests are
kept as an explicit module-level skip during migration.
"""

import pytest

pytest.skip(
    "Legacy Claude SDK tests are not applicable after Codex backend migration.",
    allow_module_level=True,
)

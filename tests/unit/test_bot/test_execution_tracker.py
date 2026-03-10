"""Tests for execution tracking and per-scope locking."""

from unittest.mock import MagicMock

from src.bot.execution_tracker import ExecutionTracker, scope_key_from_update
from src.claude.sdk_integration import StreamUpdate


def test_scope_key_from_update_private_chat() -> None:
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = 456
    update.effective_chat.is_forum = False
    update.effective_message = MagicMock()
    update.effective_message.message_thread_id = None

    key = scope_key_from_update(update)
    assert key == "123:456:0"


def test_tracker_start_stream_finish_lifecycle() -> None:
    tracker = ExecutionTracker()
    scope_key = "1:2:0"

    tracker.start(
        scope_key,
        prompt="Investigate scheduling issue",
        working_directory="/tmp/project",
        session_id="thread-abc",
    )
    tracker.record_stream(
        scope_key,
        StreamUpdate(type="assistant", tool_calls=[{"name": "Read", "input": {}}]),
    )
    tracker.record_stream(
        scope_key,
        StreamUpdate(type="stream_delta", content="Looking into cron config now."),
    )

    running = tracker.get_snapshot(scope_key)
    assert running.running is True
    assert running.prompt_preview.startswith("Investigate scheduling issue")
    assert running.last_tool == "Read"
    assert running.tool_calls == 1
    assert "cron config" in running.last_stream_preview

    tracker.finish(scope_key, session_id="thread-def")
    done = tracker.get_snapshot(scope_key)
    assert done.running is False
    assert done.session_id == "thread-def"


def test_tracker_lock_reused_per_scope() -> None:
    tracker = ExecutionTracker()
    lock_a = tracker.get_lock("1:2:0")
    lock_b = tracker.get_lock("1:2:0")
    assert lock_a is lock_b

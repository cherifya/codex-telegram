"""Track in-flight Codex executions per Telegram conversation scope."""

import asyncio
import time
from dataclasses import dataclass, replace
from typing import Dict, Optional

from telegram import Update

from ..claude.sdk_integration import StreamUpdate


def _preview(text: str, max_len: int = 160) -> str:
    """Return a compact single-line preview."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1] + "\u2026"


def _tail_ellipsize(text: str, max_len: int = 220) -> str:
    """Keep the tail of text within *max_len* characters."""
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return "\u2026"
    return "\u2026" + text[-(max_len - 1) :]


def resolve_thread_id(update: Update) -> int:
    """Resolve Telegram thread id; use 1 for forum General topic."""
    message = update.effective_message
    thread_id = getattr(message, "message_thread_id", None) if message else None
    if isinstance(thread_id, int) and thread_id > 0:
        return thread_id

    chat = update.effective_chat
    if chat and getattr(chat, "is_forum", False):
        return 1
    return 0


def build_scope_key(user_id: int, chat_id: int, thread_id: int = 0) -> str:
    """Build a unique scope key for a user+chat+thread conversation."""
    return f"{user_id}:{chat_id}:{thread_id}"


def scope_key_from_update(update: Update) -> Optional[str]:
    """Build scope key directly from Telegram update."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return None
    return build_scope_key(user.id, chat.id, resolve_thread_id(update))


@dataclass
class ExecutionSnapshot:
    """Current and recent execution state for one conversation scope."""

    running: bool = False
    started_monotonic: Optional[float] = None
    started_at_epoch: Optional[float] = None
    updated_at_epoch: Optional[float] = None
    prompt_preview: str = ""
    working_directory: str = ""
    session_id: Optional[str] = None
    tool_calls: int = 0
    last_tool: str = ""
    last_stream_preview: str = ""
    last_stream_at_epoch: Optional[float] = None
    last_error: Optional[str] = None

    def elapsed_seconds(self) -> Optional[int]:
        """Return current elapsed runtime when running."""
        if not self.running or self.started_monotonic is None:
            return None
        return max(0, int(time.monotonic() - self.started_monotonic))


class ExecutionTracker:
    """Tracks active Codex work and provides per-scope concurrency locks."""

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}
        self._state: Dict[str, ExecutionSnapshot] = {}

    def get_lock(self, scope_key: str) -> asyncio.Lock:
        """Get (or create) per-scope lock."""
        lock = self._locks.get(scope_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[scope_key] = lock
        return lock

    def is_scope_busy(self, scope_key: str) -> bool:
        """Return True when a scope is currently executing."""
        snap = self._state.get(scope_key)
        return bool(snap and snap.running)

    def start(
        self,
        scope_key: str,
        *,
        prompt: str,
        working_directory: str,
        session_id: Optional[str],
    ) -> None:
        """Mark the beginning of an execution."""
        now_epoch = time.time()
        self._state[scope_key] = ExecutionSnapshot(
            running=True,
            started_monotonic=time.monotonic(),
            started_at_epoch=now_epoch,
            updated_at_epoch=now_epoch,
            prompt_preview=_preview(prompt),
            working_directory=str(working_directory),
            session_id=session_id,
            tool_calls=0,
            last_tool="",
            last_stream_preview="",
            last_stream_at_epoch=None,
            last_error=None,
        )

    def record_stream(self, scope_key: str, update_obj: StreamUpdate) -> None:
        """Record stream updates (tool activity + text deltas)."""
        snap = self._state.get(scope_key)
        if not snap or not snap.running:
            return

        now_epoch = time.time()
        snap.updated_at_epoch = now_epoch

        if update_obj.tool_calls:
            tool_names = [
                str(tc.get("name", "")).strip()
                for tc in update_obj.tool_calls
                if isinstance(tc, dict)
            ]
            tool_names = [name for name in tool_names if name]
            if tool_names:
                snap.tool_calls += len(tool_names)
                snap.last_tool = tool_names[-1]

        if update_obj.type == "stream_delta" and update_obj.content:
            chunk = update_obj.content
            if chunk:
                snap.last_stream_preview = _tail_ellipsize(
                    snap.last_stream_preview + chunk
                )
                snap.last_stream_at_epoch = now_epoch

    def finish(
        self,
        scope_key: str,
        *,
        session_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Mark completion (success or error) of an execution."""
        snap = self._state.get(scope_key)
        if snap is None:
            snap = ExecutionSnapshot()
            self._state[scope_key] = snap

        snap.running = False
        snap.updated_at_epoch = time.time()
        if session_id:
            snap.session_id = session_id
        if error:
            snap.last_error = _preview(error, max_len=220)

    def get_snapshot(self, scope_key: str) -> ExecutionSnapshot:
        """Return a copy of current scope snapshot (or empty snapshot)."""
        snap = self._state.get(scope_key)
        if snap is None:
            return ExecutionSnapshot()
        return replace(snap)

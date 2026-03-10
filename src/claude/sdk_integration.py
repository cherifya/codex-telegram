"""Claude integration interface backed by Codex CLI JSON output.

This module preserves the existing Claude* class/API surface so the rest of the
application can stay stable while switching execution to Codex CLI.
"""

import asyncio
import glob
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from ..security.validators import SecurityValidator
from .exceptions import ClaudeMCPError, ClaudeProcessError, ClaudeTimeoutError
from .monitor import check_bash_directory_boundary

logger = structlog.get_logger()

# Default asyncio StreamReader limit is 64 KiB, which can fail on large
# single-line JSON events/tool output with:
# "Separator is not found, and chunk exceed the limit".
_SUBPROCESS_STREAM_LIMIT = 8 * 1024 * 1024  # 8 MiB


@dataclass
class ClaudeResponse:
    """Response object consumed by bot handlers and storage."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class StreamUpdate:
    """Streaming update payload used by orchestrator and handlers."""

    type: str  # 'assistant', 'stream_delta', 'result', ...
    content: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None
    metadata: Optional[Dict] = None


def find_codex_cli(config: Settings) -> Optional[str]:
    """Find Codex CLI executable in explicit paths, PATH, or common locations."""
    explicit_candidates = [
        getattr(config, "claude_cli_path", None),
        os.environ.get("CODEX_CLI_PATH"),
    ]

    for candidate in explicit_candidates:
        if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate

    in_path = shutil.which("codex")
    if in_path:
        return in_path

    common_paths = [
        os.path.expanduser("~/.nvm/versions/node/*/bin/codex"),
        os.path.expanduser("~/.npm-global/bin/codex"),
        os.path.expanduser("~/node_modules/.bin/codex"),
        "/usr/local/bin/codex",
        "/usr/bin/codex",
        os.path.expanduser("~/AppData/Roaming/npm/codex.cmd"),
    ]
    for pattern in common_paths:
        for match in glob.glob(pattern):
            if os.path.exists(match) and os.access(match, os.X_OK):
                return match

    return None


class ClaudeSDKManager:
    """Compatibility manager that executes prompts through Codex CLI."""

    def __init__(
        self,
        config: Settings,
        security_validator: Optional[SecurityValidator] = None,
    ):
        self.config = config
        self.security_validator = security_validator
        self.codex_path = find_codex_cli(config)

        if self.codex_path:
            logger.info("Codex CLI detected", codex_path=self.codex_path)
        else:
            logger.warning(
                "Codex CLI not found in PATH or common locations. "
                "Requests will fail until Codex is installed/configured."
            )

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> ClaudeResponse:
        """Execute command via ``codex exec`` while preserving ClaudeResponse."""
        start_time = asyncio.get_running_loop().time()

        output_file = tempfile.NamedTemporaryFile(
            prefix="codex-last-message-", suffix=".txt", delete=False
        )
        output_path = Path(output_file.name)
        output_file.close()

        state: Dict[str, Any] = {
            "session_id": None,
            "turn_count": 0,
            "text_fragments": [],
            "text_fingerprints": set(),
            "tools": [],
            "tool_fingerprints": set(),
            "stderr_lines": [],
            "non_json_stdout": [],
            "event_types": [],
            "event_errors": [],
        }
        process: Optional[asyncio.subprocess.Process] = None

        try:
            cmd = self._build_codex_command(
                prompt=prompt,
                session_id=session_id,
                continue_session=continue_session,
                output_path=output_path,
            )
            env = self._build_environment()

            logger.info(
                "Starting Codex CLI command",
                command=cmd,
                working_directory=str(working_directory),
                continue_session=continue_session,
                session_id=session_id,
            )

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(working_directory),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_SUBPROCESS_STREAM_LIMIT,
                )
            except FileNotFoundError as e:
                raise ClaudeProcessError(
                    "Codex CLI not found. Install Codex and ensure `codex` is in PATH, "
                    "or set CODEX_CLI_PATH/CLAUDE_CLI_PATH."
                ) from e

            async def _read_stdout() -> None:
                assert process and process.stdout is not None
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break

                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue

                    if not text.startswith("{"):
                        state["non_json_stdout"].append(text)
                        logger.debug("Codex non-JSON stdout", line=text)
                        continue

                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        logger.debug("Skipping invalid JSONL line", line=text[:200])
                        continue

                    event_type = str(event.get("type", "unknown"))
                    state["event_types"].append(event_type)

                    await self._handle_event(
                        event=event,
                        state=state,
                        working_directory=working_directory,
                        stream_callback=stream_callback,
                    )

            async def _read_stderr() -> None:
                assert process and process.stderr is not None
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        state["stderr_lines"].append(text)

            try:
                await asyncio.wait_for(
                    asyncio.gather(_read_stdout(), _read_stderr(), process.wait()),
                    timeout=self.config.claude_timeout_seconds,
                )
            except ValueError as e:
                err_text = str(e)
                if (
                    "Separator is not found" in err_text
                    and "chunk exceed the limit" in err_text
                ):
                    raise ClaudeProcessError(
                        "Codex produced an oversized output line that exceeded the "
                        "stream reader limit. Please retry with a narrower scope "
                        "(smaller files/outputs) if this persists."
                    ) from e
                raise

            duration_ms = int((asyncio.get_running_loop().time() - start_time) * 1000)

            content = ""
            content_from_assistant = False
            if output_path.exists():
                content = output_path.read_text(encoding="utf-8", errors="replace")
                if content.strip():
                    content_from_assistant = True

            if not content.strip():
                content = "\n".join(state["text_fragments"]).strip()
                if content.strip():
                    content_from_assistant = True

            diagnostics = "\n".join(
                [*state["stderr_lines"], *state["non_json_stdout"]]
            ).strip()

            if not content.strip():
                content = (
                    "I could not produce a final response for that request. "
                    "Please try again or rephrase."
                )

            return_code = process.returncode
            if return_code != 0:
                stderr = "\n".join(state["stderr_lines"][-30:]).strip()
                non_json = "\n".join(state["non_json_stdout"][-30:]).strip()
                event_error_text = "\n".join(state["event_errors"][-8:]).strip()
                err_text = (
                    event_error_text
                    or stderr
                    or non_json
                    or f"Codex CLI exited with status {return_code}"
                )
                err_lower = err_text.lower()

                if "mcp" in err_lower:
                    raise ClaudeMCPError(f"MCP server error: {err_text}")

                if "not logged in" in err_lower:
                    raise ClaudeProcessError(
                        "Codex CLI is not logged in. Run `codex login` on the host "
                        "running this bot, then retry."
                    )

                # Some Codex versions return non-zero when no final assistant artifact
                # was written even if delta text exists. Salvage streamed text when possible.
                if "no last agent message; wrote empty content" in err_lower:
                    logger.warning(
                        "Codex returned no final assistant artifact; "
                        "falling back to streamed content",
                        return_code=return_code,
                        stderr=err_text,
                    )
                    if not content.strip():
                        content = (
                            "I could not produce a final response for that request. "
                            "Please try again or rephrase."
                        )
                    if not content_from_assistant:
                        state["session_id"] = session_id if continue_session else None
                elif content_from_assistant:
                    logger.warning(
                        "Codex exited non-zero but produced assistant content",
                        return_code=return_code,
                        diagnostics=diagnostics[-500:],
                    )
                else:
                    raise ClaudeProcessError(f"Codex process error: {err_text}")

            final_session_id = (
                state["session_id"]
                or (session_id if continue_session and session_id else None)
                or ""
            )
            num_turns = state["turn_count"] or (1 if prompt.strip() else 0)

            if stream_callback:
                try:
                    await stream_callback(
                        StreamUpdate(
                            type="result",
                            metadata={
                                "execution_time_ms": duration_ms,
                                "event_types": state["event_types"][-8:],
                            },
                        )
                    )
                except Exception as callback_error:
                    logger.warning(
                        "Stream callback failed for result",
                        error=str(callback_error),
                    )

            return ClaudeResponse(
                content=content,
                session_id=final_session_id,
                cost=0.0,  # Codex CLI does not provide direct USD cost in JSONL output.
                duration_ms=duration_ms,
                num_turns=num_turns,
                tools_used=state["tools"],
            )

        except asyncio.TimeoutError as e:
            if process and process.returncode is None:
                process.kill()
                try:
                    await process.wait()
                except Exception:
                    pass
            raise ClaudeTimeoutError(
                f"Codex CLI timed out after {self.config.claude_timeout_seconds}s"
            ) from e

        finally:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to remove temp output file", path=str(output_path))

    def _build_codex_command(
        self,
        prompt: str,
        session_id: Optional[str],
        continue_session: bool,
        output_path: Path,
    ) -> List[str]:
        # Codex expects a non-empty prompt for reliable non-interactive runs.
        if continue_session and not prompt.strip():
            prompt = "Please continue where we left off."

        codex = self.codex_path or "codex"
        cmd: List[str] = [codex, "exec"]

        is_resume = continue_session and bool(session_id)
        if is_resume:
            cmd.append("resume")
            cmd.extend(["--json", "--skip-git-repo-check"])
            if getattr(self.config, "codex_yolo", True):
                cmd.append("--yolo")
        else:
            cmd.extend(["--json", "--skip-git-repo-check"])
            if getattr(self.config, "codex_yolo", True):
                cmd.append("--yolo")
            elif self.config.sandbox_enabled:
                cmd.extend(["--sandbox", "workspace-write"])
            else:
                cmd.extend(["--sandbox", "danger-full-access"])

        model = getattr(self.config, "claude_model", None)
        if model:
            cmd.extend(["--model", model])

        max_budget_usd = getattr(self.config, "claude_max_cost_per_request", None)
        if max_budget_usd is not None:
            cmd.extend(["-c", f"max_budget_usd={float(max_budget_usd)}"])

        extra_args = getattr(self.config, "codex_extra_args", None) or []
        if is_resume:
            sanitized: List[str] = []
            skip_next = False
            for arg in extra_args:
                if skip_next:
                    skip_next = False
                    continue
                if not isinstance(arg, str):
                    continue
                cleaned = arg.strip()
                if not cleaned:
                    continue
                if cleaned == "--sandbox":
                    skip_next = True
                    continue
                if cleaned.startswith("--sandbox="):
                    continue
                sanitized.append(cleaned)
            extra_args = sanitized

        for arg in extra_args:
            if not isinstance(arg, str):
                continue
            cleaned = arg.strip()
            if not cleaned:
                continue
            yolo_aliases = {"--yolo", "--dangerously-bypass-approvals-and-sandbox"}
            if cleaned in yolo_aliases and any(flag in cmd for flag in yolo_aliases):
                continue
            cmd.append(cleaned)

        # Preserve output-last-message behavior used in prior codex port; this can fail
        # on some versions with no final assistant artifact, but we already recover from
        # streamed text in execute_command().
        cmd.extend(["--output-last-message", str(output_path)])

        if is_resume and session_id:
            cmd.append(session_id)

        cmd.append(prompt)
        return cmd

    def _build_environment(self) -> Dict[str, str]:
        env = os.environ.copy()

        # Remove blank auth vars so local CLI auth is not accidentally shadowed.
        for key in (
            "CODEX_HOME",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "OPENAI_ORG_ID",
            "OPENAI_PROJECT",
        ):
            val = env.get(key)
            if val is not None and not str(val).strip():
                env.pop(key, None)

        codex_home = getattr(self.config, "codex_home", None)
        if codex_home:
            expanded = Path(codex_home).expanduser()
            if str(expanded).strip() and str(expanded) != ".":
                env["CODEX_HOME"] = str(expanded)
            else:
                env.pop("CODEX_HOME", None)

        claude_cli_path = getattr(self.config, "claude_cli_path", None)
        if claude_cli_path and "CODEX_CLI_PATH" not in env:
            env["CODEX_CLI_PATH"] = claude_cli_path

        return env

    async def _handle_event(
        self,
        event: Dict[str, Any],
        state: Dict[str, Any],
        working_directory: Path,
        stream_callback: Optional[Callable[[StreamUpdate], None]],
    ) -> None:
        event_type = str(event.get("type", ""))

        thread_id = event.get("thread_id") or event.get("session_id")
        if isinstance(thread_id, str) and thread_id:
            state["session_id"] = thread_id

        if event_type == "turn.started":
            state["turn_count"] += 1

        error_text = self._extract_error_text(event)
        if error_text:
            state["event_errors"].append(error_text)
            logger.warning("Codex event error", event_type=event_type, error=error_text)

        text_chunks = self._extract_text_chunks(event)
        for text_chunk in text_chunks:
            normalized = text_chunk.strip()
            if not normalized:
                continue
            if normalized in state["text_fingerprints"]:
                continue
            state["text_fingerprints"].add(normalized)
            state["text_fragments"].append(normalized)

            if stream_callback:
                try:
                    await stream_callback(
                        StreamUpdate(
                            type="stream_delta",
                            content=normalized,
                            metadata={"event_type": event_type},
                        )
                    )
                except Exception as callback_error:
                    logger.warning(
                        "Stream callback failed for text delta",
                        error=str(callback_error),
                    )

        tool_calls = self._extract_tool_calls(event)
        if not tool_calls:
            return

        validated_tool_calls: List[Dict[str, Any]] = []
        for tool in tool_calls:
            tool_name = str(tool.get("name", "")).strip()
            tool_input = tool.get("input")
            if not isinstance(tool_input, dict):
                tool_input = {}

            self._validate_tool_call(
                tool_name=tool_name,
                tool_input=tool_input,
                working_directory=working_directory,
            )

            fingerprint = json.dumps(
                {"name": tool_name, "input": tool_input},
                sort_keys=True,
            )
            if fingerprint in state["tool_fingerprints"]:
                continue

            state["tool_fingerprints"].add(fingerprint)
            normalized_tool = {"name": tool_name, "input": tool_input}
            state["tools"].append(normalized_tool)
            validated_tool_calls.append(normalized_tool)

        if stream_callback and validated_tool_calls:
            try:
                await stream_callback(
                    StreamUpdate(
                        type="assistant",
                        tool_calls=validated_tool_calls,
                        metadata={"event_type": event_type},
                    )
                )
            except Exception as callback_error:
                logger.warning(
                    "Stream callback failed for tool call",
                    error=str(callback_error),
                )

    def _validate_tool_call(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        working_directory: Path,
    ) -> None:
        if not tool_name:
            return

        if not self.config.disable_tool_validation:
            allowed = self.config.claude_allowed_tools or []
            if allowed and tool_name not in allowed:
                raise ClaudeProcessError(f"Tool not allowed: {tool_name}")

            disallowed = self.config.claude_disallowed_tools or []
            if tool_name in disallowed:
                raise ClaudeProcessError(f"Tool explicitly disallowed: {tool_name}")

        if self.security_validator and tool_name in {
            "create_file",
            "edit_file",
            "read_file",
            "Write",
            "Edit",
            "Read",
        }:
            file_path = tool_input.get("path") or tool_input.get("file_path")
            if file_path:
                valid, _, error = self.security_validator.validate_path(
                    file_path,
                    working_directory,
                )
                if not valid:
                    raise ClaudeProcessError(error or f"Invalid path: {file_path}")

        if tool_name in {"bash", "shell", "Bash"}:
            command = str(tool_input.get("command", ""))
            if command:
                valid, error = check_bash_directory_boundary(
                    command,
                    working_directory,
                    self.config.approved_directory,
                )
                if not valid:
                    raise ClaudeProcessError(error or "Directory boundary violation")

    def _extract_text_chunks(self, event: Dict[str, Any]) -> List[str]:
        """Extract assistant-facing text from Codex JSON events."""
        chunks: List[str] = []
        event_type = str(event.get("type", "")).lower()

        delta = event.get("delta")
        if isinstance(delta, str) and delta.strip():
            chunks.append(delta.strip())

        text = event.get("text")
        if isinstance(text, str) and text.strip() and "delta" in event_type:
            chunks.append(text.strip())

        output_text = event.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            chunks.append(output_text.strip())

        item = event.get("item")
        if isinstance(item, dict):
            chunks.extend(self._extract_text_from_message_like(item))

        message = event.get("message")
        if isinstance(message, dict):
            chunks.extend(self._extract_text_from_message_like(message))

        response = event.get("response")
        if isinstance(response, dict):
            response_output = response.get("output")
            if isinstance(response_output, list):
                for output_item in response_output:
                    if isinstance(output_item, dict):
                        chunks.extend(self._extract_text_from_message_like(output_item))

            response_text = response.get("output_text")
            if isinstance(response_text, str) and response_text.strip():
                chunks.append(response_text.strip())

        if (
            isinstance(text, str)
            and text.strip()
            and (
                "completed" in event_type
                or "assistant" in event_type
                or "response" in event_type
            )
        ):
            chunks.append(text.strip())

        return chunks

    def _extract_error_text(self, event: Dict[str, Any]) -> Optional[str]:
        """Extract structured error text from Codex JSON events."""
        event_type = str(event.get("type", "")).lower()
        if event_type not in {
            "error",
            "turn.failed",
            "response.failed",
            "session.failed",
        }:
            return None

        parts: List[str] = []

        error = event.get("error")
        if isinstance(error, str) and error.strip():
            parts.append(error.strip())
        elif isinstance(error, dict):
            for key in ("message", "detail", "reason", "code", "type"):
                val = error.get(key)
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())

        for key in ("message", "detail", "reason"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())

        errors = event.get("errors")
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    msg = (
                        item.get("message") or item.get("detail") or item.get("reason")
                    )
                    if isinstance(msg, str) and msg.strip():
                        parts.append(msg.strip())

        deduped = list(dict.fromkeys(parts))
        if deduped:
            return " | ".join(deduped)
        return event_type or "unknown codex error"

    def _extract_text_from_message_like(self, message: Dict[str, Any]) -> List[str]:
        chunks: List[str] = []
        role = message.get("role")
        if role is not None and role != "assistant":
            return chunks

        direct_text = message.get("text")
        if isinstance(direct_text, str) and direct_text.strip():
            chunks.append(direct_text.strip())

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            chunks.append(content.strip())
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                part_text = part.get("text")
                if (
                    isinstance(part_text, str)
                    and part_text.strip()
                    and part_type in {"output_text", "text", "message"}
                ):
                    chunks.append(part_text.strip())

                part_content = part.get("content")
                if (
                    isinstance(part_content, str)
                    and part_content.strip()
                    and part_type in {"output_text", "text", "message"}
                ):
                    chunks.append(part_content.strip())

        return chunks

    def _extract_tool_calls(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        event_type = str(event.get("type", "")).lower()
        tool_calls: List[Dict[str, Any]] = []
        tool_aliases = {
            "read": "Read",
            "read_file": "Read",
            "write": "Write",
            "write_file": "Write",
            "edit": "Edit",
            "edit_file": "Edit",
            "multi_edit": "MultiEdit",
            "multiedit": "MultiEdit",
            "bash": "Bash",
            "shell": "Bash",
            "glob": "Glob",
            "grep": "Grep",
            "ls": "LS",
            "task": "Task",
            "web_fetch": "WebFetch",
            "webfetch": "WebFetch",
            "web_search": "WebSearch",
            "websearch": "WebSearch",
            "todo_read": "TodoRead",
            "todo_write": "TodoWrite",
            "notebook_read": "NotebookRead",
            "notebook_edit": "NotebookEdit",
            "skill": "Skill",
        }

        tool_name = event.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            canonical = tool_aliases.get(tool_name.lower())
            if not canonical:
                return []
            tool_calls.append(
                {
                    "name": canonical,
                    "input": (
                        event.get("input")
                        if isinstance(event.get("input"), dict)
                        else {}
                    ),
                }
            )
            return tool_calls

        command = event.get("command")
        if isinstance(command, str) and command.strip():
            if (
                "exec.command" in event_type
                or "shell" in event_type
                or "bash" in event_type
            ):
                tool_calls.append(
                    {
                        "name": "Bash",
                        "input": {"command": command},
                    }
                )
                return tool_calls

        nested = event.get("tool_call")
        if isinstance(nested, dict):
            name = nested.get("name")
            if isinstance(name, str) and name:
                canonical = tool_aliases.get(name.lower())
                if not canonical:
                    return []
                tool_calls.append(
                    {
                        "name": canonical,
                        "input": (
                            nested.get("input")
                            if isinstance(nested.get("input"), dict)
                            else {}
                        ),
                    }
                )

        return tool_calls

    def get_active_process_count(self) -> int:
        """Retained for compatibility with existing status endpoints."""
        return 0

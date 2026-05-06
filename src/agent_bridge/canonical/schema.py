"""Canonical IR types — pydantic v2 models.

See docs/ARCHITECTURE.md §3 for field semantics. This MVP implements
the essential moment kinds; the rest are reserved for spec 002.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------- Session header ---------- #


class GitInfo(_Base):
    branch: str | None = None
    commit: str | None = None
    repo_url: str | None = None


class ModelHint(_Base):
    provider: str | None = None
    name: str | None = None
    reasoning_effort: str | None = None


class Permissions(_Base):
    approval: str | None = None  # default | on-request | never | granular | untrusted
    sandbox: str | None = None  # read-only | workspace-write | danger-full-access | external-sandbox
    writable_roots: list[str] = Field(default_factory=list)
    network_access: bool = False


class Session(_Base):
    schema_version: str = "1.0.0"
    id: str
    source_harness: Literal["claude-code", "codex", "hermes", "other"]
    source_session_id: str
    source_session_path: str | None = None
    cwd: str
    git: GitInfo = Field(default_factory=GitInfo)
    model_hint: ModelHint = Field(default_factory=ModelHint)
    started_at: str  # ISO 8601 with Z
    ended_at: str | None = None

    permissions: Permissions = Field(default_factory=Permissions)
    skills_inventory: list[dict[str, Any]] = Field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    memory_files: list[dict[str, Any]] = Field(default_factory=list)

    moments: list[Moment] = Field(default_factory=list)
    subagent_transcripts: dict[str, list[Moment]] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    source_metadata: dict[str, Any] = Field(default_factory=dict)


# ---------- Moments (discriminated union by `kind`) ---------- #


class _MomentBase(_Base):
    ts: str  # ISO 8601 with Z
    source_ref: dict[str, Any] | None = None
    agent_scope: str = "main"
    lossy: bool = False
    lossy_reason: str | None = None


class UserText(_MomentBase):
    kind: Literal["user_text"] = "user_text"
    text: str
    prompt_id: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class AssistantText(_MomentBase):
    kind: Literal["assistant_text"] = "assistant_text"
    text: str
    phase: Literal["commentary", "final_answer"] | None = None


class Thinking(_MomentBase):
    kind: Literal["thinking"] = "thinking"
    text: str
    format: Literal["plaintext", "summary", "redacted"] = "plaintext"
    lossy: bool = True
    lossy_reason: str = "harness-specific signature/encrypted_content not portable"


class ToolCall(_MomentBase):
    """A model-emitted tool call, mapped to canonical tool name."""

    kind: Literal["tool_call"] = "tool_call"
    tool: str  # canonical name from tools.CANONICAL_TOOLS
    call_id: str
    args: dict[str, Any] = Field(default_factory=dict)
    wire_native: dict[str, Any] | None = None


class ToolResult(_MomentBase):
    kind: Literal["tool_result"] = "tool_result"
    call_id: str
    output_text: str = ""
    output_blocks: list[dict[str, Any]] = Field(default_factory=list)
    is_error: bool = False
    exit_code: int | None = None
    duration_ms: int | None = None
    external_ref: str | None = None


class Attachment(_MomentBase):
    """Harness-injected context (CC attachment row, Codex developer envelope)."""

    kind: Literal["attachment"] = "attachment"
    subtype: str  # memory | skill_listing | skill_invoked | task_reminder | file | image | edited_file | date_change | queued_command | command_permissions | env
    data: dict[str, Any] = Field(default_factory=dict)


class ModeChange(_MomentBase):
    kind: Literal["mode_change"] = "mode_change"
    from_mode: str | None = Field(default=None, alias="from")
    to_mode: str = Field(alias="to")
    fields_changed: dict[str, Any] = Field(default_factory=dict)


class PlanUpdate(_MomentBase):
    kind: Literal["plan_update"] = "plan_update"
    items: list[dict[str, Any]] = Field(default_factory=list)
    diff: dict[str, Any] | None = None


class Error(_MomentBase):
    kind: Literal["error"] = "error"
    message: str
    subtype: str | None = None
    retry_info: dict[str, Any] | None = None


class Notification(_MomentBase):
    kind: Literal["notification"] = "notification"
    subtype: str
    content: str
    ref: str | None = None


class SummaryCompaction(_MomentBase):
    """A compaction marker: prior history was condensed to a summary."""

    kind: Literal["summary_compaction"] = "summary_compaction"
    trigger: Literal["auto", "manual"] = "auto"
    before_tokens: int | None = None
    after_tokens: int | None = None
    summary_text: str = ""
    lossy: bool = True
    lossy_reason: str = "pre-compaction history not preserved across harnesses"


class Metadata(_MomentBase):
    kind: Literal["metadata"] = "metadata"
    subtype: str
    data: dict[str, Any] = Field(default_factory=dict)


Moment = Annotated[
    Union[
        UserText,
        AssistantText,
        Thinking,
        ToolCall,
        ToolResult,
        Attachment,
        ModeChange,
        PlanUpdate,
        Error,
        Notification,
        SummaryCompaction,
        Metadata,
    ],
    Field(discriminator="kind"),
]


# Resolve forward ref on Session.moments / subagent_transcripts
Session.model_rebuild()

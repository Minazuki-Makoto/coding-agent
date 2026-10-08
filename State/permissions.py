"""Host-created permission decisions; never model-writable arguments."""
from dataclasses import dataclass


class RequestDenied(RuntimeError):
    """Stop the current request, without shutting down the terminal."""


@dataclass(frozen=True)
class ToolPermission:
    """One Host-owned decision for one exact call; never part of a tool schema."""
    permission_id: str
    tool_name: str
    actor: str
    operation: str
    session_address: str
    chat_id: str
    branch_id: str
    request_id: str
    policy_version: str
    project_root: str | None
    staging_root: str | None
    arguments_hash: str
    execution_hash: str
    is_permitted: bool = False
    consumed: bool = False


@dataclass(frozen=True)
class Permission:
    operation: str
    project_root: str
    session_address: str
    chat_id: str
    branch_id: str
    request_id: str
    policy_version: str
    operation_hash: str
    is_permitted: bool

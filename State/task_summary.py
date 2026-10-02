from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import quote

from MCP_functions.System_Files.Files_function.write_in import write_in


TASK_SUMMARY_FILE_NAME = "task_summary.md"
TASK_SUMMARY_CONTEXT_MAX_CHARS = 12000
_RECORD_MARKER = re.compile(
    r"<!-- task-summary task_id=(?P<task_id>\d+) supervisor_seq=(?P<seq>\d+) -->"
)


def task_summary_path(session_address: str, chat_id: str) -> Path:
    session_root = Path(session_address).resolve(strict=False)
    raw_chat_id = str(chat_id)
    if not raw_chat_id.strip():
        raise ValueError("chat_id is required")
    encoded = quote(raw_chat_id, safe="-_.")
    digest = hashlib.sha256(raw_chat_id.encode("utf-8")).hexdigest()[:16]
    if len(encoded) > 80:
        encoded = encoded[:60]
    # Windows paths are case-insensitive; the digest keeps distinct chat IDs isolated.
    return session_root / f"chat_{encoded}_{digest}" / TASK_SUMMARY_FILE_NAME


def read_task_summary_context(
    session_address: str,
    chat_id: str,
    current_task_id: int,
    max_chars: int = TASK_SUMMARY_CONTEXT_MAX_CHARS,
    include_other_tasks: bool = True,
):
    path = task_summary_path(session_address, chat_id)
    if not path.is_file():
        return {
            "status": "not_found",
            "current_task_latest": "",
            "current_task_locator": None,
            "other_tasks_latest": [],
        }

    content = path.read_text(encoding="utf-8")
    matches = list(_RECORD_MARKER.finditer(content))
    latest_by_task = {}
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        task_id = int(match.group("task_id"))
        supervisor_seq = int(match.group("seq"))
        latest_by_task[task_id] = {
            "task_id": task_id,
            "supervisor_seq": supervisor_seq,
            "record": content[start:end].strip(),
        }

    current = latest_by_task.pop(current_task_id, None)
    others = sorted(
        (
            item for task_id, item in latest_by_task.items()
            if include_other_tasks and task_id < current_task_id
        ),
        key=lambda item: item["supervisor_seq"], reverse=True,
    )
    selected_others = []
    used = len(current["record"]) if current else 0
    for item in others:
        size = len(item["record"])
        if selected_others and used + size > max_chars:
            break
        if not selected_others and used + size > max_chars:
            continue
        selected_others.append(item)
        used += size
    selected_others.reverse()
    return {
        "status": "success" if current or selected_others else "not_found",
        "current_task_latest": current["record"] if current else "",
        "current_task_locator": (
            {"task_id": current_task_id, "supervisor_seq": current["supervisor_seq"]}
            if current else None
        ),
        "other_tasks_latest": selected_others,
    }


def render_task_summary_record(
    *,
    task_id: int,
    supervisor_seq: int,
    target: str,
    current_turn_summary: str,
    cumulative_task_summary: str,
    current_verification_summary: str,
    corrected_or_invalidated: list[str],
    remaining_work: list[str],
    decision_summary: str,
    next_step: str,
    evidence_references: list[dict],
    task_outcome: dict | None,
):
    marker = (
        f"<!-- task-summary task_id={task_id} supervisor_seq={supervisor_seq} -->"
    )
    corrections = "\n".join(
        f"- {item}" for item in corrected_or_invalidated
    ) or "- 无"
    remaining = "\n".join(f"- {item}" for item in remaining_work) or "- 无"
    evidence = "\n".join(
        "- "
        + ", ".join(
            f"{key}={value}"
            for key, value in item.items()
            if value not in (None, "", [], {})
        )
        for item in evidence_references
    ) or "- 无新增定位信息"
    outcome_section = ""
    if task_outcome is not None:
        outcome_section = (
            "\n### 已完成 TaskOutcome\n"
            f"- accepted_summary：{task_outcome['accepted_summary']}\n"
            f"- verification_summary：{task_outcome['verification_summary']}\n"
        )
    return (
        f"{marker}\n"
        f"## Task {task_id} · Supervisor seq {supervisor_seq}\n\n"
        f"**任务目标**：{target}\n\n"
        f"### 本轮最新结果\n{current_turn_summary}\n\n"
        f"### 当前累计有效成果\n{cumulative_task_summary}\n\n"
        f"### 当前验收情况\n{current_verification_summary}\n\n"
        f"### 证据定位\n{evidence}\n\n"
        f"### 被修正或失效的旧结论\n{corrections}\n\n"
        f"### 剩余工作\n{remaining}\n\n"
        f"### Supervisor 最新决定\n{decision_summary}\n\n"
        f"**下一步**：{next_step or '无'}\n"
        f"{outcome_section}"
    ).strip()


def append_task_summary_record(
    session_address: str,
    chat_id: str,
    task_id: int,
    supervisor_seq: int,
    record: str,
):
    path = task_summary_path(session_address, chat_id)
    marker = (
        f"<!-- task-summary task_id={task_id} supervisor_seq={supervisor_seq} -->"
    )
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if marker in existing:
        return {
            "status": "success",
            "operation": "skip_duplicate",
            "file_address": str(path),
            "changed": False,
        }
    updated = f"{existing.rstrip()}\n\n{record.strip()}\n" if existing else f"{record.strip()}\n"
    result = write_in(
        str(path),
        updated,
        trusted_roots=(str(path.parent),),
    )
    if result.get("status") != "success":
        raise RuntimeError(
            "task summary save failed: "
            + (result.get("message") or "unknown write error")
        )
    return result

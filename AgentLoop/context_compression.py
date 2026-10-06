"""Explicit high-threshold context preparation; never changes persisted facts."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, asdict


class ContextBudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompressionResult:
    content: str
    summarized: bool = False
    truncated: bool = False
    original_chars: int = 0
    locator: object = None
    failure_reason: str = ""


async def compress_context(content, *, trigger_chars=60000, summary_max_chars=6000,
                           strategy="summarize", content_type="history", call_model=None,
                           goal="", locator=None, branch_scope="main", cache=None,
                           retries=2, max_input_chars=70000):
    from AgentLoop.loop_utils import bounded_text, parse_json_object
    if trigger_chars < 1 or summary_max_chars < 1 or retries < 1:
        raise ValueError("compression limits must be positive")
    if strategy not in {"summarize", "truncate"}:
        raise ValueError("strategy must be summarize or truncate")
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    size = len(text)
    if strategy == "truncate":
        return CompressionResult(bounded_text(text, summary_max_chars, keep_tail=True),
                                 truncated=size > summary_max_chars, original_chars=size,
                                 locator=locator)
    if size <= trigger_chars:
        return CompressionResult(text, original_chars=size, locator=locator)
    key = hashlib.sha256(json.dumps([text, goal, content_type, branch_scope,
                                    trigger_chars, summary_max_chars, max_input_chars, locator],
                                   ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if cache is not None and key in cache:
        return CompressionResult(**cache[key])
    prompt = ("Compress material for the current goal; no tools or new decisions. "
              "Preserve constraints, still-valid accomplishments, unresolved issues and evidence. "
              "For chronological history prioritize the current requirement, latest results and "
              "corrections; explicitly explain conflicts, never change acceptance status. "
              "For one tool body prioritize relevance/evidence, not position in the text. "
              f"Return JSON {{\"summary\":\"...\"}}, at most {summary_max_chars} characters.")
    # Chunk only after the original material crosses the high threshold. Every
    # original character enters a fragment; no pre-summary middle truncation.
    width = max(1, max_input_chars)
    fragments = [text[index:index + width] for index in range(0, size, width)]
    per_part_limit = (summary_max_chars - len(fragments) + 1) // len(fragments)
    summaries = []
    reason = "compression model unavailable"
    for index, fragment in enumerate(fragments):
        messages = [{"role": "user", "content": json.dumps({
            "instruction": prompt + f" This is part {index + 1}/{len(fragments)}; use at most {per_part_limit} characters.",
            "goal": goal, "content_type": content_type,
            "source_locator": locator, "material": fragment}, ensure_ascii=False)}]
        for _ in range(retries if call_model and per_part_limit > 0 else 0):
            try:
                response = await call_model(messages, [])
                if response.get("status") != "success" or response.get("tool"):
                    raise ValueError("compression requires a successful response without tools")
                summary = parse_json_object(response.get("message")).get("summary")
                if not isinstance(summary, str) or not summary.strip():
                    raise ValueError("empty compression summary")
                if len(summary) > per_part_limit:
                    raise ValueError(f"summary too long ({len(summary)}); rewrite within {per_part_limit}")
                summaries.append(summary)
                break
            except Exception as exc:
                reason = str(exc)
                messages.append({"role": "user", "content": reason})
        else:
            break
    if len(summaries) == len(fragments):
        result = CompressionResult("\n".join(summaries), True, False, size, locator)
    else:
        result = CompressionResult(bounded_text(text, summary_max_chars, keep_tail=True),
                                   False, True, size, locator, reason)
    if cache is not None:
        cache[key] = asdict(result)
    return result


# These fields are facts/control metadata, never rewritten by the compressor.
PROTECTED = {"tool_name", "client_name", "ok", "tool_ok", "error_type", "message",
             "error_message", "status", "task_id", "seq", "supervisor_seq", "executor_seq",
             "tool_result_seq", "event_id", "event_seq", "branch_id", "request_id",
             "root", "address", "path", "file_address", "paths", "locator", "tool_result_ref",
             "tool_result_refs", "evidence_references", "evidence_locators", "pagination",
             "has_more", "next_start_index", "next_start_char", "is_finished", "is_passed",
             "is_executor_passed", "arguments", "completion_criteria", "original_completion_criteria"}
HISTORY_FIELDS = {"recent_descriptions", "memory_window", "validation_window", "memory_query_results",
                  "supervisor_descriptions", "supervisor_history", "previous_work", "accepted_work",
                  "dependency_context", "completed_task_outcomes", "task_outcomes", "conversation_context"}


def _facts(value):
    if isinstance(value, dict):
        facts = {key: item for key, item in value.items() if key in PROTECTED}
        children = [_facts(item) for key, item in value.items() if key not in PROTECTED]
        children = [item for item in children if item]
        if children:
            facts["source_facts"] = children
        return facts
    if isinstance(value, list):
        return [item for item in (_facts(child) for child in value) if item]
    return None


async def prepare_request(now_state, messages, tools, call_model):
    """Prepare copies at the async call site, then enforce the whole character budget."""
    trigger = getattr(now_state, "context_trigger_chars", 60000)
    target = getattr(now_state, "context_summary_max_chars", 6000)
    budget = getattr(now_state, "context_max_chars", 80000)
    cache = getattr(now_state, "compression_cache", None)
    scope = getattr(now_state, "branch_id", "main")
    prepared = copy.deepcopy(messages)

    async def compress(value, kind, locator=None):
        result = await compress_context(value, trigger_chars=trigger, summary_max_chars=target,
                                        content_type=kind, call_model=call_model,
                                        goal=now_state.user_query, locator=locator,
                                        branch_scope=scope, cache=cache,
                                        max_input_chars=max(1, min(70000, budget - len(json.dumps(now_state.user_query, ensure_ascii=False)) - 4000)))
        if not result.summarized and not result.truncated:
            return value
        return {"compressed_body": result.content, "compression": {
            "summarized": result.summarized, "truncated": result.truncated,
            "original_chars": result.original_chars, "source_locator": result.locator,
            "failure_reason": result.failure_reason}, "source_facts": _facts(value)}

    async def visit(value, field=""):
        if field in HISTORY_FIELDS:
            return await compress(value, "chronological_history")
        if field in {"output", "result", "answer"}:
            if field == "result" and isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    pass
                else:
                    return json.dumps(await visit(parsed), ensure_ascii=False)
            return await compress(value, "tool_body" if field != "answer" else "answer")
        if isinstance(value, dict):
            return {key: item if key in PROTECTED else await visit(item, key)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [await visit(item) for item in value]
        return value

    # Process the original, complete JSON body before any ordinary window slicing.
    for message in prepared:
        content = message.get("content")
        if isinstance(content, str):
            prefix = ""
            try:
                value = json.loads(content)
            except (ValueError, TypeError):
                # Claude combines the prompt and information in one user block.
                prefix, separator, body = content.rpartition("\n\n")
                if not separator:
                    continue
                try:
                    value = json.loads(body)
                    prefix += separator
                except ValueError:
                    continue  # rules/user requirements are never blindly rewritten
            message["content"] = prefix + json.dumps(await visit(value), ensure_ascii=False)
        elif isinstance(content, list):
            for block in content:
                if block.get("type") != "tool_result" or not isinstance(block.get("content"), str):
                    continue
                try:
                    value = json.loads(block["content"])
                except ValueError:
                    value = {"output": block["content"]}
                block["content"] = json.dumps(await visit(value), ensure_ascii=False)

    def size():
        return len(json.dumps({"messages": prepared, "tools": tools}, ensure_ascii=False))

    if size() > budget:
        # Compress an accumulated collection only above the same high threshold.
        candidates = []
        def collect(value):
            if not isinstance(value, dict):
                return
            for key, item in value.items():
                if key in HISTORY_FIELDS:
                    candidates.append((value, key, item))
                elif isinstance(item, dict):
                    collect(item)
        decoded = []
        for message in prepared:
            prefix = ""
            try:
                value = json.loads(message.get("content", ""))
            except (ValueError, TypeError):
                content = message.get("content", "")
                if not isinstance(content, str):
                    continue
                prefix, separator, body = content.rpartition("\n\n")
                if not separator:
                    continue
                try:
                    value = json.loads(body)
                except ValueError:
                    continue
                prefix += separator
            decoded.append((message, value, prefix))
            collect(value)
        aggregate = [{"field": key, "material": value} for _, key, value in candidates]
        if len(json.dumps(aggregate, ensure_ascii=False)) > trigger:
            replacement = await compress(aggregate, "chronological_history")
            for container, key, _ in candidates:
                container[key] = {"see": "accumulated_context"}
            if decoded:
                decoded[-1][1]["accumulated_context"] = replacement
            for message, value, prefix in decoded:
                message["content"] = prefix + json.dumps(value, ensure_ascii=False)
    if size() > budget:
        raise ContextBudgetError(f"context_budget_blocked: {size()} characters > {budget}; "
                                 "requirements/rules retained; no low-threshold summarization")
    return prepared

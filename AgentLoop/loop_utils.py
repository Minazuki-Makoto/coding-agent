from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from MCP_functions.tool_registry import ToolExecutionResult


MODEL_CONTEXT_MAX_CHARS = 80000
USER_QUERY_MAX_CHARS = 16000
DESCRIPTION_MAX_CHARS = 6000
TOOL_RESULT_CONTEXT_MAX_CHARS = 12000


@dataclass
class NormalizedToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    raw_call: dict[str, Any]


async def call_chat_function(
    chat_function,
    provider: str,
    client,
    model: str,
    messages: list[dict],
    tools: list[dict],
    temperature: float,
):
    """Run synchronous SDK adapters without blocking the event loop."""
    arguments = {
        "query": messages,
        "client": client,
        "model": model,
        "tools": tools,
    }
    if provider == "chatgpt":
        arguments["expected_temperature"] = temperature
    else:
        arguments["temperature"] = temperature
    response = await asyncio.to_thread(chat_function, **arguments)
    return normalize_chat_response(response)


def normalize_chat_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {
            "status": "error",
            "message": f"model adapter returned {type(response).__name__}, expected dict",
            "tool": [],
            "total_tokens": 0,
        }
    total_tokens = response.get("total_tokens", 0)
    if not isinstance(total_tokens, int) or isinstance(total_tokens, bool):
        total_tokens = 0
    return {
        "status": response.get("status", "error"),
        "message": response.get("message"),
        "tool": response.get("tool") or [],
        "total_tokens": total_tokens,
    }


def build_model_messages(provider: str, prompt: str, information: dict):
    information_content = json.dumps(
        to_jsonable(information), ensure_ascii=False, separators=(",", ":")
    )
    if provider == "claude":
        return [{"role": "user", "content": prompt + "\n\n" + information_content}]
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": information_content},
    ]


def build_tool_observation_messages(
    provider: str,
    response_message: str | None,
    tool_call: NormalizedToolCall,
    tool_result: ToolExecutionResult,
) -> list[dict]:
    """Build one provider-valid assistant tool call and tool observation pair."""
    content = bounded_text(
        tool_result.to_model_content(), TOOL_RESULT_CONTEXT_MAX_CHARS, keep_tail=True
    )
    if provider == "claude":
        assistant_content: list[dict[str, Any]] = []
        if response_message:
            assistant_content.append({"type": "text", "text": response_message})
        assistant_content.append(
            {
                "type": "tool_use",
                "id": tool_call.call_id,
                "name": tool_call.name,
                "input": tool_call.arguments,
            }
        )
        return [
            {"role": "assistant", "content": assistant_content},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.call_id,
                        "content": content,
                        "is_error": not tool_result.ok,
                    }
                ],
            },
        ]

    raw_call = tool_call.raw_call.copy()
    raw_call.setdefault("id", tool_call.call_id)
    raw_call.setdefault("type", "function")
    raw_call.setdefault(
        "function",
        {
            "name": tool_call.name,
            "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
        },
    )
    return [
        {
            "role": "assistant",
            "content": response_message,
            "tool_calls": [raw_call],
        },
        {
            "role": "tool",
            "tool_call_id": tool_call.call_id,
            "name": tool_call.name,
            "content": content,
        },
    ]


def normalize_tool_calls(provider: str, tool_calls):
    normalized_calls = []
    for index, tool in enumerate(tool_calls or []):
        raw_tool = _to_dictionary(tool)
        if provider == "claude":
            call_id = raw_tool.get("id") or f"claude_tool_{index}"
            tool_name = raw_tool.get("name")
            arguments = raw_tool.get("input", {})
        else:
            function = raw_tool.get("function", {})
            call_id = raw_tool.get("id") or f"tool_{index}"
            tool_name = function.get("name")
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid arguments for tool {tool_name}: {exc}"
                    ) from exc
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("model returned a tool call without a valid name")
        if not isinstance(arguments, dict):
            raise ValueError(f"arguments for tool {tool_name} must be a dictionary")
        normalized_calls.append(
            NormalizedToolCall(
                call_id=call_id,
                name=tool_name,
                arguments=arguments,
                raw_call=raw_tool,
            )
        )
    return normalized_calls


def bounded_text(value, max_chars: int, keep_tail: bool = False):
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)) or is_dataclass(value):
        text = json.dumps(to_jsonable(value), ensure_ascii=False)
    else:
        text = str(value)
    if len(text) <= max_chars:
        return text
    if not keep_tail:
        suffix = f"...<省略 {len(text) - max_chars} 字符>"
        content_chars = max(max_chars - len(suffix), 0)
        return (text[:content_chars] + suffix)[:max_chars]
    separator = "\n...<中间内容因上下文预算被省略>...\n"
    content_chars = max(max_chars - len(separator), 0)
    head_chars = content_chars // 2
    tail_chars = content_chars - head_chars
    if tail_chars == 0:
        return separator[:max_chars]
    return (text[:head_chars] + separator + text[-tail_chars:])[:max_chars]


def validate_description(value):
    """Validate content; length is a writing preference, never a protocol error."""
    field_name = "description"
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串")
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} 不能为空或只包含空白字符")
    return value


def parse_json_object(content: str):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("model did not return a JSON object")
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("model response must be a JSON object")
    return result


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, ToolExecutionResult):
        return value.to_dict()
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")


def _to_dictionary(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if hasattr(value, "dict"):
        return value.dict(exclude_none=True)
    raise ValueError("unsupported tool call object")

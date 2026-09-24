from __future__ import annotations

import json
from dataclasses import dataclass
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
    arguments: dict[str,Any]
    raw_call: dict[str,Any]


def call_chat_function(
        chat_function,
        provider:str,
        client,
        model:str,
        messages:list[dict],
        tools:list[dict],
        temperature:float
):
    check_context_size(messages,tools)

    arguments = {
        "query":messages,
        "client":client,
        "model":model,
        "tools":tools,
    }

    if provider == "chatgpt":
        arguments["expected_temperature"] = temperature
    else:
        arguments["temperature"] = temperature

    return chat_function(**arguments)


def build_model_messages(provider:str,prompt:str,information:dict):
    information_content = json.dumps(
        information,
        ensure_ascii=False,
        default=str
    )

    if provider == "claude":
        return [
            {
                "role":"user",
                "content":prompt + "\n\n" + information_content
            }
        ]

    return [
        {
            "role":"system",
            "content":prompt
        },
        {
            "role":"user",
            "content":information_content
        }
    ]


def check_context_size(messages:list[dict],tools:list[dict]):
    content = json.dumps(
        {
            "messages":messages,
            "tools":tools,
        },
        ensure_ascii=False,
        default=str
    )

    if len(content) > MODEL_CONTEXT_MAX_CHARS:
        raise ValueError(
            "model input exceeds local context budget: "
            f"{len(content)} > {MODEL_CONTEXT_MAX_CHARS} characters"
        )


def normalize_tool_calls(provider:str,tool_calls):
    normalized_calls = []

    for index,tool in enumerate(tool_calls or []):
        raw_tool = _to_dictionary(tool)

        if provider == "claude":
            call_id = raw_tool.get("id") or f"claude_tool_{index}"
            tool_name = raw_tool.get("name")
            arguments = raw_tool.get("input",{})

        else:
            function = raw_tool.get("function",{})
            call_id = raw_tool.get("id") or f"tool_{index}"
            tool_name = function.get("name")
            arguments = function.get("arguments",{})

            if isinstance(arguments,str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"invalid arguments for tool {tool_name}: {e}"
                    ) from e

        if not isinstance(tool_name,str) or not tool_name:
            raise ValueError("model returned a tool call without a valid name")

        if not isinstance(arguments,dict):
            raise ValueError(f"arguments for tool {tool_name} must be a dictionary")

        normalized_calls.append(
            NormalizedToolCall(
                call_id=call_id,
                name=tool_name,
                arguments=arguments,
                raw_call=raw_tool
            )
        )

    return normalized_calls


def tool_result_for_context(
        tool_result:ToolExecutionResult,
        max_chars:int=TOOL_RESULT_CONTEXT_MAX_CHARS
):
    result = tool_result.to_dict()
    content = json.dumps(
        result,
        ensure_ascii=False,
        default=str
    )

    if len(content) <= max_chars:
        return {
            "truncated":False,
            "content":result,
        }

    return {
        "truncated":True,
        "original_chars":len(content),
        "content_preview":bounded_text(
            content,
            max_chars=max_chars,
            keep_tail=True
        ),
        "instruction":(
            "工具结果超过当前上下文预算，只保留首尾。"
            "请缩小下一次查询范围，不能根据被省略内容作结论。"
        ),
    }


def bounded_text(value,max_chars:int,keep_tail:bool=False):
    if value is None:
        return ""

    text = str(value)
    if len(text) <= max_chars:
        return text

    if not keep_tail:
        suffix = f"...<省略 {len(text) - max_chars} 字符>"
        content_chars = max(max_chars - len(suffix),0)
        return (text[:content_chars] + suffix)[:max_chars]

    separator = "\n...<中间内容因上下文预算被省略>...\n"
    content_chars = max(max_chars - len(separator),0)
    head_chars = content_chars // 2
    tail_chars = content_chars - head_chars

    if tail_chars == 0:
        return separator[:max_chars]

    return (
        text[:head_chars]
        + separator
        + text[-tail_chars:]
    )[:max_chars]


def parse_json_object(content:str):
    if not isinstance(content,str) or not content.strip():
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
    if not isinstance(result,dict):
        raise ValueError("model response must be a JSON object")

    return result


def _to_dictionary(value):
    if isinstance(value,dict):
        return value

    if hasattr(value,"model_dump"):
        return value.model_dump(mode="json",exclude_none=True)

    if hasattr(value,"dict"):
        return value.dict(exclude_none=True)

    raise ValueError("unsupported tool call object")

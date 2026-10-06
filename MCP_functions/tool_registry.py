from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class AgentRole(str, Enum):
    SUPERVISOR = "supervisor"
    EXECUTOR = "executor"


@dataclass(frozen=True)
class ToolExecutionResult:
    tool_name: str
    client_name: str | None
    ok: bool
    error_type: str | None
    message: str
    output: Any
    raw_result: dict[str, Any] | None = None

    def to_dict(self):
        return {
            "tool_name": self.tool_name,
            "client_name": self.client_name,
            "ok": self.ok,
            "error_type": self.error_type,
            "message": self.message,
            "output": self.output,
        }

    def to_model_content(self):
        return json.dumps(
            _json_safe(self.to_dict()),
            ensure_ascii=False,
        )


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json", exclude_none=True))
    if hasattr(value, "dict"):
        return _json_safe(value.dict(exclude_none=True))
    raise TypeError(f"unsupported tool result value: {type(value).__name__}")


SUPERVISOR_ONLY_TOOLS = {
    "read_now_task",
    "read_history_task",
    "read_history_chat",
    "read_task_history_error",
    "read_task_error",
    "read_supervisor_task",
    "read_supervisor_history",
    "read_tool_result",
    "read_task_summary",
}

SHARED_READ_ONLY_TOOLS = {
    "read_all_files_tool",
    "read_files_content_tool",
    "sort_files_by_suffix_tool",
}

EXECUTOR_ONLY_TOOLS = {
    "judge_spring_project_tool",
    "find_java_exe_tool",
    "run_java_get_feedback",
    "package_spring_boot_with_confirmation_tool",
    "check_spring_boot_startup_tool",
    "write_in_tool",
    "str_replace_tool",
    "find_the_python_editor_tool",
    "run_code_get_feedback_tool",
    "download_package_with_confirmation_tool",
    "get_needed_info_tool",
}


def build_default_tool_permissions():
    permissions = {}

    for tool_name in SUPERVISOR_ONLY_TOOLS:
        permissions[tool_name] = {AgentRole.SUPERVISOR}

    for tool_name in SHARED_READ_ONLY_TOOLS:
        permissions[tool_name] = {
            AgentRole.SUPERVISOR,
            AgentRole.EXECUTOR,
        }

    for tool_name in EXECUTOR_ONLY_TOOLS:
        permissions[tool_name] = {AgentRole.EXECUTOR}

    return permissions


class ToolRegistry:
    def __init__(
            self,
            host,
            permissions:dict[str,set[AgentRole]] | None = None,
            client_default_permissions:dict[str,set[AgentRole]] | None = None,
            context_provider=None,
            execution_gateway=None,
    ):
        self.host = host
        self.permissions = permissions or build_default_tool_permissions()
        self.context_provider = context_provider
        self.execution_gateway = execution_gateway

        # 外部 MCP 的工具名称是动态发现的，按服务分配给 executor。
        self.client_default_permissions = client_default_permissions or {
            "github": {AgentRole.EXECUTOR},
            "browser": {AgentRole.EXECUTOR},
            "docker": {AgentRole.EXECUTOR},
        }

        self._check_duplicate_tools()

    def _check_duplicate_tools(self):
        seen = set()
        duplicate = set()

        for tool in self.host.tools:
            tool_name = tool.get("function",{}).get("name")
            if not tool_name:
                continue

            if tool_name in seen:
                duplicate.add(tool_name)

            seen.add(tool_name)

        if duplicate:
            names = ", ".join(sorted(duplicate))
            raise ValueError(f"duplicate MCP tool names: {names}")

    def _get_allowed_roles(self,tool_name:str):
        if tool_name in self.permissions:
            return self.permissions[tool_name]

        client_name = self.host.tool_dictionary.get(tool_name)
        return self.client_default_permissions.get(client_name,set())

    def _is_allowed(self,role:AgentRole,tool_name:str):
        return role in self._get_allowed_roles(tool_name)

    def schemas_for(self,role:AgentRole | str,provider:str):
        role = AgentRole(role)
        tools = []

        for tool in self.host.tools:
            function = tool.get("function",{})
            tool_name = function.get("name")

            if not tool_name or not self._is_allowed(role,tool_name):
                continue

            parameters = function.get("parameters") or {"type": "object", "properties": {}}
            if tool_name == "read_task_summary":
                # These values are injected by the Host, never chosen by the model.
                parameters = {
                    **parameters,
                    "properties": {
                        name: schema
                        for name, schema in parameters.get("properties", {}).items()
                        if name not in {"session_address", "chat_id"}
                    },
                    "required": [
                        name for name in parameters.get("required", [])
                        if name not in {"session_address", "chat_id"}
                    ],
                }

            if provider == "claude":
                tools.append(
                    {
                        "name":tool_name,
                        "description":function.get("description", ""),
                        "input_schema": parameters,
                    }
                )

            else:
                tools.append(
                    {
                        "type":"function",
                        "function":{
                            "name":tool_name,
                            "description":function.get("description", ""),
                            "parameters": parameters,
                        }
                    }
                )

        return tools

    async def call(
            self,
            role:AgentRole | str,
            tool_name:str,
            arguments:dict[str,Any]
    ):
        try:
            role = AgentRole(role)
        except ValueError:
            return self._error(
                tool_name=str(tool_name),
                error_type="invalid_role",
                message=f"unknown agent role: {role}"
            )

        if not isinstance(tool_name,str) or not tool_name.strip():
            return self._error(
                tool_name=str(tool_name),
                error_type="invalid_tool_name",
                message="tool_name must be a non-empty string"
            )

        if not isinstance(arguments,dict):
            return self._error(
                tool_name=tool_name,
                error_type="invalid_arguments",
                message="tool arguments must be a dictionary"
            )

        if tool_name in SUPERVISOR_ONLY_TOOLS:
            try:
                bound_arguments = self._bind_history_arguments(tool_name, arguments)
                arguments.clear()
                arguments.update(bound_arguments)
            except ValueError as exc:
                return self._error(
                    tool_name=tool_name,
                    error_type="invalid_history_arguments",
                    message=str(exc),
                )

        client_name = self.host.tool_dictionary.get(tool_name)
        if client_name is None:
            return self._error(
                tool_name=tool_name,
                error_type="unknown_tool",
                message=f"tool is not registered: {tool_name}"
            )

        if not self._is_allowed(role,tool_name):
            return self._error(
                tool_name=tool_name,
                client_name=client_name,
                error_type="permission_denied",
                message=f"{role.value} is not allowed to call {tool_name}"
            )

        from State.session_checkpoint import active_run
        run = active_run()
        if tool_name in SUPERVISOR_ONLY_TOOLS and run:
            # In-process Host binding enforces branch visibility; no mutable model branch input.
            from Context import mcp_resources, mcp_supervisor_tools
            function = getattr(mcp_resources, tool_name, None) or getattr(mcp_supervisor_tools, tool_name)
            output = await function(**arguments)
            return ToolExecutionResult(tool_name, "host_history", True, None, "", output)
        if self.execution_gateway is not None:
            if run and role == AgentRole.SUPERVISOR:
                import uuid
                run.current_operation = uuid.uuid4().hex
                run.emit("tool_intent", "supervisor", operation_id=run.current_operation,
                         tool_name=tool_name, arguments=dict(arguments), status="started")
            async def raw_call():
                client = self.host.session_dictionary.get(client_name)
                if client is None:
                    return self._error(tool_name, "client_unavailable", "MCP client unavailable", client_name)
                raw = await client.call_tool(tool_name=tool_name, argument=arguments)
                return self._normalize_result(tool_name, client_name, raw)
            return await self.execution_gateway.call(tool_name, arguments, raw_call)

        client = self.host.session_dictionary.get(client_name)
        if client is None:
            return self._error(
                tool_name=tool_name,
                client_name=client_name,
                error_type="client_unavailable",
                message=f"MCP client is unavailable: {client_name}"
            )

        try:
            raw_result = await client.call_tool(
                tool_name=tool_name,
                argument=arguments
            )

        except Exception as e:
            return self._error(
                tool_name=tool_name,
                client_name=client_name,
                error_type="mcp_call_error",
                message=str(e)
            )

        return self._normalize_result(
            tool_name=tool_name,
            client_name=client_name,
            raw_result=raw_result
        )

    def _bind_history_arguments(self, tool_name, arguments):
        if not callable(self.context_provider):
            raise ValueError("host history context is not configured")
        context = self.context_provider() or {}
        session_address = context.get("session_address")
        chat_id = context.get("chat_id")
        current_task_id = context.get("task_id")
        if not isinstance(session_address, str) or not session_address:
            raise ValueError("host session_address is unavailable")
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("host chat_id is unavailable")
        bound = dict(arguments)
        bound.pop("branch_id", None)
        bound["session_address"] = session_address
        bound["chat_id"] = chat_id
        if "task_id" in bound:
            task_id = bound["task_id"]
            if type(task_id) is not int or task_id < 0:
                raise ValueError("task_id must be a non-negative integer")
            if type(current_task_id) is int and task_id > current_task_id:
                raise ValueError("task_id cannot refer to a future task")
        if "seq" in bound and (
            type(bound["seq"]) is not int or bound["seq"] < 0
        ):
            raise ValueError("seq must be a non-negative integer")
        if "tool_result_seq" in bound and (
            type(bound["tool_result_seq"]) is not int
            or bound["tool_result_seq"] < 1
        ):
            raise ValueError("tool_result_seq must be a positive integer")
        return bound

    def _normalize_result(self,tool_name,client_name,raw_result):
        payload = self._dump_result(raw_result)

        protocol_error = bool(
            payload.get("isError",payload.get("is_error",False))
        )

        output = payload.get(
            "structuredContent",
            payload.get("structured_content")
        )

        if output is None:
            output = self._extract_text_output(payload.get("content",[]))

        business_error = (
            isinstance(output,dict)
            and str(output.get("status","")).lower()
            in {"error","failed","failure","confirmation_required","cancelled","denied","unknown","interrupted"}
        )

        ok = not protocol_error and not business_error
        message = ""

        if isinstance(output,dict) and output.get("message") is not None:
            message = str(output.get("message"))

        if protocol_error and not message:
            message = "MCP server returned isError=true"

        return ToolExecutionResult(
            tool_name=tool_name,
            client_name=client_name,
            ok=ok,
            error_type=None if ok else (
                "tool_error" if business_error else "mcp_protocol_error"
            ),
            message=message,
            output=output,
            raw_result=payload
        )

    @staticmethod
    def _dump_result(raw_result):
        if isinstance(raw_result,dict):
            return raw_result

        if hasattr(raw_result,"model_dump"):
            return raw_result.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True
            )

        if hasattr(raw_result,"dict"):
            return raw_result.dict(
                by_alias=True,
                exclude_none=True
            )

        return {
            "content":[
                {
                    "type":"text",
                    "text":str(raw_result)
                }
            ]
        }

    @staticmethod
    def _extract_text_output(content):
        text_content = []

        for item in content:
            if not isinstance(item,dict):
                continue

            if item.get("type") == "text" and isinstance(item.get("text"),str):
                text_content.append(item.get("text"))

        if not text_content:
            return None

        combined = "\n".join(text_content)

        try:
            return json.loads(combined)
        except json.JSONDecodeError:
            return combined

    @staticmethod
    def _error(
            tool_name,
            error_type,
            message,
            client_name=None
    ):
        return ToolExecutionResult(
            tool_name=tool_name,
            client_name=client_name,
            ok=False,
            error_type=error_type,
            message=message,
            output=None,
            raw_result=None
        )

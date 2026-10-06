"""Host-owned operation policy and one-shot terminal approval. No model approval fields."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

from MCP_functions.sandbox import POLICY, PermissionDenied, SandboxRunner, check_path
from MCP_functions.tool_registry import ToolExecutionResult, SHARED_READ_ONLY_TOOLS


PROCESS_TOOLS = {"find_java_exe_tool", "run_java_get_feedback", "check_spring_boot_startup_tool",
                 "run_code_get_feedback_tool", "find_the_python_editor_tool",
                 "download_package_with_confirmation_tool", "package_spring_boot_with_confirmation_tool"}
CONFIRM_TOOLS = {"download_package_with_confirmation_tool", "package_spring_boot_with_confirmation_tool"}
WRITE_TOOLS = {"write_in_tool", "str_replace_tool"}
PATH_ARGUMENTS = {"address", "file_address", "home_address", "project_path", "temp_path",
                  "code_address", "code_path", "jar_path"}


class ExecutionGateway:
    def __init__(self, policy, approve=None, capabilities=None):
        self.policy = policy
        self.approve = approve
        self.capabilities = capabilities or {}
        self.runner = SandboxRunner(policy)

    async def call(self, tool_name, arguments, raw_call=None):
        args = dict(arguments)
        if any(key in args for key in ("confirmed", "approved", "trusted_roots", "policy", "network", "env")):
            return self.error(tool_name, "permission_denied", "model cannot provide approvals or policy")
        if tool_name not in PROCESS_TOOLS | WRITE_TOOLS | SHARED_READ_ONLY_TOOLS | {"judge_spring_project_tool", "get_needed_info_tool"}:
            capability = self.capabilities.get(tool_name)
            if capability is None:
                return self.error(tool_name, "permission_denied", "unknown external capability is denied")
            if not self.policy.network and "network" in capability:
                return self.error(tool_name, "permission_denied", "external networking is not enabled")
            if set(capability) - {"read"}:
                request = self._request(tool_name, args, "external capability", "network" in capability)
                if not await self._approve(request, args):
                    return self.error(tool_name, "authorization_denied", "external operation was not approved")
            return await raw_call() if raw_call else self.error(tool_name, "unknown_tool", "no external route")
        token = POLICY.set(self.policy)
        try:
            args = self._validate_paths(tool_name, args)
            if self.policy.mode != "danger-full-access":
                if "editor_address" in args:
                    args["editor_address"] = self.policy.toolchains["python"]
                if "java_path" in args:
                    args["java_path"] = self.policy.toolchains["java"]
                if "jdk_location" in args:
                    args["jdk_location"] = str(Path(self.policy.toolchains["java"]).parent)
            arguments.clear()
            arguments.update(args)
            if tool_name in CONFIRM_TOOLS:
                request = self._request(tool_name, args, "install/build requires terminal approval", True)
                from State.session_checkpoint import active_run
                run = active_run()
                if run:
                    run.emit("authorization_waiting", "host", tool_name=tool_name, arguments=args, status="confirmation_required")
                if not await self._approve(request, args):
                    return self.error(tool_name, "confirmation_required" if not self.approve else "authorization_denied",
                                      "not executed: terminal approval required/denied", request)
                # A trusted one-shot allowance, not a persisted grant or model argument.
                effective = replace(self.policy, network=True)
                result = await asyncio.to_thread(self._confirmed, tool_name, args, effective)
            elif tool_name in PROCESS_TOOLS and self.policy.mode != "danger-full-access":
                result = await asyncio.to_thread(self._restricted_process, tool_name, args)
            else:
                result = await asyncio.to_thread(self._local, tool_name, args)
            status = str(result.get("status", "success")) if isinstance(result, dict) else "success"
            ok = status not in {"error", "failed", "failure", "unknown", "interrupted", "cancelled", "confirmation_required", "denied"}
            return ToolExecutionResult(tool_name, "host_gateway", ok, None if ok else status,
                                       str(result.get("message", "")) if isinstance(result, dict) else "", result)
        except (PermissionDenied, OSError, ValueError, TypeError) as exc:
            return self.error(tool_name, "permission_denied", str(exc))
        except subprocess.TimeoutExpired:
            return self.error(tool_name, "unknown", "execution timed out and was terminated; effects require inspection, not automatic retry")
        finally:
            POLICY.reset(token)

    def _validate_paths(self, name, args):
        if name == "judge_spring_project_tool":
            def visit(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key in {"address", "path", "root", "file_address"} and isinstance(item, str):
                            self.policy.check_path(item)
                        else:
                            visit(item)
                elif isinstance(value, list):
                    for item in value:
                        visit(item)
            visit(args)
        for key, value in args.items():
            if key in PATH_ARGUMENTS and value is not None:
                write = name in WRITE_TOOLS or key == "temp_path" or (key == "project_path" and name in CONFIRM_TOOLS)
                args[key] = str(self.policy.check_path(value, write))
        return args

    def _request(self, name, args, reason, network):
        from State.session_checkpoint import active_run
        run = active_run()
        identity = {"session": str(run.store.path) if run else "", "chat": run.store.chat_id if run else "",
                    "branch": run.branch if run else "", "policy_version": self.policy.version}
        frozen = json.dumps(args, ensure_ascii=False, sort_keys=True)
        return {"request_id": uuid.uuid4().hex, "tool_name": name, "arguments": json.loads(frozen),
                "arguments_hash": hashlib.sha256(frozen.encode()).hexdigest(), "reason": reason,
                "workspace": self.policy.workspace, "network_required": network,
                "actual_execution": {"backend": "docker" if self.policy.mode != "danger-full-access" else "host-unisolated",
                    "image": self.policy.image, "toolchains": self.policy.toolchains,
                    "install_target": str(Path(self.policy.workspace) / ".agent_packages")}, **identity}

    async def _approve(self, request, args):
        if not self.approve:
            return False
        version = self.policy.version
        result = self.approve(request)
        if inspect.isawaitable(result):
            result = await result
        from State.session_checkpoint import active_run
        run = active_run()
        current = json.dumps(args, ensure_ascii=False, sort_keys=True)
        valid = (result is True and self.policy.version == version
                 and request["arguments_hash"] == hashlib.sha256(current.encode()).hexdigest()
                 and (not run or (request["branch"] == run.branch and request["session"] == str(run.store.path))))
        if run:
            run.emit("authorization", "host", tool_name=request["tool_name"], status="allowed_once" if valid else "denied")
        return valid

    def _result(self, completed, backend="docker"):
        return {"status": "success" if completed.returncode == 0 else "error",
                "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr,
                "backend": backend, "message": "process finished" if completed.returncode == 0 else "process failed"}

    def _restricted_process(self, name, args):
        if name in {"find_the_python_editor_tool", "find_java_exe_tool"}:
            kind = "python" if name == "find_the_python_editor_tool" else "java"
            result = self.runner.run([kind, "--version" if kind == "python" else "-version"])
            return {**self._result(result), "trusted_toolchain": kind, "location": self.policy.toolchains[kind]}
        if name == "run_code_get_feedback_tool":
            return self._result(self.runner.run(["python", args["code_path"]], timeout=10))
        if name == "run_java_get_feedback":
            file = Path(args["code_address"])
            result = self.runner.run(["javac", "-d", str(file.parent), str(file)], timeout=10)
            if result.returncode:
                return self._result(result)
            return self._result(self.runner.run(["java", "-cp", str(file.parent), file.stem], timeout=10))
        if name == "check_spring_boot_startup_tool":
            # Startup is a bounded probe, never leave a server behind.
            import subprocess
            try:
                completed = self.runner.run(["java", "-jar", args["jar_path"], "--server.port=0"],
                                            timeout=args.get("timeout", 30))
                return self._result(completed)
            except subprocess.TimeoutExpired:
                return {"status": "unknown", "message": "startup probe interrupted at timeout; no acceptance claimed"}
        raise ValueError("unsupported process tool")

    def _confirmed(self, name, args, policy):
        runner = SandboxRunner(policy)
        if name == "download_package_with_confirmation_tool":
            requirement = args["package_name"]
            if not isinstance(requirement, str) or not requirement.strip() or requirement.startswith("-"):
                raise ValueError("invalid package requirement")
            if policy.mode == "read-only":
                raise PermissionDenied("install requires workspace-write or explicit full-access")
            target = self.policy.check_path(Path(self.policy.workspace) / ".agent_packages", write=True)
            result = runner.run(["python" if policy.mode != "danger-full-access" else args["editor_address"],
                                 "-m", "pip", "install", "--no-input", "--target", str(target), requirement], timeout=300)
            return {**self._result(result, "docker" if policy.mode != "danger-full-access" else "host-unisolated"),
                    "install_target": str(target)}
        if policy.mode == "read-only":
            raise PermissionDenied("build requires workspace-write or explicit full-access")
        root = Path(args["project_path"])
        tool = "mvn" if (root / "pom.xml").is_file() else "gradle"
        result = runner.run([tool, "package" if tool == "mvn" else "build"], cwd=str(root), timeout=300)
        # Preserve original target-copy behavior, never claim a JAR when no build produced one.
        if result.returncode == 0:
            import shutil
            destination = self.policy.check_path(args["temp_path"], write=True)
            destination.mkdir(parents=True, exist_ok=True)
            artifacts = list((root / ("target" if tool == "mvn" else "build/libs")).glob("*.jar"))
            for artifact in artifacts:
                shutil.copy2(self.policy.check_path(artifact), self.policy.check_path(destination / artifact.name, write=True))
        return self._result(result)

    def _local(self, name, args):
        from MCP_functions.System_Files.Files_function import bfs_read, write_in, java_code, python_code
        mapping = {"read_all_files_tool": bfs_read.read_all_files,
                   "read_files_content_tool": bfs_read.read_files_content,
                   "sort_files_by_suffix_tool": bfs_read.sort_files_by_suffix,
                   "judge_spring_project_tool": java_code.judge_spring_project,
                   "write_in_tool": write_in.write_in, "str_replace_tool": write_in.str_replace,
                   "find_java_exe_tool": java_code.find_java_exe,
                   "run_java_get_feedback": java_code.run_java_get_feedback,
                   "check_spring_boot_startup_tool": java_code.check_spring_boot_startup,
                   "find_the_python_editor_tool": python_code.find_the_python_editor,
                   "run_code_get_feedback_tool": python_code.run_code_get_feedback}
        if name == "read_all_files_tool":
            return bfs_read.read_all_files(**args, max_entries=50)
        if name == "read_files_content_tool":
            return bfs_read.read_files_content(**args, max_chars=8000, max_entries=20)
        if name in {"sort_files_by_suffix_tool", "judge_spring_project_tool"}:
            if set(args) != {"home_address"}:
                raise ValueError("only home_address is allowed")
            scanned = bfs_read.read_all_files(args["home_address"])
            grouped = bfs_read.sort_files_by_suffix(scanned)
            return grouped if name == "sort_files_by_suffix_tool" else java_code.judge_spring_project(grouped)
        if name == "get_needed_info_tool":
            return {"status": "success", "needed": args.get("needed"), "message": "information collection placeholder"}
        function = mapping.get(name)
        if function is None:
            raise ValueError("unknown local tool")
        return function(**args)

    @staticmethod
    def error(name, kind, message, output=None):
        return ToolExecutionResult(name, "host_gateway", False, kind, message,
                                   output or {"status": kind, "message": message})

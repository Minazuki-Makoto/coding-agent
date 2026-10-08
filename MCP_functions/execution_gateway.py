"""Host-owned operation policy and one-shot terminal approval. No model approval fields."""
from __future__ import annotations

import asyncio
import copy
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
APPROVAL_FIELDS = {"confirmed", "approved", "trusted_roots", "policy", "network", "env",
    "permission", "tool_permission", "toolPermission", "is_permitted", "is_permieted",
    "isPermitted", "project_root", "staging_root"}
REQUIRED_ARGUMENTS = {
    "read_all_files_tool": {"home_address"}, "read_files_content_tool": {"home_address"},
    "sort_files_by_suffix_tool": {"home_address"}, "judge_spring_project_tool": {"home_address"},
    "write_in_tool": {"file_address", "code"},
    "str_replace_tool": {"file_address", "old_text", "new_text"},
    "run_java_get_feedback": {"code_address"}, "run_code_get_feedback_tool": {"code_path"},
    "check_spring_boot_startup_tool": {"jar_path"},
    "package_spring_boot_with_confirmation_tool": {"project_path", "temp_path"},
    "download_package_with_confirmation_tool": {"package_name"},
}


def _has_approval_fields(value):
    if isinstance(value, dict):
        return any(key in APPROVAL_FIELDS or _has_approval_fields(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_approval_fields(item) for item in value)
    return False


class ExecutionGateway:
    def __init__(self, policy, approve=None, capabilities=None, project_workflow=None):
        self.policy = policy
        self.approve = approve
        self.capabilities = capabilities or {}
        self.runner = SandboxRunner(policy) if policy else None
        self.project_workflow = project_workflow

    @property
    def per_tool_permissions(self):
        return self.project_workflow is not None and self.project_workflow.per_tool_permissions is True

    def _execution_scope(self, name):
        if name == "get_needed_info_tool":
            return {"backend": "host_placeholder", "network": False}
        from dataclasses import asdict
        return {"backend": "docker" if name in PROCESS_TOOLS | WRITE_TOOLS else "host_read_only",
                "policy": asdict(self.policy), "network_required": name in CONFIRM_TOOLS}

    def _trusted_arguments(self, args):
        args = self._validate_paths("", args)
        for key, kind in (("editor_address", "python"), ("java_path", "java")):
            if key in args:
                args[key] = self.policy.toolchains[kind]
        if "jdk_location" in args:
            args["jdk_location"] = str(Path(self.policy.toolchains["java"]).parent)
        return args

    async def prepare_call(self, name, arguments, actor="executor"):
        """Obtain a one-call grant BEFORE execution or filesystem cache validation."""
        if not self.per_tool_permissions:
            return None
        from State.permissions import RequestDenied
        from State.project_workspace import packed
        workspace = self.project_workflow
        workspace.active_tool_permission = None
        try:
            if workspace.denied:
                raise RequestDenied("当前请求已被拒绝；请重新说明需求")
            if _has_approval_fields(arguments):
                return self.error(name, "permission_denied", "model cannot provide approvals or policy")
            if name not in PROCESS_TOOLS | WRITE_TOOLS | SHARED_READ_ONLY_TOOLS | {"judge_spring_project_tool", "get_needed_info_tool"}:
                return self.error(name, "permission_denied", "external MCP cannot bypass the project staging gateway")
            initial = packed(arguments)
            args = copy.deepcopy(arguments)
            if name in SHARED_READ_ONLY_TOOLS | {"judge_spring_project_tool"} and "address" in args and "home_address" not in args:
                args["home_address"] = args.pop("address")
            missing = REQUIRED_ARGUMENTS.get(name, set()) - args.keys()
            if missing:
                raise ValueError("缺少工具参数：" + ", ".join(sorted(missing)) + "；请重新选择参数")
            for key in REQUIRED_ARGUMENTS.get(name, set()) & PATH_ARGUMENTS:
                if not isinstance(args[key], str) or not args[key].strip():
                    raise ValueError(key + " 必须是非空绝对路径；请重新选择参数")
            if name != "get_needed_info_tool":
                workspace.propose_project(args)
                # Metadata-only validation before copying or asking for a tool grant.
                provisional = workspace.policy(require_authorization=False)
                for key, value in args.items():
                    if key in PATH_ARGUMENTS and value is not None:
                        if not isinstance(value, str) or not Path(value).is_absolute():
                            raise ValueError(key + " 必须是明确绝对路径；不会自动使用当前目录")
                        provisional.check_path(workspace.map_arguments({key: value})[key])
                self.policy = await workspace.ensure(name in PROCESS_TOOLS | WRITE_TOOLS, per_tool=True)
                args = workspace.map_arguments(args)
                args = self._validate_paths(name, args)
                args = self._trusted_arguments(args)
                self.runner = SandboxRunner(self.policy)
            operation = ("write" if name in WRITE_TOOLS else "execute" if name in PROCESS_TOOLS
                         else "information" if name == "get_needed_info_tool" else "read")
            permission = await workspace.authorize_tool(actor, name, args, operation, self._execution_scope(name))
            if packed(arguments) != initial:
                workspace.denied = True
                raise RequestDenied("等待批准时模型工具参数被修改；未执行，请重新说明需求")
            if name != "get_needed_info_tool":
                workspace.read_allowed = True
            if workspace.project_root and not workspace.staging_root and name != "get_needed_info_tool":
                # Persist the candidate only after the user approves this actual call.
                try:
                    workspace._save()
                except Exception as exc:
                    workspace.denied = True
                    raise RequestDenied("工作区授权状态保存失败，工具未执行；当前请求停止") from exc
            arguments.clear()
            arguments.update(args)
            return permission
        except RequestDenied:
            self._close_denied_operation(name)
            raise
        except (PermissionDenied, OSError, ValueError, TypeError) as exc:
            return self.error(name, "invalid_arguments" if isinstance(exc, (ValueError, TypeError)) else "permission_denied", str(exc))

    def consume_permission(self, permission, actor, name, arguments):
        try:
            self.project_workflow.validate_tool_permission(permission, actor, name, arguments,
                self._execution_scope(name), consume=True)
        except Exception:
            self._close_denied_operation(name)
            raise

    @staticmethod
    def _close_denied_operation(name):
        from State.session_checkpoint import active_run
        run = active_run()
        if run and run.current_operation:
            run.emit("tool_completed", "host", operation_id=run.current_operation,
                     tool_name=name, ok=False, status="authorization_denied", executed=False)
            run.current_operation = None

    async def call(self, tool_name, arguments, raw_call=None, *, actor="executor", tool_permission=None):
        if self.per_tool_permissions:
            permission = tool_permission or await self.prepare_call(tool_name, arguments, actor)
            if isinstance(permission, ToolExecutionResult):
                return permission
            self.consume_permission(permission, actor, tool_name, arguments)
        args = dict(arguments)
        if _has_approval_fields(args):
            return self.error(tool_name, "permission_denied", "model cannot provide approvals or policy")
        if self.policy is None and self.project_workflow is None:
            return self.error(tool_name, "permission_denied", "请先选择项目并授权读取")
        if tool_name not in PROCESS_TOOLS | WRITE_TOOLS | SHARED_READ_ONLY_TOOLS | {"judge_spring_project_tool", "get_needed_info_tool"}:
            if self.project_workflow:
                return self.error(tool_name, "permission_denied", "external MCP cannot bypass the project staging gateway")
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
        if self.project_workflow and not self.per_tool_permissions:
            from State.permissions import RequestDenied
            try:
                self.policy = await self.project_workflow.ensure(tool_name in PROCESS_TOOLS | WRITE_TOOLS)
            except RequestDenied:
                from State.session_checkpoint import active_run
                run = active_run()
                if run and run.current_operation:
                    run.emit("tool_completed", "host", operation_id=run.current_operation,
                             tool_name=tool_name, ok=False, status="authorization_denied", executed=False)
                    run.current_operation = None
                raise
            args = self.project_workflow.map_arguments(args)
            self.runner = SandboxRunner(self.policy)
        if self.per_tool_permissions and tool_name == "get_needed_info_tool":
            result = await asyncio.to_thread(self._local, tool_name, args)
            return ToolExecutionResult(tool_name, "host_gateway", True, None, "", result)
        if self.policy is None:
            return self.error(tool_name, "permission_denied", "请先选择项目并授权读取")
        if self.policy.mode == "read-only" and tool_name in PROCESS_TOOLS:
            return self.error(tool_name, "permission_denied", "只读阶段不运行项目代码；必须批准隔离副本")
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
                if run and not self.per_tool_permissions:
                    run.emit("authorization_waiting", "host", tool_name=tool_name, arguments=args, status="confirmation_required")
                if not self.per_tool_permissions and not await self._approve(request, args):
                    return self.error(tool_name, "confirmation_required" if not self.approve else "authorization_denied",
                                      "not executed: terminal approval required/denied", request)
                # A trusted one-shot allowance, not a persisted grant or model argument.
                effective = replace(self.policy, network=True)
                result = await asyncio.to_thread(self._confirmed, tool_name, args, effective)
            elif tool_name in WRITE_TOOLS and self.project_workflow:
                result = await asyncio.to_thread(self._staged_write, tool_name, args)
            elif tool_name in PROCESS_TOOLS and self.policy.mode != "danger-full-access":
                result = await asyncio.to_thread(self._restricted_process, tool_name, args)
            else:
                result = await asyncio.to_thread(self._local, tool_name, args)
            if self.project_workflow and tool_name in PROCESS_TOOLS | WRITE_TOOLS:
                try:
                    self.project_workflow.changed(tool_name, dict(args), result)
                except Exception as exc:
                    # Execution already happened: a metadata error must not make it retryable.
                    return ToolExecutionResult(tool_name, "host_gateway", False, "unknown",
                        "操作已执行但副本元数据保存失败；检查真实效果后选择安全边界，不自动重执行：" + str(exc),
                        {"status": "unknown", "executed": True, "actual_result": result})
            status = str(result.get("status", "success")) if isinstance(result, dict) else "success"
            ok = status not in {"error", "failed", "failure", "unknown", "interrupted", "cancelled", "confirmation_required", "denied"}
            return ToolExecutionResult(tool_name, "host_gateway", ok, None if ok else status,
                                       str(result.get("message", "")) if isinstance(result, dict) else "", result)
        except (PermissionDenied, OSError, ValueError, TypeError) as exc:
            return self.error(tool_name, "permission_denied", str(exc))
        except subprocess.TimeoutExpired:
            if self.project_workflow and self.project_workflow.staging_root:
                try:
                    self.project_workflow.changed(tool_name, dict(args), {"status": "unknown",
                        "message": "execution timed out; effects unknown, inspect before retry"})
                except Exception:
                    pass  # The operation is unknown even when recording its effects also fails.
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
        result = (self.project_workflow._confirm("execute_operation", operation=request)
                  if self.project_workflow else self.approve(request))
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
        if self.project_workflow and not valid:
            from State.permissions import RequestDenied
            if run and run.current_operation:
                run.emit("tool_completed", "host", operation_id=run.current_operation,
                         tool_name=request["tool_name"], ok=False, status="authorization_denied", executed=False)
                run.current_operation = None
            raise RequestDenied("用户拒绝操作授权；当前请求停止，请重新说明需求")
        return valid

    def _result(self, completed, backend="docker"):
        return {"status": "success" if completed.returncode == 0 else "error",
                "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr,
                "backend": backend, "command": list(completed.args), "workspace": self.policy.workspace,
                "message": "process finished" if completed.returncode == 0 else "process failed"}

    def _staged_write(self, name, args):
        """Edit only the mounted staging copy, inside the existing Docker runner."""
        path = self.policy.check_path(args["file_address"], write=True)
        relative = path.relative_to(Path(self.policy.workspace)).as_posix()
        script = '''import json, os, pathlib, tempfile, sys
data = json.loads(sys.argv[1]); path = pathlib.Path('/workspace') / data['path']
try:
    if '..' in path.parts or not path.resolve().is_relative_to(pathlib.Path('/workspace')):
        raise ValueError('unsafe container file path')
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('container symlink write refused')
    if data['name'] == 'write_in_tool':
        content = data['args']['code']
        if not isinstance(content, str): raise ValueError('code must be text')
    else:
        old, new = data['args']['old_text'], data['args']['new_text']
        expected = data['args'].get('expected_replacements', 1)
        if not isinstance(old, str) or not old or not isinstance(new, str) or type(expected) is not int or expected < 1:
            raise ValueError('invalid replacement arguments')
        content = path.read_text(encoding='utf-8')
        if content.count(old) != expected: raise ValueError('replacement count mismatch')
        content = content.replace(old, new)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(content); file.flush(); os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    print(json.dumps({'status':'success', 'bytes_written':len(content.encode('utf-8'))}))
except Exception as error:
    print(json.dumps({'status':'error', 'message':str(error)})); sys.exit(1)
'''
        completed = self.runner.run(["python", "-c", script, json.dumps({"path": relative, "name": name, "args": args})])
        result = self._result(completed)
        try:
            result.update(json.loads(completed.stdout))
        except ValueError:
            result["status"], result["message"] = "error", "container write returned invalid result"
        result["file_address"] = str(path)
        return result

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
            # A trusted container-side probe captures bounded logs and stops the
            # child in finally; timeout alone is never a successful startup.
            probe = '''import collections,json,subprocess,sys,threading,time,socket
logs=collections.deque(maxlen=20); started=threading.Event()
process=subprocess.Popen([sys.argv[1],'-jar',sys.argv[2],'--server.port=18080'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
def drain():
    for line in process.stdout:
        logs.append(line[:500])
        if 'Started ' in line: started.set()
thread=threading.Thread(target=drain,daemon=True); thread.start(); healthy=False
try:
    deadline=time.monotonic()+float(sys.argv[3])
    while time.monotonic()<deadline and process.poll() is None:
        if started.is_set():
            try:
                with socket.create_connection(('127.0.0.1',18080),timeout=0.5): healthy=True; break
            except OSError: pass
        time.sleep(0.2)
finally:
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
    thread.join(timeout=1)
print(json.dumps({'status':'success' if healthy else 'error','startup_marker':started.is_set(),'tcp_health':healthy,'logs':''.join(logs),'stopped':process.poll() is not None}))
sys.exit(0 if healthy else 1)
'''
            timeout = min(max(float(args.get("timeout", 30)), 1), 120)
            jar = args["jar_path"]
            completed = self.runner.run(["python", "-c", probe, self.policy.toolchains["java"], jar, str(timeout)], timeout=timeout + 8)
            result = self._result(completed)
            try:
                result.update(json.loads(completed.stdout))
            except ValueError:
                result["status"] = "error"
            return result
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
        wrapper = root / ("mvnw" if tool == "mvn" else "gradlew")
        command = ["sh", str(wrapper)] if wrapper.is_file() and policy.mode != "danger-full-access" else [tool]
        result = runner.run([*command, "package" if tool == "mvn" else "build"], cwd=str(root), timeout=300)
        # Preserve original target-copy behavior, never claim a JAR when no build produced one.
        artifacts, copied = [], []
        if result.returncode == 0:
            import shutil
            destination = self.policy.check_path(args["temp_path"], write=True)
            destination.mkdir(parents=True, exist_ok=True)
            artifacts = list((root / ("target" if tool == "mvn" else "build/libs")).glob("*.jar"))
            for artifact in artifacts:
                shutil.copy2(self.policy.check_path(artifact), self.policy.check_path(destination / artifact.name, write=True))
                copied.append(str(destination / artifact.name))
        output = self._result(result)
        output.update(artifacts=[str(p) for p in artifacts], copied_artifacts=copied,
                      artifact_found=bool(artifacts), artifact_copy_status="copied" if copied else "not_produced")
        return output

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

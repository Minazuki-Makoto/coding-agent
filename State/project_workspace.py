"""Host-owned read -> staging -> immutable review -> conflict-checked apply.

No model-callable approval or apply tool exists. Filesystem checks reduce races;
they are not a transaction against arbitrary concurrent external programs.
"""
from __future__ import annotations
import base64
import contextvars
import difflib
import fnmatch
import hashlib
import inspect
import json
import os
import stat
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from MCP_functions.sandbox import SandboxPolicy, PermissionDenied
from MCP_functions.System_Files.Files_function.write_in import _atomic_write
from State.permissions import Permission, ToolPermission, RequestDenied

WORKSPACE = contextvars.ContextVar("coding_agent_project_workspace", default=None)
DEFAULT_EXCLUDES = (".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
                    ".gradle", "target", "build", "dist", ".agent_packages", ".env", ".env.*",
                    "INFORMATION.json", "*.pem", "*.key", "id_rsa", ".npmrc", "*.log",
                    "*.pyc", "*.class", "*.jar", "artifacts", ".aws", ".ssh", ".kube", ".docker", ".m2",
                    ".git-credentials", ".pypirc", "id_ed25519", "credentials*.json", "secrets.*",
                    "service-account*.json", "*.p12", "*.pfx", "*.jks")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def safe_target(root, relative):
    """Check each lexical component before resolving; reject junctions too."""
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(p in {"..", "."} or ":" in p for p in rel.parts):
        raise PermissionDenied("unsafe relative project path")
    root = Path(root)
    candidate = root
    for index, component in enumerate((None, *rel.parts)):
        if component is not None:
            # Windows paths are case-insensitive; forbid collisions even on Linux.
            if candidate.is_dir():
                matches = [p.name for p in candidate.iterdir() if p.name.casefold() == component.casefold()]
                if matches and matches != [component]:
                    raise PermissionDenied("case-insensitive path collision")
            candidate /= component
        if candidate.exists() or candidate.is_symlink():
            if is_link(candidate):
                raise PermissionDenied("symlink/junction/reparse point is not allowed")
            if index < len(rel.parts) and not candidate.is_dir():
                raise PermissionDenied("project parent is not a directory")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise PermissionDenied("path escapes project root")
    return candidate


def atomic_bytes(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, prefix=".agent-", suffix=".tmp") as file:
            name = file.name
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if mode is not None:
            os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


class ProjectWorkspace:
    def __init__(self, session, chat, branch, request_id, state_dir, project_root=None,
                 approve=None, select_project=None, protected=(), image="coding-agent-sandbox:local",
                 toolchains=None, network=False, excludes=DEFAULT_EXCLUDES, per_tool_permissions=False):
        for identity in (chat, branch, request_id):
            if not isinstance(identity, str) or not identity or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in identity):
                raise PermissionDenied("unsafe workspace session/chat/branch/request identity")
        self.session, self.chat, self.branch, self.request_id = str(Path(session).resolve()), chat, branch, request_id
        self.state_dir = str(Path(state_dir).resolve())
        self.project_root = str(Path(project_root).resolve()) if project_root else None
        self.staging_root = None
        self.approve, self.select_project = approve, select_project
        self.protected = tuple(str(Path(p).resolve()) for p in protected)
        self.image, self.toolchains, self.network = image, toolchains, network
        self.excludes = tuple(excludes)
        self.policy_version = uuid.uuid4().hex
        self.read_allowed = self.read_denied = self.staging_denied = False
        self.staging_allowed = self.denied = False
        self.network_allowed = False
        self.permissions = []
        self.per_tool_permissions = per_tool_permissions
        self.tool_permissions = []
        self.active_tool_permission = None
        self.workspace_hash = None
        self.scan_directories = []
        self.revision = 0
        self.manifest = None
        self.tests = []
        self.stage_parent = Path(self.state_dir).parent / (Path(self.state_dir).name + "-staging")
        self.metadata = Path(self.session) / "chats" / chat / "workspaces" / branch / request_id
        self.binding = {"session": self.session, "chat": chat, "branch": branch,
                        "request_id": request_id, "policy_version": self.policy_version}

    def _save(self):
        data = {"project_root": self.project_root, "staging_root": self.staging_root,
                "binding": self.binding, "manifest": self.manifest, "revision": self.revision,
                "tests": self.tests, "excludes": self.excludes,
                "permissions": [p.__dict__ for p in self.permissions]}
        data["tool_permissions"] = [p.__dict__ for p in self.tool_permissions]
        if self.staging_root:
            self.workspace_hash = self.tree_hash(self.staging_root)
        data["workspace_hash"] = self.workspace_hash
        _atomic_write(self.metadata / "workspace.json", json.dumps(data, ensure_ascii=False, indent=2))
        from State.session_checkpoint import active_run
        run = active_run()
        if run and run.bundle:
            agent = run.bundle["agent"]
            agent.project_root, agent.staging_root = self.project_root, self.staging_root
            agent.workspace_revision = self.revision
            agent.workspace_hash = self.workspace_hash

    async def _confirm(self, action, **details):
        request = {"action": action, **self.binding, **details}
        expected = packed(request)
        identity = self._permission_identity()
        value = self.approve(request) if self.approve else False
        if inspect.isawaitable(value):
            value = await value
        allowed = value is True and packed(request) == expected and identity == self._permission_identity()
        self.permissions.append(Permission(action, self.project_root or "", self.session, self.chat,
            self.branch, self.request_id, self.policy_version, digest(expected), allowed))
        _atomic_write(self.metadata / "permissions.json", json.dumps([p.__dict__ for p in self.permissions], ensure_ascii=False, indent=2))
        if not allowed:
            self.denied = True
        from State.session_checkpoint import active_run
        run = active_run()
        if run:
            run.emit("authorization", "host", action=action, project_root=self.project_root,
                     status="allowed_once" if allowed else "denied", policy_version=self.policy_version)
        return allowed

    def _permission_identity(self):
        from State.session_checkpoint import active_run
        run = active_run()
        return packed({"binding": self.binding, "session": self.session, "chat": self.chat,
            "branch": self.branch, "request_id": self.request_id, "version": self.policy_version,
            "project": self.project_root, "stage": self.staging_root, "image": self.image,
            "toolchains": self.toolchains, "network": self.network,
            "active_run": (str(run.store.path.resolve()), run.store.chat_id, run.branch,
                run.bundle["agent"].request_id if run.bundle else None) if run else None})

    def _save_tool_permissions(self):
        _atomic_write(self.metadata / "tool_permissions.json",
            json.dumps([p.__dict__ for p in self.tool_permissions], ensure_ascii=False, indent=2))

    async def authorize_tool(self, actor, name, arguments, operation, execution):
        """Only this trusted callback may turn a fresh False decision into True."""
        if self.denied:
            raise RequestDenied("当前请求已被拒绝；请重新说明需求")
        self.active_tool_permission = None
        frozen_arguments = packed(arguments)
        permission = ToolPermission(uuid.uuid4().hex, name, actor, operation, self.session, self.chat,
            self.branch, self.request_id, self.policy_version, self.project_root, self.staging_root,
            digest(frozen_arguments), digest(packed(execution)))
        self.tool_permissions.append(permission)
        try:
            self._save_tool_permissions()
        except Exception as exc:
            self.denied = True
            raise RequestDenied("授权记录保存失败，工具未执行；当前请求停止") from exc
        identity = self._permission_identity()
        request = {"action": "tool_call", **self.binding, "permission_id": permission.permission_id,
            "actor": actor, "tool_name": name, "operation": operation,
            "project_root": self.project_root, "staging_root": self.staging_root,
            "arguments": json.loads(frozen_arguments), "arguments_hash": permission.arguments_hash,
            "actual_execution": json.loads(packed(execution)), "is_permitted": False}
        expected = packed(request)
        from State.session_checkpoint import active_run
        run = active_run()
        if run:
            run.emit("authorization_waiting", "host", tool_name=name, requested_actor=actor,
                     permission_id=permission.permission_id, status="confirmation_required")
        try:
            value = self.approve(request) if self.approve else False
            if inspect.isawaitable(value):
                value = await value
        except (EOFError, KeyboardInterrupt):
            value = False
        except Exception as exc:
            self.denied = True
            raise RequestDenied("授权交互失败，工具未执行；当前请求停止") from exc
        allowed = (value is True and packed(request) == expected and packed(arguments) == frozen_arguments
                   and packed(execution) == packed(request["actual_execution"])
                   and self._permission_identity() == identity)
        decision = replace(permission, is_permitted=allowed)
        self.tool_permissions[-1] = decision
        try:
            self._save_tool_permissions()
        except Exception as exc:
            self.denied = True
            raise RequestDenied("授权记录保存失败，工具未执行；当前请求停止") from exc
        if run:
            run.emit("authorization", "host", tool_name=name, permission_id=decision.permission_id,
                     status="allowed_once" if allowed else "denied")
        if not allowed:
            self.denied = True
            raise RequestDenied("用户未批准本次工具调用或调用范围发生变化；当前请求停止，请重新说明需求")
        self.active_tool_permission = decision
        return decision

    def validate_tool_permission(self, permission, actor, name, arguments, execution, *, consume=False):
        valid = (not self.denied and permission is self.active_tool_permission
            and permission is not None and permission.is_permitted and not permission.consumed
            and permission.actor == actor and permission.tool_name == name
            and permission.arguments_hash == digest(packed(arguments))
            and permission.execution_hash == digest(packed(execution))
            and (permission.session_address, permission.chat_id, permission.branch_id,
                 permission.request_id, permission.policy_version, permission.project_root, permission.staging_root)
                == (self.session, self.chat, self.branch, self.request_id, self.policy_version,
                    self.project_root, self.staging_root))
        from State.session_checkpoint import active_run
        run = active_run()
        if run:
            valid = valid and (str(run.store.path.resolve()), run.store.chat_id, run.branch) == (self.session, self.chat, self.branch)
            if run.bundle:
                valid = valid and run.bundle["agent"].request_id == self.request_id
        if not valid:
            self.denied = True
            raise RequestDenied("本次工具授权无效、已消费或参数发生变化；未执行工具，请重新说明需求")
        if consume:
            decision = replace(permission, consumed=True)
            index = next(i for i, item in enumerate(self.tool_permissions) if item is permission)
            self.tool_permissions[index] = decision
            self.active_tool_permission = decision
            try:
                self._save_tool_permissions()
            except Exception as exc:
                self.active_tool_permission = None
                self.denied = True
                raise RequestDenied("授权消费记录保存失败，工具未执行；当前请求停止") from exc

    def propose_project(self, arguments):
        """Infer a candidate from explicit tool paths, never from cwd or an interpreter."""
        if self.project_root:
            return
        for key in ("project_path", "home_address", "address", "file_address", "code_path", "code_address", "jar_path"):
            value = arguments.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            path = Path(value)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("工具必须提供明确绝对路径，不得包含 ..；请重新选择工具参数")
            for item in (*path.parents, path):
                if (item.exists() or item.is_symlink()) and is_link(item):
                    raise PermissionDenied("symlink/junction/reparse paths are not allowed")
            root = path.parent if key in {"file_address", "code_path", "code_address", "jar_path"} or path.is_file() else path
            if not root.is_dir() or root.parent == root or root == Path.home():
                raise ValueError("工具路径不能确定有效项目目录；请提供具体目录或文件绝对路径")
            if any(root.is_relative_to(Path(p)) for p in (self.state_dir, self.session, self.stage_parent, *self.protected)):
                raise PermissionDenied("Host state/config/staging cannot be selected as a project")
            self.project_root = str(root.resolve())
            return
        raise ValueError("缺少项目绝对路径；请从用户需求取得路径并重新选择工具，不会自动使用当前目录")

    async def ensure(self, needs_staging=False, *, per_tool=False):
        if not per_tool and not self.project_root and self.select_project:
            value = self.select_project()
            if inspect.isawaitable(value):
                value = await value
            if value:
                self.project_root = str(Path(value).resolve())
        if not self.project_root:
            self.denied = True
            raise RequestDenied("请先选择项目并授权读取：/project <绝对路径>；当前请求已停止，请重新说明需求")
        root = Path(self.project_root)
        if not root.is_dir() or root.parent == root or root == Path.home() or is_link(root):
            raise PermissionDenied("项目必须是明确目录，不能是盘符根、用户目录或链接")
        if not self.read_allowed and not per_tool:
            if self.read_denied or not await self._confirm("read_project", project_root=self.project_root,
                                                         scope="read-only; no project code execution"):
                self.read_denied = True
                raise RequestDenied("读取授权被拒绝；当前请求停止，未读取、复制或执行项目内容。请重新说明需求。")
            self.read_allowed = True
            self._save()
        if (needs_staging or self.staging_root) and not self.staging_allowed:
            stage = Path(self.staging_root) if self.staging_root else self.stage_parent / uuid.uuid4().hex / "workspace"
            if self.staging_denied or not await self._confirm("create_staging", project_root=self.project_root,
                                                              staging_root=str(stage), network=False,
                                                              resume_existing=bool(self.staging_root)):
                self.staging_denied = True
                raise RequestDenied("副本授权被拒绝；当前请求停止，不修改或执行项目代码。请重新说明只读需求。")
            # The separate copy approval includes reading source bytes for the baseline.
            self.read_allowed = True
            from MCP_functions.sandbox import SandboxRunner
            if not SandboxRunner(self.policy()).availability()["available"]:
                raise PermissionDenied("Docker/image unavailable; no host fallback; 副本执行受阻")
            if not self.staging_root:
                self.create_staging(stage)
            self.staging_allowed = True
        if self.staging_root and self.network and not self.network_allowed:
            if not await self._confirm("enable_network", project_root=self.project_root,
                                       staging_root=self.staging_root, scope="container network on/off"):
                raise RequestDenied("网络授权被拒绝；当前请求停止，请重新说明离线需求")
            self.network_allowed = True
        return self.policy(require_authorization=not per_tool)

    def policy(self, *, require_authorization=True):
        workspace = self.staging_root or self.project_root
        if not workspace or (require_authorization and not self.read_allowed):
            raise PermissionDenied("请先选择项目并授权读取")
        values = {"workspace": workspace, "state_dir": self.state_dir,
                  "mode": "workspace-write" if self.staging_root else "read-only",
                  "protected_paths": self.protected + ((self.project_root,) if self.staging_root else ()),
                  "network": self.network and self.network_allowed, "image": self.image, "version": self.policy_version}
        if self.toolchains:
            values["toolchains"] = self.toolchains
        return SandboxPolicy(**values)

    def map_arguments(self, arguments):
        def convert(value):
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, list):
                return [convert(v) for v in value]
            if self.staging_root and isinstance(value, str) and Path(value).is_absolute():
                path = Path(value)
                if ".." in path.parts:
                    raise PermissionDenied("parent traversal is not allowed")
                if path.is_relative_to(Path(self.project_root)):
                    return str(Path(self.staging_root) / path.relative_to(self.project_root))
            return value
        return convert(arguments)

    def _excluded(self, relative, path):
        if any(path.resolve().is_relative_to(Path(p)) for p in (self.state_dir, *self.protected)):
            return True
        if relative.name.casefold() == ".env.example":
            return False
        if relative.name.casefold() in {"gradle-wrapper.jar", "maven-wrapper.jar"}:
            return False
        if any(fnmatch.fnmatchcase(relative.as_posix().casefold(), rule.casefold()) or
               any(fnmatch.fnmatchcase(part.casefold(), rule.casefold()) for part in relative.parts)
               for rule in self.excludes):
            return True
        return False

    def scan(self, root):
        root = Path(root)
        files, excluded, seen = {}, [], set()
        directories = []
        def visit(folder):
            for path in sorted(folder.iterdir()):
                rel = path.relative_to(root)
                if self._excluded(rel, path):
                    excluded.append(rel.as_posix())
                    continue
                key = rel.as_posix().casefold()
                if key in seen:
                    raise PermissionDenied("case-insensitive source collision")
                seen.add(key)
                if is_link(path):
                    excluded.append(rel.as_posix() + " [link/reparse point]")
                    continue
                info = path.stat()
                if path.is_dir():
                    directories.append(rel.as_posix())
                    visit(path)
                elif stat.S_ISREG(info.st_mode):
                    data = path.read_bytes()
                    files[rel.as_posix()] = {"sha256": digest(data), "mode": stat.S_IMODE(info.st_mode),
                                            "type": "file", "size": len(data)}
                else:
                    excluded.append(rel.as_posix() + " [special file]")
        visit(root)
        self.scan_directories = directories
        return files, excluded

    def tree_hash(self, root):
        files, excluded = self.scan(root)
        return digest(packed({"files": files, "excluded": excluded, "directories": self.scan_directories}))

    def create_staging(self, stage):
        if not self.read_allowed:
            raise PermissionDenied("cannot copy before read authorization")
        stage = Path(stage).resolve()
        if not stage.is_relative_to(self.stage_parent.resolve()) or stage.is_relative_to(Path(self.project_root)):
            raise PermissionDenied("staging must be a dedicated directory outside the real project")
        before, excluded = self.scan(self.project_root)
        directories = list(self.scan_directories)
        baseline_bytes = {}
        stage.mkdir(parents=True, exist_ok=False)
        os.chmod(stage, 0o777)  # Dedicated staging mount, writable by container uid 65534.
        for relative in directories:
            target = safe_target(stage, relative)
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, 0o777)
        for relative, info in before.items():
            source = safe_target(self.project_root, relative)
            data = source.read_bytes()
            if digest(data) != info["sha256"]:
                raise RuntimeError("复制期间源文件发生变化：" + relative)
            baseline_bytes[relative] = base64.b64encode(data).decode()
            try:
                data.decode("utf-8-sig")
                info["encoding"] = "utf-8"
            except UnicodeError:
                info["encoding"] = "binary/unknown"
            atomic_bytes(safe_target(stage, relative), data, info["mode"])
            target = stage / relative
            os.chmod(target, 0o666 | (info["mode"] & 0o111))
            for parent in target.parents:
                if parent == stage:
                    break
                os.chmod(parent, 0o777)
        after, after_excluded = self.scan(self.project_root)
        if ({k: {f: v[f] for f in v if f != "encoding"} for k, v in before.items()} != after
                or excluded != after_excluded or directories != self.scan_directories):
            raise RuntimeError("复制期间源项目发生变化；副本未被认定为可靠基线")
        self.staging_root = str(stage)
        self.manifest = {"files": before, "excluded": excluded, "project_root": self.project_root,
                         "staging_root": str(stage), "binding": dict(self.binding), "baseline_bytes": baseline_bytes,
                         "directories": directories}
        self.revision += 1
        self._save()

    def restore(self, agent):
        # Permission fields are intentionally never restored.
        self.project_root = agent.project_root or self.project_root
        if not agent.staging_root:
            return
        file = self.metadata / "workspace.json"
        if not file.is_file():
            raise RuntimeError("该分支/步骤没有可重建的副本快照；不能伪装恢复文件状态，请新建请求")
        data = json.loads(file.read_text(encoding="utf-8"))
        stage = Path(data["staging_root"]).resolve()
        binding = data["binding"]
        self.excludes = tuple(data["excludes"])
        if any(binding[k] != self.binding[k] for k in ("session", "chat", "branch", "request_id")):
            raise PermissionDenied("workspace identity mismatch")
        if (not stage.is_relative_to(self.stage_parent.resolve()) or not stage.is_dir()
                or str(stage) != agent.staging_root or is_link(stage) or data["revision"] != agent.workspace_revision
                or self.tree_hash(stage) != agent.workspace_hash):
            raise RuntimeError("副本缺失或版本与检查点不一致；请检查后重新生成，不能自动重放")
        self.staging_root, self.manifest, self.revision = str(stage), data["manifest"], data["revision"]
        if self.manifest["project_root"] != self.project_root or self.manifest["staging_root"] != str(stage):
            raise PermissionDenied("baseline/project identity mismatch")
        self.tests = data.get("tests", [])
        self.excludes = tuple(data["excludes"])
        for name, info in self.manifest["files"].items():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise PermissionDenied("unsafe baseline path")
            if digest(base64.b64decode(self.manifest["baseline_bytes"][name], validate=True)) != info["sha256"]:
                raise PermissionDenied("baseline integrity failure")

    def changed(self, tool_name, arguments, result):
        self.revision += 1
        # Store verification facts/excerpts, not another full copy of the raw tool result.
        facts = {key: result[key] for key in ("status", "returncode", "backend", "command", "workspace",
                 "artifacts", "copied_artifacts", "artifact_found", "artifact_copy_status") if key in result}
        for key in ("message", "stdout", "stderr"):
            if key in result:
                facts[key + "_excerpt"] = str(result[key])[:2000]
        from State.session_checkpoint import active_run
        run = active_run()
        self.tests.append({"tool_name": tool_name, "arguments": arguments, "result": facts,
                           "operation_id": run.current_operation if run else None,
                           "environment": "docker", "workspace": self.staging_root,
                           "revision": self.revision})
        self._save()

    def cache_scope(self, name, arguments):
        args = self.map_arguments(arguments)
        versions = []
        read = name in {"read_all_files_tool", "read_files_content_tool", "sort_files_by_suffix_tool", "judge_spring_project_tool"}
        permission = self.active_tool_permission
        authorized = (not self.per_tool_permissions or
            (permission is not None and permission.is_permitted and not self.denied
             and permission.tool_name == name and permission.arguments_hash == digest(packed(args))
             and permission.project_root == self.project_root and permission.staging_root == self.staging_root))
        if self.read_allowed and read and authorized:
            for key in ("address", "file_address", "home_address", "project_path", "code_path", "code_address"):
                if isinstance(args.get(key), str):
                    path = self.policy().check_path(args[key])
                    if path.is_file():
                        if self._excluded(path.relative_to(Path(self.policy().workspace)), path):
                            versions.append((str(path), "excluded/protected"))
                        elif name in {"read_files_content_tool", "judge_spring_project_tool"}:
                            versions.append((str(path), digest(path.read_bytes())))
                        else:
                            info = path.stat()
                            versions.append((str(path), info.st_size, info.st_mtime_ns))
                    elif path.is_dir():
                        # Listing and file-body reads have different invalidation scopes.
                        entries = []
                        for child in sorted(path.iterdir()):
                            if self._excluded(child.relative_to(Path(self.policy().workspace)), child):
                                entries.append((child.name, "excluded/protected"))
                            elif is_link(child):
                                entries.append((child.name, "link"))
                            elif name == "read_files_content_tool" and child.is_file():
                                entries.append((child.name, digest(child.read_bytes())))
                            else:
                                info = child.stat()
                                entries.append((child.name, child.is_dir(), info.st_size, info.st_mtime_ns))
                        versions.append((str(path), entries))
        return {"workspace": self.staging_root or self.project_root, "branch": self.branch,
                "request_id": self.request_id, "revision": self.revision if read else None, "arguments": args, "versions": versions,
                "execution_environment": {"image": self.image, "toolchains": self.toolchains} if name in {
                    "find_java_exe_tool", "run_java_get_feedback", "check_spring_boot_startup_tool",
                    "run_code_get_feedback_tool", "find_the_python_editor_tool",
                    "download_package_with_confirmation_tool", "package_spring_boot_with_confirmation_tool"} else None}

    def make_package(self):
        if not self.staging_root or not self.manifest:
            raise ValueError("没有已批准的隔离副本")
        current, excluded = self.scan(self.staging_root)
        baseline = self.manifest["files"]
        changes = []
        skipped = [p for p in excluded if "[" in p]
        for relative in sorted(set(baseline) | set(current)):
            old, new = baseline.get(relative), current.get(relative)
            if old and new and old["sha256"] == new["sha256"]:
                continue
            stage_path = Path(self.staging_root) / relative
            if old and new is None and (stage_path.exists() or stage_path.is_symlink()):
                skipped.append(relative + " [file type changed; not auto-applied]")
                continue
            if relative in excluded or any(relative.startswith(p.split(" [")[0] + "/") for p in excluded):
                skipped.append(relative + " [excluded/type changed; not deleted]")
                continue
            # Diff uses the saved baseline, not changed real files. Baseline bytes
            # come from a separate immutable snapshot stored at copy time.
            before = base64.b64decode(self.manifest.get("baseline_bytes", {}).get(relative, "")) if old else b""
            after = safe_target(self.staging_root, relative).read_bytes() if new else b""
            if new and digest(after) != new["sha256"]:
                raise RuntimeError("生成变更包期间副本发生变化，请重新 /diff：" + relative)
            try:
                if max(len(before), len(after)) > 1024 * 1024:
                    raise ValueError("large file")
                a, b = before.decode("utf-8-sig"), after.decode("utf-8-sig")
                if "\x00" in a + b:
                    raise ValueError("binary")
            except (UnicodeError, ValueError):
                skipped.append(relative + " [binary/large file; not auto-applied]")
                continue
            changes.append({"path": relative, "action": "delete" if new is None else "add" if old is None else "modify",
                            "before": old, "after": ({**new, "mode": old["mode"] if old else 0o644} if new else None),
                            "bytes": base64.b64encode(after).decode(),
                            "diff": "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                                for line in difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                    fromfile="baseline/" + relative, tofile="staging/" + relative))})
        body = {"binding": dict(self.binding), "project_root": self.project_root,
                "staging_root": self.staging_root, "revision": self.revision,
                "staging_hash": digest(packed({"files": current, "excluded": excluded, "directories": self.scan_directories})),
                "baseline_hash": digest(packed(self.manifest)), "changes": changes,
                "skipped": skipped, "tests": list(self.tests), "excluded": self.manifest["excluded"]}
        package_id = "cs_" + digest(packed(body))
        package = {"change_set_id": package_id, "body": body}
        target = self.metadata / "packages" / (package_id + ".json")
        if target.exists() and json.loads(target.read_text(encoding="utf-8")) != package:
            raise RuntimeError("immutable package collision")
        _atomic_write(target, json.dumps(package, ensure_ascii=False, indent=2))
        return package

    def load_package(self, package_id):
        if not package_id.startswith("cs_") or len(package_id) != 67 or any(c not in "0123456789abcdef" for c in package_id[3:]):
            raise ValueError("invalid change_set_id")
        package = json.loads((self.metadata / "packages" / (package_id + ".json")).read_text(encoding="utf-8"))
        if package["change_set_id"] != "cs_" + digest(packed(package["body"])) or package["change_set_id"] != package_id:
            raise PermissionDenied("change package integrity failure")
        body = package["body"]
        if body["project_root"] != self.project_root or any(body["binding"][k] != self.binding[k] for k in ("session", "chat", "branch", "request_id")):
            raise PermissionDenied("change package identity mismatch")
        return package

    def apply(self, package, approved=False):
        if approved is not True:
            raise PermissionDenied("真实项目写入需要 Host 对固定变更包单独批准")
        from State.session_checkpoint import single_writer
        root = self.project_root.casefold() if os.name == "nt" else self.project_root
        lock_dir = Path(self.state_dir) / ".project_apply_locks" / digest(root.encode("utf-8"))
        # Cooperating sessions in the same state root serialize project applies.
        # Independent state roots and arbitrary editors still require hash checks.
        with single_writer(lock_dir):
            return self._apply_locked(package, approved)

    def _apply_locked(self, package, approved=False):
        if approved is not True:
            raise PermissionDenied("真实项目写入需要 Host 对固定变更包单独批准")
        fixed = self.load_package(package["change_set_id"])
        if fixed != package:
            raise PermissionDenied("reviewed package changed")
        body = fixed["body"]
        if body["skipped"]:
            raise PermissionDenied("存在无法可靠审阅/应用的文件；初版不自动应用：" + str(body["skipped"]))
        changes = body["changes"]
        if not changes:
            raise ValueError("没有源码改动")
        journal_path = self.metadata / "transactions" / (fixed["change_set_id"] + ".json")
        if journal_path.exists():
            raise RuntimeError("该变更包已有应用事务，不能重放；请 /apply-status 核对")
        def check(change):
            path = safe_target(self.project_root, change["path"])
            if self._excluded(Path(change["path"]), path):
                raise PermissionDenied("protected/excluded application target")
            before = change["before"]
            if before is None:
                if path.exists():
                    raise RuntimeError("新增路径冲突：" + change["path"])
            elif not path.is_file() or digest(path.read_bytes()) != before["sha256"] or stat.S_IMODE(path.stat().st_mode) != before["mode"]:
                raise RuntimeError("原文件已变化：" + change["path"])
            return path
        for change in changes:
            check(change)
        journal = {"change_set_id": fixed["change_set_id"], "binding": self.binding,
                   "status": "applying", "files": [], "created_directories": []}
        for change in changes:
            path = check(change)
            journal["files"].append({"path": change["path"], "status": "pending",
                                     "before_bytes": base64.b64encode(path.read_bytes()).decode() if path.exists() else None,
                                     "before": change["before"], "after": change["after"]})
        def save():
            _atomic_write(journal_path, json.dumps(journal, ensure_ascii=False, indent=2))
        save()
        try:
            for change, record in zip(changes, journal["files"]):
                target = check(change)
                record["status"] = "writing"
                save()
                missing_parents = []
                for parent in target.parents:
                    if parent == Path(self.project_root):
                        break
                    if not parent.exists():
                        missing_parents.append(parent)
                for parent in reversed(missing_parents):
                    safe_target(self.project_root, str(parent.relative_to(self.project_root)))
                    parent.mkdir()
                    journal["created_directories"].append({"path": parent.relative_to(self.project_root).as_posix(), "status": "created"})
                    save()
                if change["action"] == "delete":
                    target.unlink()
                else:
                    data = base64.b64decode(change["bytes"], validate=True)
                    if digest(data) != change["after"]["sha256"]:
                        raise PermissionDenied("package bytes hash mismatch")
                    atomic_bytes(target, data, change["before"]["mode"] if change["before"] else 0o644)
                if change["after"] is None:
                    if target.exists() or target.is_symlink():
                        raise RuntimeError("删除后目标再次出现：" + change["path"])
                elif not target.is_file() or digest(target.read_bytes()) != change["after"]["sha256"]:
                    raise RuntimeError("应用后内容校验失败：" + change["path"])
                record["status"] = "written"
                save()
            journal["status"] = "applied"
            save()
            return journal
        except BaseException as exc:
            journal["status"], journal["error"] = "failed", str(exc)
            for record in reversed(journal["files"]):
                if record["status"] not in {"writing", "written"}:
                    continue
                try:
                    target = safe_target(self.project_root, record["path"])
                    after = record["after"]
                    at_after = not target.exists() if after is None else target.is_file() and digest(target.read_bytes()) == after["sha256"]
                    before = record["before"]
                    at_before = not target.exists() if before is None else target.is_file() and digest(target.read_bytes()) == before["sha256"]
                    if at_before:
                        record["status"] = "unchanged"
                    elif not at_after:
                        record["status"] = "manual_recovery_required"
                    elif record["before_bytes"] is None:
                        target.unlink()
                        record["status"] = "restored"
                    else:
                        atomic_bytes(target, base64.b64decode(record["before_bytes"]), before["mode"])
                        record["status"] = "restored"
                except BaseException as restore_error:
                    record["status"], record["restore_error"] = "manual_recovery_required", str(restore_error)
            for directory in reversed(journal["created_directories"]):
                try:
                    target = safe_target(self.project_root, directory["path"])
                    target.rmdir()  # Only empty directories; never recursive deletion.
                    directory["status"] = "removed"
                except OSError as directory_error:
                    directory["status"], directory["error"] = "retained_for_inspection", str(directory_error)
            save()
            raise RuntimeError("应用失败；已尝试恢复，请 /apply-status 核对：" + str(exc)) from exc

    def transaction_status(self):
        result = []
        for path in (self.metadata / "transactions").glob("*.json"):
            journal = json.loads(path.read_text(encoding="utf-8"))
            for record in journal["files"]:
                record.pop("before_bytes", None)
                try:
                    target = safe_target(self.project_root, record["path"])
                    record["current_sha256"] = digest(target.read_bytes()) if target.is_file() else None
                except (OSError, PermissionDenied) as exc:
                    record["inspection_error"] = str(exc)
            result.append(journal)
        return result

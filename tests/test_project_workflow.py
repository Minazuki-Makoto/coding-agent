"""Real temporary files + simulated models/runners; never user projects."""
import asyncio
import base64
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from AgentLoop.agent import RuntimeConfig, main
from AgentLoop.executor_loop import _tool_call_signature
from MCP_functions.execution_gateway import ExecutionGateway
from MCP_functions.sandbox import SandboxRunner, PermissionDenied
from State.permissions import Permission, RequestDenied
from State.project_workspace import ProjectWorkspace, WORKSPACE, atomic_bytes, safe_target, digest
from State.session_checkpoint import ActiveRun, ACTIVE_RUN, SessionStore, decode, encode, single_writer
from State.terminal_settings import TerminalSettings, default_state_dir
from terminal_cli import Terminal, parser
from terminal_render import Renderer, RENDERER
from test_agent_loop import QueueChat, make_states, model_response, json_message, openai_tool, TEST_TEMP_ROOT


CONFIG = RuntimeConfig("chatgpt", "chatgpt", None, None, "fake", "fake", 0.0, None, None)


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def workspace(self, directory, approve=None):
        root = Path(directory)
        project = root / "project with spaces"
        project.mkdir()
        atomic_bytes(project / "a.py", b"print('dirty working tree')\n")
        atomic_bytes(project / "b.py", b"old b\n")
        atomic_bytes(project / ".env", b"FAKE_TEST_SECRET=not-a-real-key\n")
        atomic_bytes(project / ".env.example", b"NAME=${NAME}\n")
        atomic_bytes(project / ".gitignore", b"target/\n")
        (project / "target").mkdir()
        atomic_bytes(project / "target/out.jar", b"excluded-build")
        return ProjectWorkspace(root / "state/session", "main", "main", "r_test", root / "state",
                                project, approve=approve or (lambda _: True))

    async def staged(self, directory):
        workspace = self.workspace(directory)
        with patch.object(SandboxRunner, "availability", return_value={"available": True}):
            await workspace.ensure(True)
        return workspace

    async def test_permission_is_host_owned_frozen_and_denial_reads_nothing(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = self.workspace(directory, approve=lambda _: False)
            gateway = ExecutionGateway(None, project_workflow=workspace)
            with patch.object(workspace, "scan") as scan, patch.object(gateway, "_local") as read:
                with self.assertRaises(RequestDenied):
                    await gateway.call("read_all_files_tool", {"address": workspace.project_root})
                scan.assert_not_called()
                read.assert_not_called()
            self.assertTrue(workspace.denied)
            self.assertIsNone(workspace.staging_root)
            self.assertFalse(workspace.permissions[-1].is_permitted)
            with self.assertRaises(FrozenInstanceError):
                workspace.permissions[-1].is_permitted = True
            self.assertFalse((await gateway.call("write_in_tool", {"permission": {"is_permitted": True}})).ok)

    async def test_read_permission_only_and_second_denial_no_copy_or_process(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            requests = []
            def approve(request):
                requests.append(request)
                return request["action"] == "read_project"
            workspace = self.workspace(directory, approve)
            gateway = ExecutionGateway(None, project_workflow=workspace)
            await workspace.ensure()
            with self.assertRaises(PermissionDenied):
                workspace.policy().check_path(Path(workspace.project_root) / "a.py", write=True)
            with patch.object(workspace, "create_staging") as copy, patch.object(SandboxRunner, "run") as run:
                with self.assertRaises(RequestDenied):
                    await gateway.call("run_code_get_feedback_tool", {"code_path": str(Path(workspace.project_root) / "a.py")})
                copy.assert_not_called()
                run.assert_not_called()
            self.assertEqual([r["action"] for r in requests], ["read_project", "create_staging"])

    async def test_docker_missing_never_copies_or_falls_back(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = self.workspace(directory)
            with patch.object(SandboxRunner, "availability", return_value={"available": False}), patch.object(workspace, "create_staging") as copy:
                with self.assertRaisesRegex(PermissionDenied, "no host fallback"):
                    await workspace.ensure(True)
                copy.assert_not_called()
            self.assertIsNone(workspace.staging_root)

    async def test_exclusions_are_case_insensitive_and_support_relative_globs(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = self.workspace(directory)
            project = Path(workspace.project_root)
            atomic_bytes(project / ".ENV", b"FAKE_TEST_SECRET=not-a-key\n")
            atomic_bytes(project / "private/config.json", b"{}\n")
            workspace.excludes = (*workspace.excludes, "private/*.json")
            with patch.object(SandboxRunner, "availability", return_value={"available": True}):
                await workspace.ensure(True)
            stage = Path(workspace.staging_root)
            self.assertFalse((stage / ".ENV").exists())
            self.assertFalse((stage / "private/config.json").exists())
            self.assertTrue((stage / ".env.example").exists())
            self.assertIn(".ENV", workspace.manifest["excluded"])
            self.assertIn("private/config.json", workspace.manifest["excluded"])

    async def test_network_setting_is_not_permission_and_new_request_reconfirms(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            requests = []
            def approve(request):
                requests.append(request["action"])
                return request["action"] != "enable_network"
            workspace = self.workspace(directory, approve)
            workspace.network = True
            with patch.object(SandboxRunner, "availability", return_value={"available": True}):
                with self.assertRaises(RequestDenied):
                    await workspace.ensure(True)
            self.assertEqual(requests, ["read_project", "create_staging", "enable_network"])
            self.assertFalse(workspace.policy().network)
            self.assertTrue(workspace.denied)

    async def test_copy_dirty_source_exclusions_and_original_unchanged(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            stage = Path(workspace.staging_root)
            self.assertEqual((stage / "a.py").read_bytes(), b"print('dirty working tree')\n")
            self.assertTrue((stage / ".gitignore").exists())
            self.assertTrue((stage / ".env.example").exists())
            self.assertFalse((stage / ".env").exists())
            self.assertFalse((stage / "target").exists())
            atomic_bytes(stage / "a.py", b"new code\n")
            self.assertEqual((Path(workspace.project_root) / "a.py").read_bytes(), b"print('dirty working tree')\n")
            package = workspace.make_package()
            self.assertIn("dirty working tree", package["body"]["changes"][0]["diff"])
            self.assertEqual([c["path"] for c in package["body"]["changes"]], ["a.py"])
            self.assertEqual(workspace.policy().workspace, str(stage))
            self.assertNotEqual(workspace.policy().workspace, workspace.project_root)
            with self.assertRaises(PermissionDenied):
                workspace.policy().check_path(Path(workspace.project_root) / "a.py", write=True)

    async def test_copy_detects_source_changes_and_links_not_followed(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = self.workspace(directory)
            await workspace.ensure()
            original_scan = workspace.scan
            count = 0
            def scan(root):
                nonlocal count
                count += 1
                if count == 2:
                    atomic_bytes(Path(workspace.project_root) / "a.py", b"external update")
                return original_scan(root)
            with patch.object(workspace, "scan", side_effect=scan):
                with self.assertRaisesRegex(RuntimeError, "发生变化"):
                    workspace.create_staging(workspace.stage_parent / "copy/workspace")
            self.assertIsNone(workspace.staging_root)
            with self.assertRaises(PermissionDenied):
                safe_target(workspace.project_root, "../outside")
            with self.assertRaises(PermissionDenied):
                safe_target(workspace.project_root, "A.py")

    async def test_immutable_bytes_no_approval_and_conflict_prechecks(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            atomic_bytes(Path(workspace.staging_root) / "a.py", b"approved version\n")
            package = workspace.make_package()
            atomic_bytes(Path(workspace.staging_root) / "a.py", b"later unapproved version\n")
            with self.assertRaises(PermissionDenied):
                workspace.apply(package)
            result = workspace.apply(package, approved=True)
            self.assertEqual(result["status"], "applied")
            self.assertEqual((Path(workspace.project_root) / "a.py").read_bytes(), b"approved version\n")
            self.assertEqual((Path(workspace.project_root) / "b.py").read_bytes(), b"old b\n")
            with self.assertRaisesRegex(RuntimeError, "不能重放"):
                workspace.apply(package, approved=True)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            atomic_bytes(Path(workspace.staging_root) / "a.py", b"candidate\n")
            atomic_bytes(Path(workspace.staging_root) / "new.py", b"new\n")
            package = workspace.make_package()
            atomic_bytes(Path(workspace.project_root) / "new.py", b"user new file\n")
            with self.assertRaisesRegex(RuntimeError, "新增路径冲突"):
                workspace.apply(package, approved=True)
            self.assertIn(b"dirty", (Path(workspace.project_root) / "a.py").read_bytes())
            (Path(workspace.project_root) / "new.py").unlink()
            atomic_bytes(Path(workspace.project_root) / "a.py", b"external edit\n")
            with self.assertRaisesRegex(RuntimeError, "原文件已变化"):
                workspace.apply(package, approved=True)
            self.assertEqual((Path(workspace.project_root) / "a.py").read_bytes(), b"external edit\n")

    async def test_add_delete_apply_and_binary_tamper_rejected(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            (Path(workspace.staging_root) / "b.py").unlink()
            atomic_bytes(Path(workspace.staging_root) / "new.py", b"new source\n")
            package = workspace.make_package()
            actions = {c["path"]: c["action"] for c in package["body"]["changes"]}
            self.assertEqual(actions, {"b.py": "delete", "new.py": "add"})
            workspace.apply(package, approved=True)
            self.assertFalse((Path(workspace.project_root) / "b.py").exists())
            self.assertTrue((Path(workspace.project_root) / "new.py").exists())
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            atomic_bytes(Path(workspace.staging_root) / "binary", b"\x00\xff\x03")
            package = workspace.make_package()
            self.assertTrue(package["body"]["skipped"])
            with self.assertRaises(PermissionDenied):
                workspace.apply(package, approved=True)
            package["body"]["changes"].append({"path": "forged"})
            with self.assertRaises(PermissionDenied):
                workspace.apply(package, approved=True)

    async def test_mid_apply_failure_restores_written_files_and_journal(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            for name in ("a.py", "b.py"):
                atomic_bytes(Path(workspace.staging_root) / name, b"changed\n")
            package = workspace.make_package()
            def fail_second(path, data, mode=None):
                if path == Path(workspace.project_root) / "b.py":
                    raise OSError("simulated write failure")
                return atomic_bytes(path, data, mode)
            with patch("State.project_workspace.atomic_bytes", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "应用失败"):
                    workspace.apply(package, approved=True)
            self.assertIn(b"dirty", (Path(workspace.project_root) / "a.py").read_bytes())
            self.assertEqual((Path(workspace.project_root) / "b.py").read_bytes(), b"old b\n")
            journal = workspace.transaction_status()[0]
            self.assertEqual(journal["status"], "failed")
            self.assertEqual(journal["files"][0]["status"], "restored")
            self.assertEqual(journal["files"][1]["status"], "unchanged")
            self.assertNotIn("before_bytes", json.dumps(journal))

    async def test_failed_apply_does_not_overwrite_concurrent_new_user_edit(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            for name in ("a.py", "b.py"):
                atomic_bytes(Path(workspace.staging_root) / name, b"candidate\n")
            package = workspace.make_package()
            def external_change(path, data, mode=None):
                if path.name == "b.py":
                    atomic_bytes(Path(workspace.project_root) / "a.py", b"concurrent user edit\n")
                    raise OSError("second file failed")
                return atomic_bytes(path, data, mode)
            with patch("State.project_workspace.atomic_bytes", side_effect=external_change):
                with self.assertRaises(RuntimeError):
                    workspace.apply(package, approved=True)
            self.assertEqual((Path(workspace.project_root) / "a.py").read_bytes(), b"concurrent user edit\n")
            self.assertEqual(workspace.transaction_status()[0]["files"][0]["status"], "manual_recovery_required")

    async def test_real_link_or_windows_junction_excluded_and_not_deleted(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = self.workspace(directory)
            outside = Path(directory) / "outside"
            outside.mkdir()
            atomic_bytes(outside / "marker.txt", b"outside-content")
            link = Path(workspace.project_root) / "linked"
            if os.name == "nt":
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
                if result.returncode:
                    self.skipTest("Windows junction creation unavailable")
            else:
                link.symlink_to(outside, target_is_directory=True)
            try:
                await workspace.ensure()
                with patch.object(SandboxRunner, "availability", return_value={"available": True}):
                    await workspace.ensure(True)
                self.assertFalse((Path(workspace.staging_root) / "linked").exists())
                self.assertTrue(any("link/reparse" in p for p in workspace.manifest["excluded"]))
                with self.assertRaises(PermissionDenied):
                    workspace.policy().check_path(link / "marker.txt")
                self.assertEqual((outside / "marker.txt").read_bytes(), b"outside-content")
            finally:
                if os.name == "nt":
                    os.rmdir(link)  # Remove only the junction, never its target.
                else:
                    link.unlink()

    async def test_permission_request_mutation_is_not_a_valid_approval(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            def forge(request):
                request["project_root"] = "forged root"
                return True
            workspace = self.workspace(directory, approve=forge)
            with self.assertRaises(RequestDenied):
                await workspace.ensure()
            self.assertFalse(workspace.permissions[-1].is_permitted)

    async def test_saved_request_identity_cannot_escape_metadata_root(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            with self.assertRaises(PermissionDenied):
                ProjectWorkspace(Path(directory) / "session", "main", "main", "../../outside", Path(directory) / "state")

    async def test_cooperating_project_apply_lock_blocks_second_writer(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            atomic_bytes(Path(workspace.staging_root) / "a.py", b"candidate\n")
            package = workspace.make_package()
            root = workspace.project_root.casefold() if os.name == "nt" else workspace.project_root
            lock = Path(workspace.state_dir) / ".project_apply_locks" / digest(root.encode("utf-8"))
            with single_writer(lock):
                with self.assertRaises(RuntimeError):
                    workspace.apply(package, approved=True)
            self.assertIn(b"dirty", (Path(workspace.project_root) / "a.py").read_bytes())

    async def test_restore_revokes_permissions_missing_version_and_branch_block(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            agent, _, _, _ = make_states(Path(directory) / "state/session")
            agent.project_root, agent.staging_root = workspace.project_root, workspace.staging_root
            agent.workspace_revision, agent.workspace_hash = workspace.revision, workspace.workspace_hash
            restored = ProjectWorkspace(workspace.session, "main", "main", "r_test", workspace.state_dir)
            restored.restore(decode(encode(agent)))
            self.assertFalse(restored.read_allowed)
            self.assertFalse(restored.staging_allowed)
            self.assertEqual(restored.permissions, [])
            self.assertEqual(restored.staging_root, workspace.staging_root)
            branch = ProjectWorkspace(workspace.session, "main", "other_branch", "r_test", workspace.state_dir)
            with self.assertRaisesRegex(RuntimeError, "没有可重建"):
                branch.restore(agent)
            atomic_bytes(Path(workspace.staging_root) / "a.py", b"external stage edit\n")
            with self.assertRaisesRegex(RuntimeError, "版本与检查点不一致"):
                restored.restore(agent)

    async def test_cache_scope_changes_for_body_listing_workspace_and_branch(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            token = WORKSPACE.set(workspace)
            try:
                args = {"address": str(Path(workspace.staging_root) / "a.py")}
                first = _tool_call_signature("read_files_content_tool", args)
                atomic_bytes(Path(workspace.staging_root) / "a.py", b"different source\n")
                self.assertNotEqual(first, _tool_call_signature("read_files_content_tool", args))
                listed = _tool_call_signature("read_all_files_tool", {"address": workspace.staging_root})
                atomic_bytes(Path(workspace.staging_root) / "new.py", b"new\n")
                self.assertNotEqual(listed, _tool_call_signature("read_all_files_tool", {"address": workspace.staging_root}))
                stable = _tool_call_signature("write_in_tool", {"file_address": args["address"], "code": "x"})
                workspace.revision += 1
                self.assertEqual(stable, _tool_call_signature("write_in_tool", {"file_address": args["address"], "code": "x"}))
                workspace.branch = "new_branch"
                self.assertNotEqual(stable, _tool_call_signature("write_in_tool", {"file_address": args["address"], "code": "x"}))
            finally:
                WORKSPACE.reset(token)

    async def test_metadata_failure_after_execution_is_unknown_not_retryable(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            gateway = ExecutionGateway(None, project_workflow=workspace)
            actual = {"status": "success", "stdout": "actual execution result"}
            with (patch.object(gateway, "_staged_write", return_value=actual) as execute,
                  patch.object(workspace, "changed", side_effect=OSError("metadata unavailable"))):
                result = await gateway.call("write_in_tool", {
                    "file_address": str(Path(workspace.project_root) / "a.py"), "content": "new"})
            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, "unknown")
            self.assertTrue(result.output["executed"])
            self.assertEqual(result.output["actual_result"], actual)
            execute.assert_called_once()

    async def test_workspace_evidence_does_not_copy_full_raw_result(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            output = {"status": "success", "returncode": 0, "stdout": "x" * 25000,
                      "content": {"raw": "full fact"}, "command": ["python", "test.py"]}
            workspace.changed("run_code_get_feedback_tool", {}, output)
            facts = workspace.tests[-1]["result"]
            self.assertNotIn("content", facts)
            self.assertNotIn("stdout", facts)
            self.assertEqual(len(facts["stdout_excerpt"]), 2000)
            self.assertEqual(facts["command"], output["command"])

    async def test_container_write_mapping_and_build_wrapper_evidence(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace = await self.staged(directory)
            gateway = ExecutionGateway(None, approve=lambda _: True, project_workflow=workspace)
            def run(command, **kwargs):
                if "-c" in command:
                    self.assertEqual(json.loads(command[-1])["path"], "a.py")
                    self.assertEqual(gateway.policy.workspace, workspace.staging_root)
                    return subprocess.CompletedProcess(command, 0, '{"status":"success"}', '')
                self.assertEqual(command[:2], ["sh", str(Path(workspace.staging_root) / "mvnw")])
                return subprocess.CompletedProcess(command, 0, "BUILD SUCCESS", "")
            atomic_bytes(Path(workspace.staging_root) / "pom.xml", b"<project/>\n")
            atomic_bytes(Path(workspace.staging_root) / "mvnw", b"#!/bin/sh\n")
            with patch.object(SandboxRunner, "run", side_effect=run):
                result = await gateway.call("write_in_tool", {"file_address": str(Path(workspace.project_root) / "a.py"), "code": "new"})
                self.assertTrue(result.ok)
                result = await gateway.call("package_spring_boot_with_confirmation_tool", {"project_path": workspace.project_root,
                    "temp_path": str(Path(workspace.project_root) / "artifacts")})
                self.assertTrue(result.ok)
                self.assertFalse(result.output["artifact_found"])
                self.assertEqual(result.output["artifact_copy_status"], "not_produced")
            self.assertIn(b"dirty", (Path(workspace.project_root) / "a.py").read_bytes())
            self.assertEqual(workspace.tests[-1]["environment"], "docker")


class TerminalWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def terminal(self, directory, output=None, extra=()):
        root = Path(directory)
        args = parser().parse_args(["--state-dir", str(root / "state"), "--settings-file", str(root / "user/settings.json"), *extra])
        return Terminal(args, output=output or (lambda _: None))

    async def test_no_argument_project_empty_docker_lazy_and_ordinary_model_question(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            terminal = self.terminal(directory)
            self.assertIsNone(parser().parse_args([]).workspace)
            self.assertIsNone(terminal.project_root)
            self.assertIsNone(terminal.policy)
            terminal.command("/settings")
            terminal.command("/project")
            terminal.command("/permissions")
            chat = QueueChat([
                model_response(json_message(task_list=["问答"], description="规划")),
                model_response(json_message(description="指导", executor_guidance="回答", completion_criteria="回答问题")),
                model_response(json_message(description="普通回答", is_finished=True)),
                model_response(json_message(description="已检查回答", is_passed=True, is_finished=True)),
                model_response(json_message(description="通过", is_next_target=True, is_finished=True, final_answer="普通问答答案")),
                model_response(json_message(description="请求摘要")),
            ])
            with (patch("AgentLoop.agent.load_runtime_config", return_value=CONFIG),
                  patch("AgentLoop.agent._register_mcp_clients", new=AsyncMock()),
                  patch("AgentLoop.agent._build_model_client", return_value=object()),
                  patch("AgentLoop.agent._close_model_client", new=AsyncMock()),
                  patch("AgentLoop.agent._chat_map", return_value={"chatgpt": chat}),
                  patch.object(SandboxRunner, "availability", side_effect=AssertionError("Docker should be lazy"))):
                result = await terminal.ask("解释一下列表和元组")
                terminal.command("/history")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["answer"], "普通问答答案")
            self.assertIsNone(terminal.policy)
            self.assertEqual(terminal.workspace.permissions, [])

    async def test_completed_request_automatically_reviews_fixed_package_yes_or_no(self):
        for approved in (True, False):
            with self.subTest(approved=approved), tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
                project = Path(directory) / "project"
                project.mkdir()
                original = project / "main.py"
                atomic_bytes(original, b"old\n")
                output, prompts = [], []
                terminal = self.terminal(directory, output.append, extra=("--workspace", str(project)))
                def approve(request):
                    prompts.append(request)
                    return approved if request["action"] == "apply_changes" else True
                terminal.approval = approve
                async def completed(*args, **kwargs):
                    workspace = kwargs["project_workflow"]
                    await workspace.ensure(True)
                    atomic_bytes(Path(workspace.staging_root) / "main.py", b"new fixed bytes\n")
                    workspace.changed("write_in_tool", {}, {"status": "success"})
                    return {"status": "completed", "answer": "副本修改完成"}
                with (patch("AgentLoop.agent.load_runtime_config", return_value=CONFIG),
                      patch("AgentLoop.agent.main", new=completed),
                      patch.object(SandboxRunner, "availability", return_value={"available": True})):
                    result = await terminal.ask("修改项目")
                self.assertEqual([p["action"] for p in prompts], ["read_project", "create_staging", "apply_changes"])
                self.assertTrue(prompts[-1]["change_set_id"].startswith("cs_"))
                self.assertEqual(original.read_bytes(), b"new fixed bytes\n" if approved else b"old\n")
                self.assertEqual(result["project_status"], "applied" if approved else "not_applied")
                self.assertEqual(terminal.status, "completed" if approved else "denied")
                history = terminal.store.history("timeline.jsonl", terminal.run.branch)
                self.assertTrue(any(row["kind"] == ("application" if approved else "application_declined") for row in history))
                self.assertIn("没有执行构建", "\n".join(output))

    async def test_settings_persist_and_active_storage_stays_put(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            terminal = self.terminal(directory)
            terminal.new()
            old = terminal.store.path
            target = Path(directory) / "new state with spaces"
            terminal.command(f'/settings state-dir "{target}"')
            self.assertEqual(terminal.store.path, old)
            self.assertEqual(terminal.state_dir, Path(directory) / "state")
            args = parser().parse_args(["--settings-file", str(terminal.settings.path)])
            restart = Terminal(args, output=lambda _: None)
            self.assertEqual(restart.state_dir, target)
            terminal.new()
            self.assertTrue(terminal.store.path.is_relative_to(target))
            previous = terminal.settings.path.read_bytes()
            with patch("State.terminal_settings._atomic_write", side_effect=OSError("write denied")):
                with self.assertRaises(OSError):
                    terminal.command(f'/settings state-dir "{Path(directory) / "failed setting"}"')
            self.assertEqual(terminal.settings.path.read_bytes(), previous)

    async def test_read_denial_stops_model_loop_and_allows_new_request(self):
        class GatewayRegistry:
            def __init__(self, host, execution_gateway, **kwargs):
                self.gateway = execution_gateway
            def schemas_for(self, *args):
                return []
            async def call(self, role, tool_name, arguments):
                return await self.gateway.call(tool_name, arguments)
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            project = Path(directory) / "project"
            project.mkdir()
            atomic_bytes(project / "main.py", b"untouched\n")
            terminal = self.terminal(directory, extra=("--workspace", str(project)))
            terminal.approval = lambda _: False
            chat = QueueChat([
                model_response(json_message(task_list=["读项目"], description="规划")),
                model_response(json_message(description="指导", executor_guidance="读取目录", completion_criteria="目录证据")),
                model_response(tool=[openai_tool("read_all_files_tool", {"address": str(project)})]),
            ])
            with (patch("AgentLoop.agent.load_runtime_config", return_value=CONFIG),
                  patch("AgentLoop.agent._register_mcp_clients", new=AsyncMock()),
                  patch("AgentLoop.agent._build_model_client", return_value=object()),
                  patch("AgentLoop.agent._close_model_client", new=AsyncMock()),
                  patch("AgentLoop.agent._chat_map", return_value={"chatgpt": chat}),
                  patch("AgentLoop.agent.ToolRegistry", GatewayRegistry)):
                result = await terminal.ask("分析选定项目")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(terminal.status, "denied")
            self.assertEqual(len(chat.calls), 3)
            self.assertEqual(terminal.store.load()["status"], "denied")
            self.assertFalse(terminal.store.load()["unknown_operations"])
            self.assertEqual((project / "main.py").read_bytes(), b"untouched\n")
            with self.assertRaises(ValueError):
                await terminal.ask(continuing=True)
            async def ordinary(*args, **kwargs):
                return {"status": "completed", "answer": "重新说明需求已接受"}
            with patch("AgentLoop.agent.load_runtime_config", return_value=CONFIG), patch("AgentLoop.agent.main", new=ordinary):
                self.assertEqual((await terminal.ask("改为普通问答"))["status"], "completed")

    async def test_restored_supervisor_rejection_really_returns_to_executor(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            store = SessionStore(Path(directory) / "session", "001")
            store.initialize()
            agent, supervisor, executor, evaluation = make_states(store.path)
            agent.request_id, agent.task_attempt = "r_restored", 1
            executor.output_content = "待修正结果"
            run = ActiveRun(store)
            run.bundle = {"agent": agent, "supervisor": supervisor, "executor": executor, "evaluation": evaluation}
            run.phase, run.payload, run.resume = "decision", {"rounds": 0}, True
            chat = QueueChat([
                model_response(json_message(description="否决并要求修正", is_next_target=False, is_finished=True, next_executor_target="修正回答")),
                model_response(json_message(description="已按要求修正", is_finished=True)),
                model_response(json_message(description="修正后验收通过", is_passed=True, is_finished=True)),
                model_response(json_message(description="任务完成", is_next_target=True, is_finished=True, final_answer="修正后的答案")),
                model_response(json_message(description="请求总结")),
            ])
            with (patch("AgentLoop.agent._register_mcp_clients", new=AsyncMock()),
                  patch("AgentLoop.agent._build_model_client", return_value=object()),
                  patch("AgentLoop.agent._close_model_client", new=AsyncMock()),
                  patch("AgentLoop.agent._chat_map", return_value={"chatgpt": chat})):
                result = await main("修正测试", str(store.path), "001", session_run=run, runtime_config=CONFIG)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["answer"], "修正后的答案")
            self.assertEqual(len(chat.calls), 5)
            self.assertGreater(agent.executor_executing_times, 0)

    async def test_default_windows_path_storage_unavailable_no_silent_fallback(self):
        with patch("State.terminal_settings.os.name", "nt"):
            self.assertEqual(str(default_state_dir()), "D:\\coding-agent-state")
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            with patch("terminal_cli.validate_state_dir", side_effect=OSError("missing drive")), patch("terminal_cli.sys.stdin.isatty", return_value=False):
                with self.assertRaisesRegex(ValueError, "未静默切换"):
                    self.terminal(directory)

    async def test_yes_no_text_eof_noninteractive_and_control_safe(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            output = []
            terminal = self.terminal(directory, output.append)
            terminal.input = lambda _: "yes"
            with patch("terminal_cli.sys.stdin.isatty", return_value=True):
                self.assertTrue(terminal.approval({"action": "read_project", "project_root": "D:\\a project"}))
                terminal.input = lambda _: (_ for _ in ()).throw(EOFError())
                self.assertFalse(terminal.approval({"action": "read_project", "project_root": "D:\\a project"}))
            self.assertIn("yes：", "\n".join(output))
            self.assertIn("no ：", "\n".join(output))
            with patch("terminal_cli.sys.stdin.isatty", return_value=False):
                terminal.input = Mock(side_effect=AssertionError("must not read stdin"))
                self.assertFalse(terminal.approval({"action": "anything"}))

    async def test_steps_include_plain_evaluation_and_resume_no_grants(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            output = []
            terminal = self.terminal(directory, output.append)
            terminal.new()
            agent, supervisor, executor, evaluation = make_states(terminal.store.path)
            for state in (agent, supervisor, executor, evaluation):
                state.chat_id = "main"
            agent.project_root = str(Path(directory) / "candidate only")
            terminal.run.bundle = {"agent": agent, "supervisor": supervisor, "executor": executor, "evaluation": evaluation}
            selected = terminal.run.boundary("evaluation", "supervisor")
            terminal.command("/steps --phase evaluation")
            self.assertIn(str(selected["event_seq"]), "\n".join(output))
            self.assertIn('"phase": "evaluation"', "\n".join(output))
            restart = self.terminal(directory)
            with patch("State.project_workspace.ProjectWorkspace.ensure", side_effect=AssertionError("no tools during resume")):
                restart.resume(terminal.store.path.name)
            self.assertEqual(restart.project_root, agent.project_root)
            self.assertIsNone(restart.policy)
            self.assertIsNone(restart.workspace)


class RendererTests(unittest.TestCase):
    def test_dim_warning_no_color_redirect_and_sanitized_content(self):
        output = []
        with patch.dict(os.environ, {}, clear=True):
            renderer = Renderer(output.append, "always")
            renderer.say("第 1 轮\x1b[31m injected", "dim")
            renderer.say("yes/no", "warning")
            renderer.say("最终答案")
            self.assertTrue(output[0].startswith("\x1b[2;90m"))
            self.assertIn("\\x1b[31m", output[0])
            self.assertTrue(output[1].startswith("\x1b[1;33m"))
            self.assertNotIn("\x1b", output[2])
            plain = []
            Renderer(plain.append, "auto", stream=Mock(isatty=lambda: False)).say("history", "dim")
            self.assertEqual(plain, ["history"])
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            output = []
            Renderer(output.append, "always").say("history", "dim")
            self.assertEqual(output, ["history"])

    def test_all_loop_prints_use_trusted_renderer(self):
        import ast
        root = Path(__file__).resolve().parents[1] / "AgentLoop"
        for name in ("agent.py", "executor_loop.py", "supervisor_loop.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            self.assertFalse([node for node in ast.walk(tree) if isinstance(node, ast.Call)
                              and isinstance(node.func, ast.Name) and node.func.id == "print"], name)


@unittest.skipUnless(os.environ.get("CODING_AGENT_STAGING_DOCKER_INTEGRATION") == "1", "staging Docker isolation requires explicit opt-in")
class StagingDockerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_container_only_changes_staging_not_original(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            root = Path(directory)
            project = root / "source"
            project.mkdir()
            atomic_bytes(project / "main.txt", b"real-original\n")
            workspace = ProjectWorkspace(root / "state/session", "main", "main", "r_docker", root / "state", project,
                approve=lambda _: True, image=os.environ.get("CODING_AGENT_STAGING_DOCKER_IMAGE", "nginx:alpine"))
            policy = await workspace.ensure(True)
            runner = SandboxRunner(policy)
            result = await asyncio.to_thread(runner.run, ["sh", "-c", "printf 'container-staging\\n' > /workspace/main.txt"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((project / "main.txt").read_bytes(), b"real-original\n")
            self.assertEqual((Path(workspace.staging_root) / "main.txt").read_bytes(), b"container-staging\n")
            self.assertEqual(policy.workspace, workspace.staging_root)
            self.assertFalse(runner.containers)


if __name__ == "__main__":
    unittest.main()

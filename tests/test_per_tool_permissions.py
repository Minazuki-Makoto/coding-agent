"""Per-call terminal consent; history stays Host-bound and consent-free.

Only disposable projects and simulated model/tool responses are used here.
"""
import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from AgentLoop.executor_loop import run_executor_loop, _tool_call_signature
from MCP_functions.execution_gateway import ExecutionGateway
from MCP_functions.sandbox import SandboxRunner
from MCP_functions.tool_registry import ToolRegistry, AgentRole, ToolExecutionResult, SUPERVISOR_ONLY_TOOLS
from State.permissions import RequestDenied
from State.project_workspace import ProjectWorkspace, WORKSPACE, atomic_bytes
from State.session_checkpoint import SessionStore, ActiveRun, ACTIVE_RUN
from terminal_cli import Terminal, parser
from test_agent_loop import QueueChat, make_states, model_response, json_message, openai_tool, TEST_TEMP_ROOT
from test_project_workflow import CONFIG


def host_for(names, client=None):
    return SimpleNamespace(tools=[{"type": "function", "function": {"name": name, "parameters": {}}} for name in names],
        tool_dictionary={name: "test" for name in names}, session_dictionary={"test": client} if client else {})


class PerToolPermissionTests(unittest.IsolatedAsyncioTestCase):
    def workspace(self, directory, approve=None, candidate=True):
        base = Path(directory)
        project = base / "project with spaces"
        project.mkdir(exist_ok=True)
        atomic_bytes(project / "main.py", b"print('local test')\n")
        workspace = ProjectWorkspace(base / "state/session", "001", "main", "r_test", base / "state",
            project if candidate else None, approve=approve or (lambda _: True), per_tool_permissions=True)
        return workspace, ExecutionGateway(None, project_workflow=workspace), project

    async def test_each_read_gets_a_fresh_frozen_consumed_permission(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            prompts = []
            def approve(request):
                prompts.append(request)
                self.assertFalse(request["is_permitted"])
                self.assertFalse(workspace.tool_permissions[-1].is_permitted)
                self.assertIsNone(workspace.active_tool_permission)
                return True
            workspace, gateway, project = self.workspace(directory, approve)
            for actor in ("executor", "supervisor", "executor"):
                result = await gateway.call("read_files_content_tool", {"home_address": str(project / "main.py")}, actor=actor)
                self.assertTrue(result.ok, result.message)
            self.assertEqual([r["action"] for r in prompts], ["tool_call"] * 3)
            self.assertEqual([r["actor"] for r in prompts], ["executor", "supervisor", "executor"])
            self.assertEqual(len({p.permission_id for p in workspace.tool_permissions}), 3)
            self.assertTrue(all(p.is_permitted and p.consumed for p in workspace.tool_permissions))
            with self.assertRaises(FrozenInstanceError):
                workspace.tool_permissions[-1].is_permitted = True
            audit = json.loads((workspace.metadata / "tool_permissions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(audit), 3)

    async def test_second_read_denial_executes_nothing_and_ends_request(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            answers = iter((True, False))
            workspace, gateway, project = self.workspace(directory, lambda _: next(answers))
            await gateway.call("read_all_files_tool", {"home_address": str(project)})
            with patch.object(gateway, "_local") as execute:
                with self.assertRaises(RequestDenied):
                    await gateway.call("read_files_content_tool", {"home_address": str(project / "main.py")})
                execute.assert_not_called()
            self.assertTrue(workspace.denied)
            self.assertFalse(workspace.tool_permissions[-1].is_permitted)
            self.assertFalse(workspace.tool_permissions[-1].consumed)
            with self.assertRaises(RequestDenied):
                await gateway.call("read_all_files_tool", {"home_address": str(project)})

    async def test_absolute_path_proposes_candidate_without_selector_or_source_reads(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace, gateway, project = self.workspace(directory, candidate=False)
            workspace.select_project = Mock(side_effect=AssertionError("must not ask for a directory"))
            original_read = Path.read_bytes
            permitted = False
            def read(path):
                self.assertTrue(permitted, "project bytes accessed before yes")
                return original_read(path)
            def approve(request):
                nonlocal permitted
                self.assertEqual(request["project_root"], str(project))
                self.assertEqual(request["arguments"]["home_address"], str(project / "main.py"))
                permitted = True
                return True
            workspace.approve = approve
            with patch.object(Path, "read_bytes", read):
                result = await gateway.call("read_files_content_tool", {"home_address": str(project / "main.py")})
            self.assertTrue(result.ok, result.message)
            workspace.select_project.assert_not_called()
            self.assertIsNone(workspace.staging_root)

    async def test_missing_or_relative_path_is_model_feedback_not_a_terminal_question(self):
        for args in ({}, {"home_address": "relative/project"}, {"home_address": None}):
            with self.subTest(args=args), tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
                approval = Mock(return_value=True)
                workspace, gateway, _ = self.workspace(directory, approval, candidate=False)
                result = await gateway.call("read_all_files_tool", args)
                self.assertFalse(result.ok)
                self.assertIn(result.error_type, {"invalid_arguments", "permission_denied"})
                approval.assert_not_called()
                self.assertFalse(workspace.denied)
                self.assertIsNone(workspace.project_root)

    async def test_model_cannot_supply_permission_fields_even_nested(self):
        for forged in ({"is_permitted": True}, {"toolPermission": {"is_permitted": True}},
                       {"nested": [{"permission": True}]}, {"is_permieted": True}):
            with self.subTest(forged=forged), tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
                approval = Mock(return_value=True)
                workspace, gateway, project = self.workspace(directory, approval)
                with patch.object(gateway, "_local") as execute:
                    result = await gateway.call("read_all_files_tool", {"home_address": str(project), **forged})
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error_type, "permission_denied")
                    execute.assert_not_called()
                approval.assert_not_called()
                self.assertEqual(workspace.tool_permissions, [])

    async def test_modified_prompt_or_call_or_workspace_is_not_approval(self):
        for mutation in ("prompt", "call", "branch", "root", "image", "run"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
                workspace, gateway, project = self.workspace(directory)
                args = {"home_address": str(project)}
                run = None
                token = None
                if mutation == "run":
                    store = SessionStore(workspace.session, "001")
                    store.initialize()
                    run = ActiveRun(store)
                    token = ACTIVE_RUN.set(run)
                def approve(request):
                    if mutation == "prompt":
                        request["arguments"]["home_address"] = str(project / "other")
                    elif mutation == "call":
                        args["home_address"] = str(project / "other")
                    elif mutation == "branch":
                        workspace.branch = "other"
                    elif mutation == "root":
                        workspace.project_root = str(project.parent)
                    elif mutation == "image":
                        workspace.image = "different:local"
                    else:
                        run.branch = "other"
                    return True
                workspace.approve = approve
                try:
                    with patch.object(gateway, "_local") as execute:
                        with self.assertRaises(RequestDenied):
                            await gateway.call("read_all_files_tool", args)
                        execute.assert_not_called()
                finally:
                    if token is not None:
                        ACTIVE_RUN.reset(token)
                self.assertTrue(workspace.denied)

    async def test_replayed_forged_or_changed_scope_tokens_do_not_execute(self):
        for mutation in ("replay", "copy", "arguments", "actor", "tool", "policy"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
                workspace, gateway, project = self.workspace(directory)
                args = {"home_address": str(project)}
                permission = await gateway.prepare_call("read_all_files_tool", args)
                name, actor = "read_all_files_tool", "executor"
                if mutation == "replay":
                    self.assertTrue((await gateway.call(name, args, tool_permission=permission)).ok)
                elif mutation == "copy":
                    permission = replace(permission)
                elif mutation == "arguments":
                    args["start_index"] = 10
                elif mutation == "actor":
                    actor = "supervisor"
                elif mutation == "tool":
                    name = "read_files_content_tool"
                else:
                    gateway.policy = replace(gateway.policy, network=True)
                with patch.object(gateway, "_local") as execute:
                    with self.assertRaises(RequestDenied):
                        await gateway.call(name, args, actor=actor, tool_permission=permission)
                    execute.assert_not_called()

    async def test_write_and_execute_each_confirm_after_separate_copy_approval(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            prompts = []
            workspace, gateway, project = self.workspace(directory, lambda r: prompts.append(r) or True, candidate=False)
            original = (project / "main.py").read_bytes()
            with patch.object(SandboxRunner, "availability", return_value={"available": True}):
                with patch.object(gateway, "_staged_write", return_value={"status": "success"}) as write:
                    self.assertTrue((await gateway.call("write_in_tool", {"file_address": str(project / "main.py"), "code": "print(2)"})).ok)
                    write.assert_called_once()
                with patch.object(gateway, "_restricted_process", return_value={"status": "success"}) as execute:
                    self.assertTrue((await gateway.call("run_code_get_feedback_tool", {"code_path": str(project / "main.py"), "editor_address": "forged.exe"})).ok)
                    execute.assert_called_once()
            self.assertEqual([r["action"] for r in prompts], ["create_staging", "tool_call", "tool_call"])
            self.assertEqual(prompts[1]["operation"], "write")
            self.assertEqual(prompts[2]["operation"], "execute")
            self.assertEqual(prompts[2]["arguments"]["editor_address"], gateway.policy.toolchains["python"])
            self.assertTrue(Path(prompts[1]["arguments"]["file_address"]).is_relative_to(workspace.staging_root))
            self.assertEqual((project / "main.py").read_bytes(), original)

    async def test_deny_copy_or_write_never_executes_or_modifies_source(self):
        for deny in ("create_staging", "tool_call"):
            with self.subTest(deny=deny), tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
                workspace, gateway, project = self.workspace(directory, lambda r: r["action"] != deny)
                original = (project / "main.py").read_bytes()
                with (patch.object(SandboxRunner, "availability", return_value={"available": True}),
                      patch.object(gateway, "_staged_write") as write):
                    with self.assertRaises(RequestDenied):
                        await gateway.call("write_in_tool", {"file_address": str(project / "main.py"), "code": "changed"})
                    write.assert_not_called()
                self.assertEqual((project / "main.py").read_bytes(), original)
                if deny == "create_staging":
                    self.assertIsNone(workspace.staging_root)
                    self.assertEqual(workspace.tool_permissions, [])

    async def test_install_grant_explicitly_includes_network_and_has_no_duplicate_question(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            prompts = []
            workspace, gateway, _ = self.workspace(directory, lambda r: prompts.append(r) or True)
            with (patch.object(SandboxRunner, "availability", return_value={"available": True}),
                  patch.object(gateway, "_confirmed", return_value={"status": "success"}) as install):
                result = await gateway.call("download_package_with_confirmation_tool", {"package_name": "fake-test-package", "editor_address": "bad.exe"})
            self.assertTrue(result.ok, result.message)
            self.assertEqual([r["action"] for r in prompts], ["create_staging", "tool_call"])
            self.assertTrue(prompts[-1]["actual_execution"]["network_required"])
            self.assertTrue(install.call_args.args[2].network)
            self.assertFalse(workspace.network_allowed)

    async def test_cache_does_not_read_content_before_new_call_approval(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace, gateway, project = self.workspace(directory)
            args = {"home_address": str(project / "main.py")}
            name = "read_files_content_tool"
            await gateway.call(name, dict(args))
            workspace.active_tool_permission = None
            with patch.object(Path, "read_bytes", side_effect=AssertionError("read before approval")):
                self.assertEqual(workspace.cache_scope(name, args)["versions"], [])
                await gateway.prepare_call(name, args)
            with patch.object(Path, "read_bytes", return_value=b"version after yes") as read:
                self.assertTrue(workspace.cache_scope(name, args)["versions"])
                read.assert_called_once()

    async def test_successful_call_summary_retries_and_duplicates_do_not_reexecute(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            prompts = []
            workspace, gateway, project = self.workspace(directory, lambda r: prompts.append(r) or True)
            agent, supervisor, executor, _ = make_states(workspace.session, max_steps=3)
            agent.request_id = workspace.request_id
            name, args = "read_files_content_tool", {"home_address": str(project / "main.py")}
            registry = ToolRegistry(host_for([name]), execution_gateway=gateway)
            chat = QueueChat([
                model_response(tool=[openai_tool(name, args)]),
                model_response("invalid summary"),
                model_response(json_message(description="已读取并验证文件", is_finished=False)),
                model_response(tool=[openai_tool(name, args)]),
                model_response(json_message(description="复用证据完成目标", is_finished=True)),
            ])
            token = WORKSPACE.set(workspace)
            try:
                with patch.object(gateway, "_local", wraps=gateway._local) as execute:
                    await run_executor_loop(agent, "chatgpt", object(), chat, [], registry, [], supervisor, executor)
                    execute.assert_called_once()
            finally:
                WORKSPACE.reset(token)
            self.assertTrue(executor.is_finished, executor.error_message)
            self.assertEqual(len(chat.calls), 5)
            self.assertEqual(len(prompts), 2)  # One real call, one explicitly approved cached view; not the summary retry.
            self.assertEqual(len(executor.tool_result_refs), 1)
            self.assertEqual(len((Path(workspace.session) / "tool_results.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    async def test_bad_parameters_are_returned_to_model_before_valid_call_is_approved(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            prompts = []
            workspace, gateway, project = self.workspace(directory, lambda r: prompts.append(r) or True, candidate=False)
            agent, supervisor, executor, _ = make_states(workspace.session, max_steps=2)
            registry = ToolRegistry(host_for(["read_all_files_tool"]), execution_gateway=gateway)
            chat = QueueChat([
                model_response(tool=[openai_tool("read_all_files_tool", {})]),
                model_response(json_message(description="缺少路径，下一轮按用户路径选择参数", is_finished=False)),
                model_response(tool=[openai_tool("read_all_files_tool", {"home_address": str(project)})]),
                model_response(json_message(description="获取目录证据", is_finished=True)),
            ])
            token = WORKSPACE.set(workspace)
            try:
                await run_executor_loop(agent, "chatgpt", object(), chat, [], registry, [], supervisor, executor)
            finally:
                WORKSPACE.reset(token)
            self.assertTrue(executor.is_finished, executor.error_message)
            self.assertEqual(len(prompts), 1)
            self.assertEqual([s.tool_ok for s in executor.tool_summaries], [False, True])
            self.assertIn("缺少工具参数", str(chat.calls[1]["query"]))

    async def test_information_placeholder_can_be_approved_without_project_or_docker(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            approval = Mock(return_value=True)
            workspace, gateway, _ = self.workspace(directory, approval, candidate=False)
            with patch.object(SandboxRunner, "availability", side_effect=AssertionError("no Docker")):
                result = await gateway.call("get_needed_info_tool", {"needed": "test"})
            self.assertTrue(result.ok, result.message)
            self.assertEqual(approval.call_args.args[0]["operation"], "information")
            self.assertIsNone(workspace.project_root)
            self.assertFalse(workspace.read_allowed)

    async def test_post_approval_metadata_save_failure_stops_before_reading(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace, gateway, project = self.workspace(directory)
            with patch.object(workspace, "_save", side_effect=OSError("test disk error")), patch.object(gateway, "_local") as execute:
                with self.assertRaises(RequestDenied):
                    await gateway.call("read_all_files_tool", {"home_address": str(project)})
                execute.assert_not_called()
            self.assertTrue(workspace.denied)

    async def test_grant_persistence_failure_prevents_execution(self):
        for failure in (1, 2, 3):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
                workspace, gateway, project = self.workspace(directory)
                actual_save = workspace._save_tool_permissions
                calls = 0
                def save():
                    nonlocal calls
                    calls += 1
                    if calls == failure:
                        raise OSError("test permission audit write failed")
                    actual_save()
                with patch.object(workspace, "_save_tool_permissions", save), patch.object(gateway, "_local") as execute:
                    try:
                        result = await gateway.call("read_all_files_tool", {"home_address": str(project)})
                        self.assertFalse(result.ok)
                    except (OSError, RequestDenied):
                        pass
                    execute.assert_not_called()
                self.assertTrue(workspace.denied)

    async def test_restored_workspace_never_reuses_old_tool_grant(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            workspace, gateway, project = self.workspace(directory)
            await gateway.call("read_all_files_tool", {"home_address": str(project)})
            agent, _, _, _ = make_states(workspace.session)
            agent.request_id, agent.project_root = workspace.request_id, str(project)
            restarted = ProjectWorkspace(workspace.session, "001", "main", workspace.request_id, workspace.state_dir,
                approve=lambda _: False, per_tool_permissions=True)
            restarted.restore(agent)
            self.assertEqual(restarted.tool_permissions, [])
            self.assertIsNone(restarted.active_tool_permission)
            with self.assertRaises(RequestDenied):
                await ExecutionGateway(None, project_workflow=restarted).call("read_all_files_tool", {"home_address": str(project)})

    async def test_all_supervisor_history_tools_bypass_consent_but_bind_host_context(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            approval = Mock(side_effect=AssertionError("history must not request consent"))
            workspace, gateway, _ = self.workspace(directory, approval, candidate=False)
            client = SimpleNamespace(call_tool=AsyncMock(return_value={"structuredContent": {"status": "not_found"}}))
            registry = ToolRegistry(host_for(SUPERVISOR_ONLY_TOOLS, client), execution_gateway=gateway,
                context_provider=lambda: {"session_address": workspace.session, "chat_id": "001", "task_id": 2})
            for name in SUPERVISOR_ONLY_TOOLS:
                args = {"session_address": "forged", "chat_id": "other", "task_id": 0, "seq": 1, "tool_result_seq": 1}
                await registry.call(AgentRole.SUPERVISOR, name, args)
                self.assertEqual(client.call_tool.call_args.kwargs["argument"]["session_address"], workspace.session)
                self.assertEqual(client.call_tool.call_args.kwargs["argument"]["chat_id"], "001")
            calls = client.call_tool.call_count
            denied = await registry.call(AgentRole.EXECUTOR, "read_tool_result", {"tool_result_seq": 1})
            self.assertEqual(denied.error_type, "permission_denied")
            self.assertEqual(client.call_tool.call_count, calls)
            approval.assert_not_called()
            self.assertIsNone(workspace.project_root)
            self.assertEqual(workspace.tool_permissions, [])

    async def test_in_process_supervisor_history_needs_no_consent_or_project(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            approval = Mock(side_effect=AssertionError("history must not request consent"))
            workspace, gateway, _ = self.workspace(directory, approval, candidate=False)
            store = SessionStore(workspace.session, "001")
            store.initialize()
            run = ActiveRun(store)
            registry = ToolRegistry(host_for(["read_tool_result"]), execution_gateway=gateway,
                context_provider=lambda: {"session_address": workspace.session, "chat_id": "001", "task_id": 0})
            token = ACTIVE_RUN.set(run)
            try:
                result = await registry.call(AgentRole.SUPERVISOR, "read_tool_result",
                    {"tool_result_seq": 1, "session_address": "forged", "chat_id": "other"})
            finally:
                ACTIVE_RUN.reset(token)
            self.assertEqual(result.output["status"], "not_found")
            approval.assert_not_called()
            self.assertFalse((Path(workspace.session) / "tool_results.jsonl").exists())

    async def test_supervisor_file_tool_still_requires_consent_and_role_veto_remains(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            approval = Mock(return_value=True)
            workspace, gateway, project = self.workspace(directory, approval)
            registry = ToolRegistry(host_for(["read_all_files_tool", "write_in_tool"]), execution_gateway=gateway)
            result = await registry.call(AgentRole.SUPERVISOR, "read_all_files_tool", {"home_address": str(project)})
            self.assertTrue(result.ok, result.message)
            self.assertEqual(approval.call_args.args[0]["actor"], "supervisor")
            approval.reset_mock()
            result = await registry.call(AgentRole.SUPERVISOR, "write_in_tool", {"file_address": str(project / "main.py"), "code": "new"})
            self.assertEqual(result.error_type, "permission_denied")
            approval.assert_not_called()

    async def test_source_root_cannot_expand_to_state_or_drive_or_parent(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            approval = Mock(return_value=True)
            workspace, gateway, project = self.workspace(directory, approval, candidate=False)
            Path(workspace.state_dir).mkdir()
            for path in (workspace.state_dir, Path(project).anchor, str(project / "..")):
                self.assertFalse((await gateway.call("read_all_files_tool", {"home_address": str(path)})).ok)
            approval.assert_not_called()
            self.assertIsNone(workspace.project_root)

    async def test_supervisor_denial_stops_current_request_without_more_models(self):
        async def register(host, *args, **kwargs):
            sample = host_for(["read_all_files_tool", "read_files_content_tool"])
            host.tools, host.tool_dictionary, host.session_dictionary = sample.tools, sample.tool_dictionary, {}
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            atomic_bytes(project / "main.py", b"untouched\n")
            terminal = Terminal(parser().parse_args(["--state-dir", str(base / "state"), "--settings-file", str(base / "settings.json")]), output=lambda _: None)
            prompts = []
            terminal.approval = lambda r: prompts.append(r) or r.get("actor") != "supervisor"
            chat = QueueChat([
                model_response(json_message(task_list=["读项目"], description="规划")),
                model_response(json_message(description="指导", executor_guidance="读取目录", completion_criteria="目录证据")),
                model_response(tool=[openai_tool("read_all_files_tool", {"home_address": str(project)})]),
                model_response(json_message(description="目录已读取", is_finished=True)),
                model_response(tool=[openai_tool("read_files_content_tool", {"home_address": str(project / "main.py")})]),
            ])
            with (patch("AgentLoop.agent.load_runtime_config", return_value=CONFIG),
                  patch("AgentLoop.agent._register_mcp_clients", new=register),
                  patch("AgentLoop.agent._build_model_client", return_value=object()),
                  patch("AgentLoop.agent._close_model_client", new=AsyncMock()),
                  patch("AgentLoop.agent._chat_map", return_value={"chatgpt": chat})):
                result = await terminal.ask("分析 " + str(project))
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(terminal.status, "denied")
            self.assertEqual(len(chat.calls), 5)
            self.assertEqual([r["actor"] for r in prompts], ["executor", "supervisor"])
            self.assertFalse(terminal.store.load()["unknown_operations"])
            self.assertEqual(terminal.run.bundle["agent"].now_task_id, 0)
            self.assertEqual(terminal.run.bundle["agent"].task_outcomes, {})
            self.assertEqual((project / "main.py").read_bytes(), b"untouched\n")


class ToolPermissionTerminalTests(unittest.TestCase):
    def test_terminal_only_yes_no_and_no_directory_prompt(self):
        for answer in ("yes", "no", "", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=type(answer).__name__), tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
                base = Path(directory)
                output, prompts = [], []
                def read(prompt):
                    prompts.append(prompt)
                    if isinstance(answer, BaseException):
                        raise answer
                    return answer
                terminal = Terminal(parser().parse_args(["--state-dir", str(base / "state"), "--settings-file", str(base / "settings.json")]),
                    input_function=read, output=output.append)
                request = {"action": "tool_call", "actor": "executor", "operation": "read", "tool_name": "read_all_files_tool",
                    "arguments": {"home_address": str(base / "project with spaces")}, "actual_execution": {"backend": "host_read_only"}}
                with patch("sys.stdin.isatty", return_value=True):
                    self.assertEqual(terminal.approval(request), answer in {"yes", ""} if isinstance(answer, str) else False)
                self.assertEqual(len(prompts), 1)
                self.assertIn("yes/no", prompts[0])
                self.assertIn("请求访问/查看", "\n".join(output))
                self.assertNotIn("项目绝对路径", "\n".join(output + prompts))
                self.assertIsNone(terminal.project_root)

    def test_noninteractive_never_reads_stdin_for_permissions(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            base = Path(directory)
            input_function = Mock(side_effect=AssertionError("must not read stdin"))
            terminal = Terminal(parser().parse_args(["--state-dir", str(base / "state"), "--settings-file", str(base / "settings.json"), "--query", "test"]),
                input_function=input_function, output=lambda _: None)
            with patch("sys.stdin.isatty", return_value=True):
                self.assertFalse(terminal.approval({"action": "tool_call"}))
            terminal.args.query = None
            with patch("sys.stdin.isatty", return_value=False):
                self.assertFalse(terminal.approval({"action": "tool_call"}))
            input_function.assert_not_called()


@unittest.skipUnless(os.environ.get("CODING_AGENT_STAGING_DOCKER_INTEGRATION") == "1", "per-call Docker test requires opt-in")
class PerToolDockerTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_gateway_requires_each_yes_then_executes_only_in_copy(self):
        """Use an explicit shell test toolchain, not a pretend Python/JDK validation."""
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            base = Path(directory)
            project = base / "source"
            project.mkdir()
            atomic_bytes(project / "probe.py", b"printf 'isolated-test\\n' > /workspace/result.txt\n")
            prompts = []
            allowed = True
            def approve(request):
                prompts.append(request)
                return allowed
            workspace = ProjectWorkspace(base / "state/session", "001", "main", "r_docker", base / "state", project,
                approve=approve, per_tool_permissions=True,
                image=os.environ.get("CODING_AGENT_STAGING_DOCKER_IMAGE", "nginx:alpine"), toolchains={"python": "/bin/sh"})
            gateway = ExecutionGateway(None, project_workflow=workspace)
            result = await gateway.call("run_code_get_feedback_tool", {"code_path": str(project / "probe.py"), "editor_address": "/bin/sh"})
            self.assertTrue(result.ok, result.message + str(result.output))
            self.assertFalse((project / "result.txt").exists())
            stage = Path(workspace.staging_root)
            self.assertEqual((stage / "result.txt").read_bytes(), b"isolated-test\n")
            self.assertEqual([r["action"] for r in prompts], ["create_staging", "tool_call"])
            allowed = False
            with patch.object(SandboxRunner, "run", side_effect=AssertionError("denied execution")):
                with self.assertRaises(RequestDenied):
                    await gateway.call("run_code_get_feedback_tool", {"code_path": str(project / "probe.py"), "editor_address": "/bin/sh"})
            self.assertEqual([r["action"] for r in prompts], ["create_staging", "tool_call", "tool_call"])
            self.assertFalse(gateway.runner.containers)

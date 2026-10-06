import asyncio
import ast
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from MCP_functions.sandbox import SandboxPolicy, SandboxRunner, POLICY, PermissionDenied, safe_display
from MCP_functions.execution_gateway import ExecutionGateway
from MCP_functions.System_Files.Files_function.write_in import write_in
from MCP_functions.System_Files.Files_function.bfs_read import read_files_content
from MCP_functions.System_Files.Files_server import mcp_system_server

class SandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_path_modes_protected_state_traversal_and_link_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            outside = root / "outside"
            outside.mkdir()
            policy = SandboxPolicy(str(work), str(root / "state"), mode="read-only")
            token = POLICY.set(policy)
            try:
                self.assertEqual(write_in(str(work / "file.txt"), "no")["status"], "error")
                self.assertEqual(read_files_content(str(outside))["status"], "error")
                with self.assertRaises(PermissionDenied):
                    policy.check_path(work / ".." / "outside")
                with self.assertRaises(PermissionDenied):
                    policy.check_path(root / "state" / "checkpoint.json")
                POLICY.set(SandboxPolicy(str(work), str(root / "state")))
                self.assertEqual(write_in(str(work / "file.txt"), "yes")["status"], "success")
                try:
                    (work / "link").symlink_to(outside, target_is_directory=True)
                except OSError:
                    pass  # OS may require link privilege; Docker checks are independent.
                else:
                    with self.assertRaises(PermissionDenied):
                        POLICY.get().check_path(work / "link" / "secret")
            finally:
                POLICY.reset(token)

    async def test_backend_unavailable_never_falls_back_and_unknown_external_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            work.mkdir()
            policy = SandboxPolicy(str(work), str(Path(directory) / "state"))
            runner = SandboxRunner(policy)
            with patch.object(runner, "availability", return_value={"available": False}), patch("MCP_functions.sandbox.subprocess.Popen") as spawn:
                with self.assertRaises(PermissionDenied):
                    runner.run(["python", "script.py"])
                spawn.assert_not_called()
            gateway = ExecutionGateway(policy)
            raw = AsyncMock()
            result = await gateway.call("read_unknown_remote_write", {}, raw)
            self.assertFalse(result.ok)
            raw.assert_not_awaited()
            result = await gateway.call("write_in_tool", {"file_address": str(work / "file"), "code": "x", "confirmed": True})
            self.assertFalse(result.ok)

    async def test_noninteractive_mcp_and_approval_is_one_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            work.mkdir()
            policy = SandboxPolicy(str(work), str(Path(directory) / "state"))
            arguments = {"editor_address": "python", "package_name": "requests==2.32.3"}
            deny = ExecutionGateway(policy, approve=lambda _: False)
            with patch.object(deny, "_confirmed") as operation:
                self.assertFalse((await deny.call("download_package_with_confirmation_tool", arguments)).ok)
                operation.assert_not_called()
            allow = ExecutionGateway(policy, approve=lambda _: True)
            with patch.object(allow, "_confirmed", return_value={"status": "success"}) as operation:
                self.assertTrue((await allow.call("download_package_with_confirmation_tool", arguments)).ok)
                self.assertEqual(operation.call_count, 1)
            changed = ExecutionGateway(policy)
            def alter(request):
                request["arguments_hash"] = "forged"
                return True
            changed.approve = alter
            with patch.object(changed, "_confirmed") as operation:
                self.assertFalse((await changed.call("download_package_with_confirmation_tool", arguments)).ok)
                operation.assert_not_called()
            with patch("builtins.input", side_effect=AssertionError("MCP must not consume stdin")):
                with patch.object(mcp_system_server, "download_package_with_confirmation", return_value={"status": "confirmation_required"}) as wrapper:
                    result = await mcp_system_server.download_package_with_confirmation_tool("python", "requests")
                    self.assertEqual(result["status"], "confirmation_required")
                    self.assertFalse(wrapper.call_args.kwargs["interactive"])

    async def test_all_project_process_calls_use_gateway_and_display_escapes_control_codes(self):
        folder = Path(__file__).resolve().parents[1] / "MCP_functions/System_Files/Files_function"
        for name in ("python_code.py", "java_code.py"):
            tree = ast.parse((folder / name).read_text(encoding="utf-8"))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                     and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}]
            self.assertEqual(calls, [], name)
        self.assertNotIn("\x1b", safe_display("\x1b[31m malicious"))

@unittest.skipUnless(os.environ.get("CODING_AGENT_DOCKER_INTEGRATION") == "1", "real Docker tests require explicit opt-in")
class DockerIntegrationTests(unittest.TestCase):
    def test_real_mount_readonly_network_credentials_and_cancel_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            secret = root / "host-secret.txt"
            secret.write_text("HOST_ONLY", encoding="utf-8")
            image = os.environ.get("CODING_AGENT_DOCKER_TEST_IMAGE", "nginx:alpine")
            policy = SandboxPolicy(str(work), str(root / "state"), image=image, toolchains={"sh": "/bin/sh"})
            runner = SandboxRunner(policy)
            self.assertTrue(runner.availability()["available"])
            result = runner.run(["sh", "-c", "echo OK > /workspace/allowed.txt; cat /workspace/allowed.txt"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "FAKE_TEST_VALUE", "ANTHROPIC_API_KEY": "FAKE_TEST_VALUE"}):
                result = runner.run(["sh", "-c", "test ! -e /host-secret.txt && test -z \"$OPENAI_API_KEY\" && test -z \"$ANTHROPIC_API_KEY\" && ! cat /host-secret.txt && ! sh -c 'echo DENIED > /host-secret.txt'"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(secret.read_text(encoding="utf-8"), "HOST_ONLY")
            from dataclasses import replace
            readonly = SandboxRunner(replace(policy, mode="read-only"))
            self.assertNotEqual(readonly.run(["sh", "-c", "echo NO > /workspace/denied.txt"]).returncode, 0)
            self.assertFalse((work / "denied.txt").exists())
            result = runner.run(["sh", "-c", "wget -T 2 -O /tmp/network http://1.1.1.1"])
            self.assertNotEqual(result.returncode, 0)
            completed = []
            thread = threading.Thread(target=lambda: completed.append(runner.run(["sh", "-c", "sleep 120 & wait"], timeout=30)))
            thread.start()
            deadline = time.monotonic() + 10
            while not runner.containers and time.monotonic() < deadline:
                time.sleep(0.02)
            owned = list(runner.containers)
            self.assertTrue(owned)
            runner.cancel_all()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            for name in owned:
                result = subprocess.run(["docker", "inspect", name], capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)

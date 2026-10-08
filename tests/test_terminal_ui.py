"""UI-only regressions: role colors and explicit yes/no keyboard selection."""
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from terminal_cli import Terminal, parser
from terminal_render import Renderer, RENDERER, process
from terminal_select import choose_yes_no, _windows_key
from test_agent_loop import TEST_TEMP_ROOT


class RoleColorTests(unittest.TestCase):
    def test_roles_are_distinct_dim_white_and_green_and_untrusted_ansi_is_sanitized(self):
        output = []
        with patch.dict(os.environ, {}, clear=True):
            renderer = Renderer(output.append, "always")
            renderer.say("executor\x1b[31m injected", "executor")
            renderer.say("supervisor", "supervisor")
        self.assertTrue(output[0].startswith("\x1b[2;37m"))
        self.assertIn("\\x1b[31m", output[0])
        self.assertTrue(output[1].startswith("\x1b[2;32m"))
        self.assertTrue(all(line.endswith("\x1b[0m") for line in output))

    def test_loop_messages_use_explicit_actor_not_keywords_in_untrusted_text(self):
        output = []
        with patch.dict(os.environ, {}, clear=True):
            token = RENDERER.set(Renderer(output.append, "always"))
            try:
                process("进入 executor", actor="executor")
                process("进入 supervisor", actor="supervisor")
                process("untrusted text mentions supervisor")
            finally:
                RENDERER.reset(token)
        self.assertTrue(output[0].startswith("\x1b[2;37m"))
        self.assertTrue(output[1].startswith("\x1b[2;32m"))
        self.assertTrue(output[2].startswith("\x1b[2;90m"))

    def test_event_role_colors_preserve_warning_error_and_final_answer_priority(self):
        self.assertEqual(Terminal.event_style({"kind": "boundary", "actor": "executor"}), "executor")
        self.assertEqual(Terminal.event_style({"kind": "tool_completed", "actor": "supervisor", "ok": True}), "supervisor")
        self.assertEqual(Terminal.event_style({"kind": "authorization", "actor": "supervisor"}), "warning")
        self.assertEqual(Terminal.event_style({"kind": "tool_completed", "actor": "executor", "ok": False}), "error")
        self.assertEqual(Terminal.event_style({"kind": "answer", "actor": "supervisor"}), "normal")
        self.assertEqual(Terminal.event_style({"kind": "boundary", "actor": "host"}), "dim")

    def test_no_color_never_and_redirected_output_remain_plain(self):
        for mode, env, tty in (("never", {}, True), ("always", {"NO_COLOR": "1"}, True), ("auto", {}, False)):
            with self.subTest(mode=mode, env=env), patch.dict(os.environ, env, clear=True):
                output = []
                renderer = Renderer(output.append, mode, stream=Mock(isatty=lambda: tty))
                renderer.say("executor", "executor")
                renderer.say("supervisor", "supervisor")
                self.assertEqual(output, ["executor", "supervisor"])


class KeyboardChoiceTests(unittest.TestCase):
    def choose(self, keys):
        stream, output = io.StringIO(), []
        reader = iter(keys)
        result = choose_yes_no(Renderer(output.append, "never"), key_reader=lambda: next(reader), stream=stream)
        return result, stream.getvalue(), output

    def test_default_yes_requires_enter_confirmation(self):
        stream = io.StringIO()
        reader = Mock()
        def enter():
            self.assertIn("> [ yes ]", stream.getvalue())
            return "\r"
        reader.side_effect = enter
        self.assertTrue(choose_yes_no(Renderer(lambda _: None, "never"), key_reader=reader, stream=stream))
        reader.assert_called_once()
        self.assertTrue(stream.getvalue().endswith("\n"))

    def test_arrows_select_no_or_yes_then_enter_confirms(self):
        for keys, expected in ((["right", "\r"], False), (["down", "\r"], False),
                               (["right", "left", "\r"], True), (["down", "up", "\r"], True)):
            with self.subTest(keys=keys):
                result, text, _ = self.choose(keys)
                self.assertEqual(result, expected)
                self.assertIn("> [ no  ]", text)

    def test_tab_toggles_and_unknown_keys_do_not_confirm(self):
        self.assertFalse(self.choose(["\t", "x", "\r"])[0])
        self.assertTrue(self.choose(["\t", "\t", "unknown", "\r"])[0])

    def test_cancel_and_eof_never_approve_default_yes(self):
        for key in ("\x1b", "\x03", "\x1a", "", "escape"):
            with self.subTest(key=repr(key)):
                result, text, _ = self.choose([key])
                self.assertFalse(result)
                self.assertIn("> [ no  ]", text)
        for exc in (EOFError(), KeyboardInterrupt(), OSError("console unavailable")):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(choose_yes_no(Renderer(lambda _: None, "never"),
                    key_reader=Mock(side_effect=exc), stream=io.StringIO()))

    def test_yes_shortcut_only_selects_and_cancel_still_denies(self):
        self.assertFalse(self.choose(["right", "y", "\x1b"])[0])
        self.assertTrue(self.choose(["right", "y", "\r"])[0])

    def test_windows_extended_key_codes_are_decoded_without_newline_input(self):
        for code, expected in (("H", "up"), ("P", "down"), ("K", "left"), ("M", "right")):
            for prefix in ("\x00", "\xe0"):
                fake = SimpleNamespace(getwch=Mock(side_effect=[prefix, code]))
                with self.subTest(code=code, prefix=repr(prefix)), patch.dict("sys.modules", {"msvcrt": fake}):
                    self.assertEqual(_windows_key(), expected)
                    self.assertEqual(fake.getwch.call_count, 2)

    def test_windows_real_tty_routes_to_keyboard_menu_not_input(self):
        stream = Mock()
        stream.isatty.return_value = True
        with (patch("terminal_select.os.name", "nt"), patch("terminal_select.sys.stdin.isatty", return_value=True),
              patch("terminal_select._windows_key", return_value="\r") as reader):
            self.assertTrue(choose_yes_no(Renderer(lambda _: None, "never"), stream=stream))
        reader.assert_called_once()

    def test_numbered_fallback_keeps_explicit_default_yes_and_denial(self):
        for answer in ("", "1", "yes", "2", "no", "bad"):
            with self.subTest(answer=answer):
                output = []
                result = choose_yes_no(Renderer(output.append, "never"), input_function=lambda _: answer, stream=io.StringIO())
                self.assertEqual(result, answer in {"", "1", "yes"})
                self.assertIn("1) yes", "\n".join(output))
        self.assertFalse(choose_yes_no(Renderer(lambda _: None, "never"),
            input_function=Mock(side_effect=EOFError()), stream=io.StringIO()))

    def test_noninteractive_approval_does_not_open_menu_or_auto_approve(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            args = parser().parse_args(["--state-dir", directory, "--settings-file", directory + "/settings.json", "--query", "test"])
            terminal = Terminal(args, output=lambda _: None)
            with patch("terminal_cli.choose_yes_no", side_effect=AssertionError("no menu")):
                self.assertFalse(terminal.approval({"action": "tool_call"}))
                terminal.args.query = None
                with patch("terminal_cli.sys.stdin.isatty", return_value=False):
                    self.assertFalse(terminal.approval({"action": "tool_call"}))

    def test_permission_question_is_shown_before_selector_and_boolean_is_returned(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            output = []
            terminal = Terminal(parser().parse_args(["--state-dir", directory, "--settings-file", directory + "/settings.json"]), output=output.append)
            request = {"action": "tool_call", "actor": "supervisor", "operation": "read", "tool_name": "read_all_files_tool",
                       "arguments": {"home_address": directory}, "actual_execution": {"backend": "host_read_only"}}
            def select(*args):
                self.assertIn("supervisor 请求访问/查看", "\n".join(output))
                self.assertIn("yes：", "\n".join(output))
                self.assertIn("no ：", "\n".join(output))
                return False
            with patch("terminal_cli.sys.stdin.isatty", return_value=True), patch("terminal_cli.choose_yes_no", side_effect=select) as selector:
                self.assertFalse(terminal.approval(request))
                selector.assert_called_once()

"""Trusted, optional styles; stored events and untrusted text stay plain."""
import contextvars
import os
import sys

from MCP_functions.sandbox import safe_display

RENDERER = contextvars.ContextVar("coding_agent_terminal_renderer", default=None)


class Renderer:
    def __init__(self, output=print, color="auto", stream=None):
        self.output = output
        stream = stream or sys.stdout
        self.color = ("NO_COLOR" not in os.environ and color != "never"
                      and (color == "always" or (stream.isatty() and os.environ.get("TERM") != "dumb")))
        if self.color and os.name == "nt":
            # Enable VT on Windows consoles; failures naturally downgrade auto.
            try:
                import ctypes
                handle = ctypes.windll.kernel32.GetStdHandle(-11)
                mode = ctypes.c_ulong()
                enabled = (ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode))
                           and ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 4))
                if not enabled and color == "auto":
                    self.color = False
            except (AttributeError, OSError):
                if color == "auto":
                    self.color = False

    def format(self, value, style="normal"):
        text = safe_display(value)
        code = {"dim": "2;90", "executor": "2;37", "supervisor": "2;32",
                "warning": "1;33", "error": "1;31"}.get(style)
        return f"\x1b[{code}m{text}\x1b[0m" if self.color and code else text

    def say(self, value, style="normal"):
        self.output(self.format(value, style))


def process(value, actor=None):
    renderer = RENDERER.get()
    if renderer:
        renderer.say(value, actor if actor in {"executor", "supervisor"} else "dim")
    else:
        print(safe_display(value))


def show_description(actor, description, *, task_id=None, phase=None, seq=None):
    """Presentation only; same role style as process messages, no text guessing."""
    if not isinstance(description, str) or not description.strip():
        return
    fields = [actor + " description"]
    if task_id is not None:
        fields.append(f"task={task_id}")
    if phase:
        fields.append(f"phase={phase}")
    if seq is not None:
        fields.append(f"seq={seq}")
    process(" ".join(fields) + "\n" + description, actor=actor)

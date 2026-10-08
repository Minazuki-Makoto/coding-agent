"""Host-only yes/no selection. No dependencies or model-controlled key handling."""
import builtins
import os
import sys


def _windows_key():
    import msvcrt
    key = msvcrt.getwch()
    if key in {"\x00", "\xe0"}:
        return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(msvcrt.getwch(), "unknown")
    return key


def choose_option(renderer, options, input_function=builtins.input, *, key_reader=None, stream=None):
    """Return a confirmed option index, or None on cancellation; no execution."""
    stream = stream or sys.stdout
    if not options:
        return None
    for index, label in enumerate(options, 1):
        renderer.say(f"  {index}) {label}", "dim")
    if key_reader is None:
        if (os.name == "nt" and input_function is builtins.input
                and sys.stdin.isatty() and stream.isatty()):
            key_reader = _windows_key
        else:
            while True:
                try:
                    answer = input_function("选择编号（空行取消）：").strip()
                    if not answer:
                        return None
                    index = int(answer) - 1
                    if 0 <= index < len(options):
                        return index
                except (EOFError, KeyboardInterrupt, OSError):
                    return None
                except ValueError:
                    pass
                renderer.say("请输入列表中的编号，或空行取消。", "warning")
    renderer.say("↑/↓ 或 Tab 选择，Enter 打开，Esc 取消；仅打开状态，不执行任务。", "dim")
    selected = 0
    def draw():
        stream.write("\r" + renderer.format(f"> selected: [ {selected + 1:4d} / {len(options):4d} ]", "dim"))
        stream.flush()
    try:
        draw()
        while True:
            key = key_reader()
            if key in {"\r", "\n"}:
                return selected
            if key in {"", "\x1b", "\x03", "\x1a", "escape"}:
                return None
            if key in {"up", "left"}:
                selected = (selected - 1) % len(options)
            elif key in {"down", "right", "\t"}:
                selected = (selected + 1) % len(options)
            else:
                continue
            draw()
    except (EOFError, KeyboardInterrupt, OSError):
        return None
    finally:
        stream.write("\n")
        stream.flush()


def choose_yes_no(renderer, input_function=builtins.input, *, key_reader=None, stream=None):
    """Arrow/Tab selection on Windows TTY; safe numbered fallback elsewhere.

    Merely highlighting yes is not consent. Only Enter on yes returns True.
    Redirected/noninteractive permission refusal remains enforced by Terminal.
    Injected input functions use the line fallback, never the real console.
    """
    stream = stream or sys.stdout
    if key_reader is None:
        if (os.name == "nt" and input_function is builtins.input
                and sys.stdin.isatty() and stream.isatty()):
            key_reader = _windows_key
        else:
            renderer.say("选项：1) yes（允许，默认）  2) no（拒绝）", "warning")
            try:
                return input_function("请选择 yes/no（1/2）[默认 1=yes，Enter 确认]：").strip().lower() in {"", "1", "y", "yes"}
            except (EOFError, KeyboardInterrupt, OSError):
                return False

    renderer.say("方向键或 Tab 切换选项，Enter 确认；Esc 取消（默认 yes）。", "dim")
    selected_yes = True

    def draw():
        # Fixed-width ASCII buttons need only carriage return, even with NO_COLOR.
        buttons = "> [ yes ]    [ no  ]" if selected_yes else "  [ yes ]  > [ no  ]"
        stream.write("\r" + renderer.format(buttons, "warning"))
        stream.flush()

    try:
        draw()
        while True:
            key = key_reader()
            if key in {"\r", "\n"}:
                return selected_yes
            if key in {"", "\x1b", "\x03", "\x1a", "escape"}:
                selected_yes = False
                draw()
                return False
            if key in {"up", "left", "y", "Y"}:
                selected_yes = True
            elif key in {"down", "right", "n", "N"}:
                selected_yes = False
            elif key == "\t":
                selected_yes = not selected_yes
            else:
                continue
            draw()
    except (EOFError, KeyboardInterrupt, OSError):
        return False
    finally:
        stream.write("\n")
        stream.flush()

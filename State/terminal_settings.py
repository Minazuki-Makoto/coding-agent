"""User settings independent of any replaceable session storage directory."""
import json
import os
import tempfile
from pathlib import Path

from MCP_functions.System_Files.Files_function.write_in import _atomic_write


def default_state_dir():
    return Path("D:/coding-agent-state") if os.name == "nt" else Path.home() / ".local/share/coding-agent/state"


def settings_path():
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local")) if os.name == "nt" else Path.home() / ".config"
    return base / "coding-agent/settings.json"


def validate_state_dir(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("状态存储目录必须是明确的绝对路径")
    path = path.resolve()
    if path.parent == path or path == Path.home():
        raise ValueError("不能使用盘符根或整个用户目录存储状态")
    path.mkdir(parents=True, exist_ok=True)
    # A successful mkdir does not prove write access. Do not silently relocate.
    with tempfile.TemporaryFile(dir=path) as probe:
        probe.write(b"coding-agent storage probe")
        probe.flush()
        os.fsync(probe.fileno())
    return path


class TerminalSettings:
    def __init__(self, path=None):
        self.path = Path(path) if path else settings_path()
        self.values = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(self.values, dict):
            raise ValueError("用户设置必须是 JSON 对象；未改写原配置")
        if "network" in self.values and type(self.values["network"]) is not bool:
            raise ValueError("network 设置必须是 bool；未改写原配置")
        if not isinstance(self.values.get("extra_excludes", []), list) or any(not isinstance(v, str) for v in self.values.get("extra_excludes", [])):
            raise ValueError("extra_excludes 必须是字符串列表；未改写原配置")

    def state_dir(self):
        return self.values.get("state_dir", str(default_state_dir()))

    def set_state_dir(self, value):
        target = validate_state_dir(value)
        values = {**self.values, "state_dir": str(target)}
        _atomic_write(self.path, json.dumps(values, ensure_ascii=False, indent=2))
        self.values = values
        return target

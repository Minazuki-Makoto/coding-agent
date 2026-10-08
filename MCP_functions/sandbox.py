"""Trusted path policy and real Docker execution gateway (no restricted fallback)."""
from __future__ import annotations

import contextvars
import json
import os
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path


POLICY = contextvars.ContextVar("coding_agent_sandbox_policy", default=None)
RUNNERS = set()
MINIMAL_ENV_KEYS = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
                    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "USERPROFILE"}


class PermissionDenied(PermissionError):
    pass


@dataclass(frozen=True)
class SandboxPolicy:
    workspace: str
    state_dir: str
    mode: str = "workspace-write"
    readable_roots: tuple[str, ...] = ()
    writable_roots: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    network: bool = False
    image: str = "coding-agent-sandbox:local"
    output_max_chars: int = 20000
    toolchains: dict = field(default_factory=lambda: {"python": "/usr/local/bin/python",
        "java": "/usr/lib/jvm/java-17-openjdk-amd64/bin/java", "javac": "/usr/lib/jvm/java-17-openjdk-amd64/bin/javac",
        "mvn": "/usr/bin/mvn", "gradle": "/usr/bin/gradle", "sh": "/bin/sh"})
    version: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self):
        if self.mode not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("invalid sandbox mode")
        root = Path(self.workspace).resolve()
        if not root.is_dir() or root.parent == root:
            raise ValueError("workspace must be a selected existing directory, not a drive root")
        if Path(self.state_dir).resolve() == root:
            raise ValueError("state directory must be dedicated, not the workspace root")
        for item in (*self.readable_roots, *self.writable_roots):
            path = Path(item).resolve()
            if path.parent == path or path == Path.home():
                raise ValueError("drive roots and user home cannot be default permission roots")

    def check_path(self, value, write=False):
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise PermissionDenied("empty path")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = Path(self.workspace) / candidate
        if ".." in candidate.parts:
            raise PermissionDenied("parent traversal is not allowed")
        for item in (*candidate.parents, candidate):
            if item.exists() or item.is_symlink():
                info = item.lstat()
                if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise PermissionDenied("symlink/junction/reparse paths are not allowed")
        candidate = candidate.resolve(strict=False)
        protected = (self.state_dir, *self.protected_paths)
        if any(_within(candidate, Path(item).resolve()) for item in protected):
            raise PermissionDenied("Host state/configuration is protected")
        if self.mode == "danger-full-access":
            return candidate
        if write and self.mode == "read-only":
            raise PermissionDenied("read-only sandbox prohibits workspace changes")
        roots = (self.workspace, *(self.writable_roots if write else self.readable_roots))
        if not any(_within(candidate, Path(root).resolve()) for root in roots):
            raise PermissionDenied("path outside trusted permission roots")
        return candidate

    def describe(self):
        return {**asdict(self), "backend": "host-unisolated" if self.mode == "danger-full-access" else "docker",
                "network_enforced": self.mode != "danger-full-access",
                "effective_network": self.network if self.mode != "danger-full-access" else "unrestricted host network",
                "network_granularity": "on/off, not domain filtering",
                "process_isolated": self.mode != "danger-full-access",
                "extra_mounts": "only selected workspace; extra file roots are not container mounts"}


def _within(path, root):
    return path == root or path.is_relative_to(root)


def current_policy():
    policy = POLICY.get()
    if policy is None and os.environ.get("CODING_AGENT_SANDBOX_POLICY"):
        policy = SandboxPolicy(**json.loads(os.environ["CODING_AGENT_SANDBOX_POLICY"]))
    return policy


def check_path(value, write=False):
    policy = current_policy()
    return policy.check_path(value, write) if policy else Path(value).resolve(strict=False)


class SandboxRunner:
    def __init__(self, policy):
        self.policy = policy
        self.processes = set()
        self.containers = set()
        self.guard = threading.Lock()
        RUNNERS.add(self)

    def availability(self):
        if self.policy.mode == "danger-full-access":
            return {"available": True, "backend": "host-unisolated"}
        try:
            result = subprocess.run(["docker", "image", "inspect", self.policy.image],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    timeout=5, env=self._environment())
            return {"available": result.returncode == 0, "backend": "docker",
                    "reason": "" if result.returncode == 0 else "Docker daemon/image unavailable; no host fallback"}
        except (OSError, subprocess.SubprocessError):
            return {"available": False, "backend": "docker", "reason": "Docker unavailable; no host fallback"}

    def _environment(self):
        return {key: value for key, value in os.environ.items() if key.upper() in MINIMAL_ENV_KEYS}

    def _map(self, value):
        text = str(value)
        basename = Path(text).name.lower().removesuffix(".exe")
        if basename in self.policy.toolchains:
            return self.policy.toolchains[basename]
        if Path(text).is_absolute():
            path = self.policy.check_path(text)
            root = Path(self.policy.workspace).resolve()
            if not _within(path, root):
                raise PermissionDenied("host toolchain/cache path is not mounted in the container")
            return "/workspace/" + path.relative_to(root).as_posix()
        return text

    def run(self, arguments, *, cwd=None, timeout=30, network=None, **ignored):
        if not isinstance(arguments, (list, tuple)) or not arguments:
            raise PermissionDenied("execution requires an argument list, not shell text")
        if ignored.get("shell"):
            raise PermissionDenied("shell=True is not a trusted execution interface")
        workspace = self.policy.check_path(cwd or self.policy.workspace)
        actual_network = self.policy.network if network is None else network
        if actual_network and not self.policy.network:
            raise PermissionDenied("network must be enabled by trusted policy")
        name = None
        if self.policy.mode == "danger-full-access":
            command = [str(item) for item in arguments]
            process_cwd = str(workspace)
        else:
            if not self.availability()["available"]:
                raise PermissionDenied("sandbox_backend_unavailable: Docker/image unavailable; execution refused")
            root = Path(self.policy.workspace).resolve()
            # Mounting a workspace containing Host secrets/state would leak them to project code.
            for protected in (self.policy.state_dir, *self.policy.protected_paths):
                if _within(Path(protected).resolve(), root):
                    raise PermissionDenied("workspace mount contains protected Host state/config; select a separate workspace")
            name = "coding-agent-" + uuid.uuid4().hex
            mount = f"type=bind,source={root},target=/workspace"
            if self.policy.mode == "read-only":
                mount += ",readonly"
            mapped_cwd = "/workspace/" + workspace.relative_to(root).as_posix()
            mapped = [self._map(value) for value in arguments]
            command = ["docker", "create", "--pull=never", "--name", name,
                       "--network", "bridge" if actual_network else "none", "--read-only",
                       "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
                       "--memory=512m", "--cpus=1", "--user=65534:65534", "--init",
                       "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
                       "--env", "HOME=/tmp", "--env", "PYTHONPATH=/workspace/.agent_packages",
                       "--env", "GRADLE_USER_HOME=/tmp/.gradle", "--env", "MAVEN_OPTS=-Duser.home=/tmp",
                       "--mount", mount, "--workdir", mapped_cwd,
                       "--entrypoint", mapped[0], self.policy.image, *mapped[1:]]
            process_cwd = None
            created = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                     timeout=15, env=self._environment())
            if created.returncode:
                raise PermissionDenied("Docker sandbox creation failed: " + created.stderr.decode("utf-8", errors="replace"))
            # A cancellation-visible container already exists before user code can start.
            with self.guard:
                self.containers.add(name)
            command = ["docker", "start", "--attach", name]
        # Drain all output into bounded buffers; no unbounded communicate() allocation.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=process_cwd, env=self._environment(),
                                   start_new_session=os.name != "nt",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        with self.guard:
            self.processes.add(process)
            if name:
                self.containers.add(name)
        buffers = [bytearray(), bytearray()]
        limit = self.policy.output_max_chars * 4
        def drain(stream, buffer):
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                remaining = max(0, limit - len(buffer))
                buffer.extend(chunk[:remaining])
        threads = [threading.Thread(target=drain, args=(stream, buffer), daemon=True)
                   for stream, buffer in zip((process.stdout, process.stderr), buffers)]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=timeout)
            exit_code = process.returncode
            if name:
                inspected = subprocess.run(["docker", "inspect", "--format", "{{.State.ExitCode}}", name],
                                           capture_output=True, timeout=5, env=self._environment())
                if inspected.returncode == 0:
                    exit_code = int(inspected.stdout.strip())
        except (subprocess.TimeoutExpired, BaseException):
            self._terminate(process, name)
            raise
        finally:
            for thread in threads:
                thread.join(timeout=3)
            process.stdout.close()
            process.stderr.close()
            if name:
                subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10, env=self._environment())
            with self.guard:
                self.processes.discard(process)
                if name:
                    self.containers.discard(name)
        stdout, stderr = (bytes(buffer).decode("utf-8", errors="replace") for buffer in buffers)
        return subprocess.CompletedProcess(arguments, exit_code, stdout, stderr)

    def _terminate(self, process, name=None):
        if name:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, env=self._environment())
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, env=self._environment())
            else:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if name:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, env=self._environment())

    def cancel_all(self):
        with self.guard:
            processes, containers = list(self.processes), list(self.containers)
        for name in containers:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, env=self._environment())
        for process in processes:
            self._terminate(process)


def execution_run(arguments, **kwargs):
    policy = current_policy()
    if policy is None:
        raise PermissionDenied("process execution requires a trusted Host sandbox policy")
    runner = getattr(_RUNNER_LOCAL, "runner", None)
    if runner is None or runner.policy != policy:
        runner = _RUNNER_LOCAL.runner = SandboxRunner(policy)
    return runner.run(arguments, **kwargs)


_RUNNER_LOCAL = threading.local()


def cancel_all():
    for runner in list(RUNNERS):
        runner.cancel_all()


def safe_display(value):
    text = str(value)
    return "".join(char if (char.isprintable() or char in "\n\t") else f"\\x{ord(char):02x}" for char in text)

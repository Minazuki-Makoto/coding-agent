"""Typed checkpoints, append-only timeline and user-selected history branches."""
from __future__ import annotations

import contextvars
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path

from MCP_functions.System_Files.Files_function.write_in import _atomic_write


ACTIVE_RUN = contextvars.ContextVar("coding_agent_active_run", default=None)
SCHEMA_VERSION = 1


def encode(value):
    if is_dataclass(value):
        return {"$type": type(value).__name__, "fields": {
            field.name: encode(getattr(value, field.name)) for field in fields(value)}}
    if isinstance(value, dict):
        return {"$map": [[encode(key), encode(item)] for key, item in value.items()]}
    if isinstance(value, (set, tuple)):
        return {"$set" if isinstance(value, set) else "$tuple": [encode(item) for item in value]}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"checkpoint cannot serialize {type(value).__name__}")


def decode(value):
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$map" in value:
        return {decode(key): decode(item) for key, item in value["$map"]}
    if "$set" in value:
        return set(decode(item) for item in value["$set"])
    if "$tuple" in value:
        return tuple(decode(item) for item in value["$tuple"])
    if "$type" in value:
        from AgentLoop import agent
        from State import save_tool_result
        allowed = {name: getattr(module, name) for module in (agent, save_tool_result)
                   for name in dir(module) if isinstance(getattr(module, name), type)
                   and hasattr(getattr(module, name), "__dataclass_fields__")}
        cls = allowed.get(value["$type"])
        if cls is None:
            raise ValueError("unknown checkpoint dataclass")
        return cls(**{key: decode(item) for key, item in value["fields"].items()})
    return {key: decode(item) for key, item in value.items()}


def rows(path):
    path = Path(path)
    if not path.is_file():
        return []
    result = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"corrupt history {path.name}:{number}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"history record must be an object: {path.name}:{number}")
        result.append(record)
    return result


@contextmanager
def single_writer(session):
    path = Path(session) / ".writer.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0, 2)
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("session already has an active writer; close the other terminal") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class SessionStore:
    def __init__(self, session_address, chat_id="main"):
        self.path = Path(session_address).resolve()
        self.chat_id = str(chat_id)
        if not self.chat_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in self.chat_id):
            raise ValueError("chat identifier must contain only letters, digits, '_' or '-'")
        self.directory = self.path / "chats" / self.chat_id

    def initialize(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if not (self.directory / "branches.json").exists():
            self._write("branches.json", {"main": {"parent": None, "cutoff": None}})

    def _write(self, relative, value):
        _atomic_write(self.directory / relative, json.dumps(value, ensure_ascii=False, indent=2))

    def branches(self):
        return json.loads((self.directory / "branches.json").read_text(encoding="utf-8"))

    def validate_branch(self, branch):
        if branch not in self.branches():
            raise ValueError(f"unknown branch: {branch}")

    def lineage(self, branch):
        branches = self.branches()
        self.validate_branch(branch)
        limits = {branch: None}
        seen = {branch}
        while branches[branch]["parent"] is not None:
            parent = branches[branch]["parent"]
            if parent in seen or parent not in branches:
                raise ValueError("invalid branch lineage")
            inherited = limits[branch]
            cutoff = branches[branch]["cutoff"]
            limits[parent] = cutoff if inherited is None else min(inherited, cutoff)
            branch = parent
            seen.add(branch)
        return limits

    def visible(self, record, branch):
        if record.get("chat_id", self.chat_id) != self.chat_id:
            return False
        limits = self.lineage(branch)
        owner = record.get("branch_id", "main")
        if owner not in limits:
            return False
        cutoff = limits[owner]
        from State.history_recovery import indexed_identity
        identity = indexed_identity(self, record)
        seq = identity.get("event_seq")
        return cutoff is None or (type(seq) is int and seq <= cutoff)

    def history(self, filename, branch="main", all_branches=False):
        self.validate_branch(branch)
        from State.history_recovery import indexed_rows, legacy_events
        records = indexed_rows(self, filename)
        if filename == "timeline.jsonl":
            records.extend(legacy_events(self))
            records.sort(key=lambda row: row.get("event_seq", 0))
        return [row for row in records
                if row.get("chat_id", self.chat_id) == self.chat_id
                and (all_branches or self.visible(row, branch))]

    def latest_event_seq(self):
        from State.history_recovery import high_water
        return high_water(self)

    def open_history(self, branch="main"):
        """Create a fresh chat checkpoint from recorded context, never replay tools."""
        from State.history_recovery import ensure_index, rebuild_bundle
        with single_writer(self.path):
            if not self.directory.resolve().is_relative_to(self.path):
                raise ValueError("chat directory escapes session")
            self.initialize()
            self.validate_branch(branch)
            checkpoint = self.directory / "checkpoints" / f"{branch}.json"
            if checkpoint.is_file():
                return
            ensure_index(self)
            run = ActiveRun(self, branch)
            run.bundle = rebuild_bundle(self, branch)
            run.payload = {"recovery_mode": "history", "requires_new_request": True}
            run.checkpoint("ready")

    def load(self, branch="main"):
        self.validate_branch(branch)
        file = self.directory / "checkpoints" / f"{branch}.json"
        if not file.exists():
            raise ValueError("no precise checkpoint; legacy history is viewable, not resumable")
        document = json.loads(file.read_text(encoding="utf-8"))
        if document.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported checkpoint schema")
        if document.get("session_address") != str(self.path) or document.get("chat_id") != self.chat_id or document.get("branch_id") != branch:
            raise ValueError("checkpoint identity mismatch")
        bundle = decode(document["bundle"])
        for name in ("agent", "executor", "supervisor"):
            state = bundle[name]
            if state.session_address != str(self.path) or state.chat_id != self.chat_id:
                raise ValueError("state identity mismatch")
        agent = bundle["agent"]
        for role in ("executor", "supervisor"):
            maximum = max((row.get("seq", 0) for row in rows(self.path / f"{role}_history.jsonl")
                           if row.get("chat_id") == self.chat_id), default=0)
            setattr(agent, role + "_seq", max(getattr(agent, role + "_seq"), maximum))
        from State.save_tool_result import load_last_tool_result_seq, ToolResultLocator
        from AgentLoop.executor_loop import _tool_call_fingerprint
        agent.tool_result_seq = load_last_tool_result_seq(str(self.path), self.chat_id)
        # Recover committed results beyond a stale checkpoint without replaying calls.
        pending = []
        supervisor_pending = []
        for row in self.history("tool_results.jsonl", branch):
            if row.get("event_seq", 0) <= document.get("high_water", 0) or row.get("request_id") != agent.request_id:
                continue
            if row.get("actor") == "executor":
                fingerprint = _tool_call_fingerprint(row["task_id"], row["tool_name"], row["arguments"])
                if row["ok"]:
                    completed = agent.task_completed_tool_calls.setdefault(row["task_id"], [])
                    if fingerprint not in completed:
                        completed.append(fingerprint)
                pending.append(row)
            elif row.get("actor") == "supervisor":
                supervisor_pending.append(row)
        if pending:
            row = pending[-1]
            bundle["executor"].last_tool_result_ref = ToolResultLocator(str(self.path), self.chat_id, row["tool_result_seq"])
            bundle["executor"].tool_name = row["tool_name"]
            bundle["executor"].tool_arguments = row["arguments"]
            bundle["executor"].tool_ok = row["ok"]
            from AgentLoop.executor_loop import _build_latest_tool_context
            from MCP_functions.tool_registry import ToolExecutionResult
            bundle["executor"].latest_tool_context = _build_latest_tool_context(
                row["tool_name"], ToolExecutionResult(**row["content"]), row["tool_result_seq"])
            reference = bundle["executor"].last_tool_result_ref
            if reference not in bundle["executor"].tool_result_refs:
                bundle["executor"].tool_result_refs.append(reference)
            agent.executor_seq = max(agent.executor_seq, row.get("actor_turn_seq") or 0)
            document["phase"] = "executor_tool_summary"
            document["payload"] = {**document.get("payload", {}), "tool_result_seq": row["tool_result_seq"],
                                   "arguments": row["arguments"], "tool_name": row["tool_name"]}
        if supervisor_pending and (not pending or supervisor_pending[-1]["event_seq"] > pending[-1]["event_seq"]):
            row = supervisor_pending[-1]
            reference = ToolResultLocator(str(self.path), self.chat_id, row["tool_result_seq"])
            bundle["supervisor"].last_tool_result_ref = reference
            bundle["supervisor"].tool_name = row["tool_name"]
            bundle["supervisor"].tool_arguments = row["arguments"]
            bundle["supervisor"].tool_ok = row["ok"]
            if reference not in bundle["supervisor"].tool_result_refs:
                bundle["supervisor"].tool_result_refs.append(reference)
            descriptor = {"tool_result_seq": row["tool_result_seq"], "tool_name": row["tool_name"], "arguments": row["arguments"]}
            if row["phase"] == "evaluation":
                document["phase"] = "evaluation"
                payload = document.setdefault("payload", {})
                payload.setdefault("queries", []).append(descriptor)
                payload["rounds"] = payload.get("rounds", 0) + 1
            elif row["phase"] in {"planning", "decision"}:
                document["phase"] = row["phase"] + "_tool_summary"
                document["payload"] = descriptor
        visible = self.history("timeline.jsonl", branch)
        intents = {row["operation_id"] for row in visible if row.get("kind") == "tool_intent"}
        ended = {row.get("operation_id") for row in visible if row.get("kind") == "tool_completed"
                 and row.get("status") not in {"unknown", "interrupted"}}
        ended.update(row.get("operation_id") for row in self.history("tool_results.jsonl", branch)
                     if row.get("error_type") not in {"unknown", "interrupted"})
        document["unknown_operations"] = sorted(intents - ended)
        document["bundle"] = bundle
        return document

    def fork(self, event_seq, branch="main", allow_history=False):
        with single_writer(self.path):
            matches = [row for row in self.history("timeline.jsonl", branch)
                       if row.get("event_seq") == event_seq]
            if len(matches) != 1:
                raise ValueError("event not found in the current branch")
            if not matches[0].get("before_snapshot") and not allow_history:
                raise ValueError("event has no verified pre-state; cannot precisely rewind")
            event = matches[0]
            snapshot = self.directory / event["before_snapshot"] if event.get("before_snapshot") else None
            if snapshot and snapshot.is_file():
                document = json.loads(snapshot.read_text(encoding="utf-8"))
            elif not allow_history:
                raise ValueError("event snapshot is unavailable")
            else:
                document = None
            new_branch = "b_" + uuid.uuid4().hex[:12]
            branches = self.branches()
            branches[new_branch] = {"parent": branch, "cutoff": event_seq - 1}
            self._write("branches.json", branches)
            if document is None:
                from State.history_recovery import rebuild_bundle
                run = ActiveRun(self, new_branch)
                run.bundle = rebuild_bundle(self, new_branch, replay=event)
                run.payload = {"recovery_mode": "history", "rewind_event_seq": event_seq,
                               "inferred_order": event.get("inferred_order", False)}
                run.checkpoint("rewound" if run.bundle["agent"].user_query else "ready")
                return new_branch
            document["branch_id"] = new_branch
            bundle = decode(document["bundle"])
            bundle["agent"].branch_id = new_branch
            bundle["agent"].compression_cache = {}
            document["bundle"] = encode(bundle)
            self._write(f"checkpoints/{new_branch}.json", document)
            return new_branch


class ActiveRun:
    def __init__(self, store, branch="main", callback=None):
        self.store, self.branch, self.callback = store, branch, callback
        self.store.validate_branch(branch)
        self.bundle = None
        self.phase = "planning"
        self.payload = {}
        self.resume = False
        self.current_snapshot = None
        self.current_operation = None
        self.event_seq = store.latest_event_seq()

    def checkpoint(self, status="running"):
        if self.bundle is None:
            return
        document = {"schema_version": SCHEMA_VERSION, "session_address": str(self.store.path),
                    "chat_id": self.store.chat_id, "branch_id": self.branch,
                    "phase": self.phase, "payload": self.payload, "status": status,
                    "high_water": self.event_seq, "updated_at": datetime.now().isoformat(),
                    "bundle": encode(self.bundle)}
        self.store._write(f"checkpoints/{self.branch}.json", document)
        return document

    def emit(self, kind, actor="host", **data):
        self.event_seq += 1
        agent = self.bundle["agent"] if self.bundle else None
        event = {"schema_version": SCHEMA_VERSION, "event_seq": self.event_seq,
                 "event_id": uuid.uuid4().hex, "chat_id": self.store.chat_id,
                 "branch_id": self.branch, "request_id": agent.request_id if agent else "",
                 "task_id": agent.now_task_id if agent else None,
                 "target": agent.now_target if agent else "", "task_attempt": agent.task_attempt if agent else 0,
                 "actor": actor, "phase": self.phase, "kind": kind,
                 "created_at": datetime.now().isoformat(), **data}
        if self.current_snapshot:
            event["before_snapshot"] = self.current_snapshot
        with (self.store.path / "timeline.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
            file.flush()
            os.fsync(file.fileno())
        if self.callback:
            self.callback(event)
        return event

    def boundary(self, phase, actor="host", **payload):
        self.phase, self.payload = phase, payload
        document = self.checkpoint()
        if document:
            self.current_snapshot = f"snapshots/{self.branch}_{self.event_seq + 1}.json"
            self.store._write(self.current_snapshot, document)
        return self.emit("boundary", actor)


def active_run():
    return ACTIVE_RUN.get()


def boundary(phase, actor="host", **payload):
    run = active_run()
    if run:
        return run.boundary(phase, actor, **payload)


def annotate_record(record, filename):
    run = active_run()
    if run:
        event = run.emit("record", record.get("actor", "supervisor" if "supervisor" in filename else "executor"),
                         file=filename, seq=record.get("seq"), tool_result_seq=record.get("tool_result_seq"),
                         record_type=record.get("record_type"), tool_name=record.get("tool_name"),
                         status=("success" if record.get("tool_ok") is True else "failed" if record.get("tool_ok") is False else None))
        record.update({key: event[key] for key in ("event_seq", "branch_id", "request_id")})
    return record


def record_visible(record):
    run = active_run()
    return not run or run.store.visible(record, run.branch)

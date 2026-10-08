"""Continue old chats and fork their recorded history without inventing execution snapshots.

The sidecar contains only source references/hashes and navigation metadata. Old JSONL
and raw tool bodies are never rewritten or copied. Unsequenced legacy navigation is
an inferred order, not proof of the original execution order.
"""
import hashlib
import json
import uuid
from dataclasses import fields

from State.session_checkpoint import rows

FILES = ("executor_history.jsonl", "supervisor_history.jsonl", "tool_results.jsonl", "chat_history.jsonl")


def fingerprint(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def index_entries(store):
    path = store.directory / "history_index.json"
    if not path.is_file():
        return []
    if not path.resolve().is_relative_to(store.path):
        raise ValueError("history index escapes session")
    version = path.stat().st_mtime_ns
    cached = getattr(store, "_history_index", None)
    if cached and cached[0] == version:
        return cached[1]
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or document.get("chat_id") != store.chat_id:
        raise ValueError("invalid legacy history index")
    entries = document["entries"]
    if any(e["file"] not in FILES for e in entries):
        raise ValueError("invalid history source")
    store._history_index = (version, entries)
    return entries


def high_water(store):
    """Session-wide event numbering includes all chats' reserved legacy indexes."""
    maximum = max((r.get("event_seq", 0) for r in rows(store.path / "timeline.jsonl")), default=0)
    for path in store.path.glob("chats/*/history_index.json"):
        if not path.resolve().is_relative_to(store.path):
            raise ValueError("history index escapes session")
        document = json.loads(path.read_text(encoding="utf-8"))
        maximum = max(maximum, max((e["event_seq"] for e in document["entries"]), default=0))
    return maximum


def ensure_index(store):
    """Caller holds session writer lock. Publish once; no edits to old records."""
    if (store.directory / "history_index.json").exists():
        return
    known = {r.get("event_seq") for r in rows(store.path / "timeline.jsonl")}
    sources = {name: rows(store.path / name) for name in FILES}
    seq = max(high_water(store), max((r.get("event_seq", 0) for records in sources.values()
                                   for r in records), default=0))
    entries = []
    for name, records in sources.items():
        for position, record in enumerate(records):
            if str(record.get("chat_id", "main")) != store.chat_id:
                continue
            original_seq = record.get("event_seq")
            if type(original_seq) is int and original_seq in known:
                continue
            event_seq = original_seq
            actor = "supervisor" if name.startswith("supervisor") else "executor" if name.startswith("executor") else record.get("actor", "host")
            entries.append({"file": name, "position": position, "hash": fingerprint(record),
                            "event_seq": event_seq, "branch_id": record.get("branch_id", "main"),
                            "chat_id": store.chat_id, "task_id": record.get("task_id"), "actor": actor,
                            "phase": "history_" + record.get("record_type", "record"),
                            "target": record.get("target", record.get("now_target", "")),
                            "seq": record.get("seq"), "tool_result_seq": record.get("tool_result_seq"),
                            "request_id": record.get("request_id", "legacy"),
                            "inferred_order": type(original_seq) is not int})
    maximum_task = max((e["task_id"] for e in entries if type(e["task_id"]) is int), default=0)
    def order(entry):
        phase = entry["phase"]
        rank = (0 if "planning" in phase else 1 if "guidance" in phase else
                4 if "evaluation" in phase else 5 if "decision" in phase else
                6 if "final_summary" in phase else 3 if "executor_turn" in phase else 2)
        if entry["file"] == "chat_history.jsonl":
            rank = 7
        task = entry["task_id"] if type(entry["task_id"]) is int else maximum_task
        return (task, rank, entry["seq"] if type(entry["seq"]) is int else entry["position"], entry["file"])
    for entry in sorted(entries, key=order):
        if entry["inferred_order"]:
            seq += 1
            entry["event_seq"] = seq
    store._write("history_index.json", {"schema_version": 1, "chat_id": store.chat_id, "entries": entries})


def indexed_rows(store, filename):
    records = rows(store.path / filename)
    entries = {e["position"]: e for e in index_entries(store) if e["file"] == filename}
    for position, entry in entries.items():
        if position >= len(records) or fingerprint(records[position]) != entry["hash"]:
            raise ValueError("indexed historical record changed; refusing unsafe recovery")
        records[position] = {**records[position], "event_seq": entry["event_seq"],
                             "branch_id": entry["branch_id"], "chat_id": store.chat_id}
    return records


def legacy_events(store):
    records = []
    sources = {}
    for entry in index_entries(store):
        filename = entry["file"]
        if filename not in sources:
            sources[filename] = indexed_rows(store, filename)
        original = sources[filename][entry["position"]]
        event = {**entry, "schema_version": 1, "event_id": "history_" + entry["hash"],
                 "kind": "legacy_chat" if filename == "chat_history.jsonl" else "boundary",
                 "record_type": original.get("record_type", "legacy_record"), "recovery_mode": "history"}
        if filename == "chat_history.jsonl":
            event.update(query_content=original.get("query_content", ""),
                         answer_content=original.get("answer_content", ""))
        records.append(event)
    return records


def indexed_identity(store, record):
    if type(record.get("event_seq")) is int:
        return record
    entries = index_entries(store)
    if "_legacy_supervisor_seq" in record:
        matches = [e for e in entries if e["file"] == "supervisor_history.jsonl"
                   and e["seq"] == record["_legacy_supervisor_seq"] and e["task_id"] == record.get("task_id")]
    else:
        digest = fingerprint(record)
        matches = [e for e in entries if e["hash"] == digest]
    if not matches:
        return record
    # Without a source location, identical legacy records are ambiguous. Latest
    # matching identity conservatively hides all if any could be after the cutoff.
    entry = max(matches, key=lambda e: e["event_seq"])
    return {**record, "chat_id": store.chat_id, "branch_id": entry["branch_id"], "event_seq": entry["event_seq"]}


def conversation_context(store, branch):
    """Old request logs + modern timeline, deduplicated by request/event identity."""
    events = store.history("timeline.jsonl", branch)
    modern_roles = {}
    for event in events:
        if event.get("kind") in {"user", "answer"} and event.get("request_id"):
            modern_roles.setdefault(event["request_id"], set()).add("user" if event["kind"] == "user" else "assistant")
    context = []
    def add_archived(record):
        for role, field in (("user", "query_content"), ("assistant", "answer_content")):
            if not record.get(field) or role in modern_roles.get(record.get("request_id"), set()):
                continue
            item = {"role": role, "request_id": record.get("request_id"), "content": record[field]}
            if role == "user" and record.get("request_id"):
                at = next((i for i, r in enumerate(context) if r.get("request_id") == record["request_id"]), len(context))
                context.insert(at, item)
            else:
                context.append(item)
    for event in events:
        kind = event.get("kind")
        if kind == "legacy_chat":
            add_archived(event)
        elif kind in {"user", "answer", "application", "application_declined"}:
            context.append({"role": "user" if kind == "user" else "assistant",
                            "request_id": event.get("request_id"), "content": event.get("text", event.get("answer", ""))})
    # Sequenced old chat records may lack matching timeline user/answer events.
    for record in store.history("chat_history.jsonl", branch):
        if any(e.get("kind") == "legacy_chat" and e["event_seq"] == record.get("event_seq") for e in events):
            continue
        add_archived(record)
    return context


def rebuild_bundle(store, branch, replay=None):
    """Rebuild known tasks, confirmed outcomes and dialogue, not execution state."""
    from AgentLoop.agent import AgentState, SupervisorState, ExecutorState, SupervisorEvaluationState, TaskOutcome, EvidenceReference
    from State.save_tool_result import load_last_tool_result_seq
    agent = AgentState(str(store.path), store.chat_id, branch_id=branch)
    for actor in ("executor", "supervisor"):
        for row in store.history(actor + "_history.jsonl", branch):
            if type(row.get("seq")) is int:
                setattr(agent, actor + "_seq", max(getattr(agent, actor + "_seq"), row["seq"]))
            task_id = row.get("task_id")
            if type(task_id) is not int or task_id < 0:
                continue
            while len(agent.task_list) <= task_id:
                agent.task_list.append(f"历史任务 {len(agent.task_list)}（目标未记录）")
            if row.get("target") or row.get("now_target"):
                agent.task_list[task_id] = row.get("target") or row["now_target"]
            description = row.get("description") or row.get("output_content")
            if isinstance(description, str):
                agent.task_previous_work.setdefault(task_id, []).append(description)
            outcome = row.get("task_outcome")
            if actor == "supervisor" and isinstance(outcome, dict) and row.get("is_next_target") is True and row.get("is_executor_passed") is True:
                if outcome.get("task_id") != task_id:
                    continue
                if not all(isinstance(outcome.get(k), str) and outcome[k].strip()
                           for k in ("accepted_summary", "verification_summary")):
                    continue
                references = [EvidenceReference(**{k: v for k, v in ref.items()
                    if k in {f.name for f in fields(EvidenceReference)}})
                    for ref in outcome.get("evidence_references", []) if isinstance(ref, dict)
                    and type(ref.get("tool_result_seq")) is int and isinstance(ref.get("tool_name"), str)]
                agent.task_outcomes[task_id] = TaskOutcome(task_id, agent.task_list[task_id],
                    outcome["accepted_summary"], outcome["verification_summary"], references)
            elif actor == "supervisor" and row.get("is_executor_passed") is False:
                agent.task_outcomes.pop(task_id, None)
    agent.tool_result_seq = load_last_tool_result_seq(str(store.path), store.chat_id)
    agent.conversation_context = conversation_context(store, branch)
    agent.now_task_id = agent.request_start_task_id = len(agent.task_list)
    if replay:
        task_id = replay.get("task_id")
        agent.user_query = replay.get("query_content") or replay.get("text") or replay.get("target")
        if not agent.user_query and agent.conversation_context:
            agent.user_query = next((r["content"] for r in reversed(agent.conversation_context) if r["role"] == "user"), "")
        if type(task_id) is int and task_id >= 0:
            agent.request_start_task_id = agent.now_task_id = task_id
            agent.task_list = agent.task_list[:task_id]
            agent.task_outcomes = {k: v for k, v in agent.task_outcomes.items() if k < task_id}
        agent.request_id = "recovered_" + uuid.uuid4().hex
    return {"agent": agent, "supervisor": SupervisorState(session_address=str(store.path), chat_id=store.chat_id),
            "executor": ExecutorState(session_address=str(store.path), chat_id=store.chat_id),
            "evaluation": SupervisorEvaluationState(chat_id=store.chat_id)}

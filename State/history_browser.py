"""Read-only terminal browsing; never restore state or expand model memory."""
from pathlib import Path

from State.session_checkpoint import SessionStore, rows


def list_chats(session_address):
    session = Path(session_address).resolve()
    ids = set()
    chats = session / "chats"
    if chats.is_dir():
        ids.update(p.name for p in chats.iterdir()
                   if p.is_dir() and p.resolve().is_relative_to(session))
    for filename in ("timeline.jsonl", "chat_history.jsonl", "executor_history.jsonl",
                     "supervisor_history.jsonl", "tool_results.jsonl"):
        ids.update(str(record.get("chat_id", "main")) for record in rows(session / filename))
    if not ids:
        ids.add("main")
    result = []
    for chat in sorted(ids):
        try:
            store = SessionStore(session, chat)
        except ValueError:
            continue
        if not store.directory.resolve().is_relative_to(session):
            continue
        branches = store.branches() if (store.directory / "branches.json").is_file() else {}
        result.append({"chat_id": chat, "branches": list(branches), "legacy": not branches})
    return result


def conversation_history(store, branch="main"):
    """Use branch-aware timeline first; old request logs are view-only fallback."""
    if not store.directory.resolve().is_relative_to(store.path):
        raise ValueError("chat directory escapes session")
    modern = (store.directory / "branches.json").is_file()
    if modern:
        timeline = store.history("timeline.jsonl", branch)
        modern_ids = {r.get("request_id") for r in timeline if r.get("kind") == "user" and r.get("request_id")}
        history = [r for r in timeline if r.get("kind") in {"user", "answer", "interrupted", "legacy_chat"}
                   and not (r.get("kind") == "legacy_chat" and r.get("request_id") in modern_ids)]
        if history:
            return history
        return store.history("chat_history.jsonl", branch)
    if branch != "main":
        raise ValueError("legacy history has no reliable branch metadata; only main is viewable")
    def selected(filename):
        return [r for r in rows(store.path / filename)
                if str(r.get("chat_id", "main")) == store.chat_id
                and r.get("branch_id", "main") == "main"]
    timeline = [r for r in selected("timeline.jsonl")
                if r.get("kind") in {"user", "answer", "interrupted"}]
    return timeline or selected("chat_history.jsonl")


def format_conversation(records):
    sections = []
    for r in records:
        header = f"request={r.get('request_id', 'legacy')} event={r.get('event_seq', '-')}"
        kind = r.get("kind")
        if kind == "user":
            text = "用户：\n" + str(r.get("text", ""))
        elif kind == "answer":
            text = "智能体：\n" + str(r.get("answer", ""))
        elif kind == "interrupted":
            text = "请求已中断：" + str(r.get("reason", r.get("message", "")))
        else:
            text = ("用户：\n" + str(r.get("query_content", ""))
                    + "\n\n智能体：\n" + str(r.get("answer_content", "")))
        sections.append(header + "\n" + text)
    return "\n\n".join(sections)


def branch_choices(state_dir, session_address=None):
    """Each choice carries real IDs; read-only legacy entries never claim resume."""
    root = Path(state_dir).resolve()
    sessions = [Path(session_address)] if session_address else sorted(root.iterdir())
    choices = []
    for session in sessions:
        if not session.is_dir() or not session.resolve().is_relative_to(root):
            continue
        chats = list_chats(session)
        for chat in chats:
            store = SessionStore(session, chat["chat_id"])
            for branch in chat["branches"] or ["main"]:
                checkpoint = store.directory / "checkpoints" / (branch + ".json")
                available = checkpoint.is_file() and checkpoint.resolve().is_relative_to(session.resolve())
                status, updated = "可续聊（无执行快照）", ""
                if available:
                    import json
                    document = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
                    status, updated = document.get("status", "unknown"), document.get("updated_at", "")
                choices.append({"session": session.name, "chat": chat["chat_id"], "branch": branch,
                                "resumable": available, "status": status, "updated_at": updated})
    return choices


def description_history(store, branch="main"):
    if not store.directory.resolve().is_relative_to(store.path):
        raise ValueError("chat directory escapes session")
    modern = (store.directory / "branches.json").is_file()
    if not modern and branch != "main":
        raise ValueError("legacy history has no reliable branch metadata")
    records = []
    for actor in ("executor", "supervisor"):
        filename = actor + "_history.jsonl"
        selected = store.history(filename, branch) if modern else [
            r for r in rows(store.path / filename)
            if str(r.get("chat_id", "main")) == store.chat_id and r.get("branch_id", "main") == "main"]
        for row in selected:
            kind = row.get("record_type", "")
            if kind.endswith("tool_event") or kind == "supervisor_review":
                continue
            text = (row.get("output_content") or row.get("description")) if actor == "executor" else row.get("description")
            if isinstance(text, str) and text.strip():
                records.append({**row, "actor": actor, "description": text})
    # Modern events have a shared chronological index. Legacy role seqs aren't comparable.
    return sorted(records, key=lambda r: r.get("event_seq", 0))

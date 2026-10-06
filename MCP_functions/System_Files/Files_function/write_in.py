import os
import tempfile
from pathlib import Path


WRITABLE_ROOTS_ENV = "CODING_AGENT_WRITABLE_ROOTS"


def _trusted_roots(override=None):
    if override is not None:
        return [Path(item).resolve() for item in override]
    raw_roots = os.environ.get(WRITABLE_ROOTS_ENV, "")
    return [
        Path(item).resolve()
        for item in raw_roots.split(os.pathsep)
        if item.strip()
    ]


def _resolve_allowed_path(file_address: str, trusted_roots=None):
    if not isinstance(file_address, str) or not file_address.strip():
        raise ValueError("file_address must be a non-empty string")
    if trusted_roots is None:
        from MCP_functions.sandbox import current_policy
        policy = current_policy()
        if policy is not None:
            return policy.check_path(file_address, write=True)
    roots = _trusted_roots(trusted_roots)
    if not roots:
        raise PermissionError("no trusted writable root is configured by the host")
    target = Path(file_address).resolve(strict=False)
    if not any(target == root or target.is_relative_to(root) for root in roots):
        raise PermissionError("file_address is outside the trusted writable roots")
    return target


def _atomic_write(target: Path, content: str):
    target.parent.mkdir(parents=True, exist_ok=True)
    created = not target.exists()
    previous = target.read_text(encoding="utf-8") if target.exists() else None
    changed = previous != content
    if not changed:
        return created, False, len(content.encode("utf-8"))
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temp_name = temporary_file.name
        os.replace(temp_name, target)
    finally:
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink()
    return created, True, len(content.encode("utf-8"))


def write_in(file_address: str, code: str, *, trusted_roots=None):
    try:
        if not isinstance(code, str):
            raise ValueError("code must be a string containing the complete file")
        target = _resolve_allowed_path(file_address, trusted_roots)
        created, changed, bytes_written = _atomic_write(target, code)
        return {
            "status": "success", "operation": "overwrite",
            "file_address": str(target), "created": created,
            "changed": changed, "bytes_written": bytes_written,
        }
    except (OSError, ValueError, PermissionError) as exc:
        return {
            "status": "error", "operation": "overwrite",
            "file_address": str(file_address), "created": False,
            "changed": False, "bytes_written": 0, "message": str(exc),
        }


def str_replace(
    file_address: str,
    old_text: str,
    new_text: str,
    expected_replacements: int = 1,
):
    try:
        if not isinstance(old_text, str) or not old_text:
            raise ValueError("old_text must be a non-empty string")
        if not isinstance(new_text, str):
            raise ValueError("new_text must be a string")
        if type(expected_replacements) is not int or expected_replacements < 1:
            raise ValueError("expected_replacements must be a positive integer")
        target = _resolve_allowed_path(file_address)
        if not target.is_file():
            raise FileNotFoundError("target file does not exist")
        content = target.read_text(encoding="utf-8")
        matches = content.count(old_text)
        if matches != expected_replacements:
            raise ValueError(
                f"expected {expected_replacements} replacements, found {matches}"
            )
        updated = content.replace(old_text, new_text)
        _, changed, bytes_written = _atomic_write(target, updated)
        if target.read_text(encoding="utf-8") != updated:
            raise OSError("replacement verification failed")
        return {
            "status": "success", "operation": "str_replace",
            "file_address": str(target), "created": False,
            "changed": changed, "replacements": matches,
            "bytes_written": bytes_written,
        }
    except (OSError, ValueError, PermissionError) as exc:
        return {
            "status": "error", "operation": "str_replace",
            "file_address": str(file_address), "created": False,
            "changed": False, "replacements": 0,
            "bytes_written": 0, "message": str(exc),
        }


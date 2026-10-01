from pathlib import Path


IGNORED_FILE_NAMES = {
    ".dockerignore", ".gitattributes", ".gitignore", ".env", "mvnw", "mvnw.cmd",
}
IGNORED_FILE_PREFIXES = (".env.",)
IGNORED_SUFFIXES = {
    ".class", ".jar", ".pyc", ".pyo", ".exe", ".dll", ".so", ".dylib",
    ".png", ".jpg", ".jpeg", ".json", ".gif", ".ico", ".pdf", ".zip",
    ".gz", ".7z",
}
IGNORED_FOLDER_NAMES = {
    ".git", ".gradle", ".idea", ".pytest_cache", ".venv", ".vscode",
    "__pycache__", "build", "dist", "node_modules", "out", "target", "venv",
}
MAX_FILE_BYTES = 1_000_000
MAX_FILE_CONTENT_CHARS = 8_000
MAX_TOTAL_CONTENT_CHARS = 12_000
MAX_DIRECTORY_ENTRIES = 200
MAX_CONTENT_DIRECTORY_FILES = 50


def _validate_page(start_index, max_entries, maximum):
    if not isinstance(start_index, int) or isinstance(start_index, bool) or start_index < 0:
        return "start_index must be a non-negative integer"
    if not isinstance(max_entries, int) or isinstance(max_entries, bool):
        return "max_entries must be an integer"
    if not 1 <= max_entries <= maximum:
        return f"max_entries must be between 1 and {maximum}"
    return None


def _unsafe_content_reason(path: Path):
    name_lower = path.name.lower()
    if name_lower in IGNORED_FILE_NAMES:
        return "ignored_file_name"
    if name_lower.startswith(IGNORED_FILE_PREFIXES):
        return "sensitive_environment_file"
    if path.suffix.lower() in IGNORED_SUFFIXES:
        return "ignored_or_binary_suffix"
    return None


def _file_metadata(path: Path):
    return {
        "file_name": path.name,
        "address": str(path.resolve()),
        "suffix": path.suffix.lower(),
    }


def _directory_metadata(path: Path):
    return {
        "folder_name": path.name,
        "address": str(path.resolve()),
    }


def _read_text_preview(path: Path, remaining_chars: int):
    if remaining_chars <= 0:
        return None, "total_content_budget_exhausted", 0
    allowed_chars = min(MAX_FILE_CONTENT_CHARS, remaining_chars)
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            content = stream.read(allowed_chars + 1)
    except UnicodeDecodeError:
        return None, "not_utf8_text", 0
    except OSError as exc:
        return None, f"read_failed:{type(exc).__name__}", 0
    if len(content) > allowed_chars:
        return content[:allowed_chars], "content_truncated", allowed_chars
    return content, "content_read", len(content)


def _read_text_page(path: Path, start_char: int, max_chars: int):
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            remaining = start_char
            while remaining:
                skipped = stream.read(min(remaining, 8192))
                if not skipped:
                    break
                remaining -= len(skipped)
            if remaining:
                return "", False, start_char - remaining
            content = stream.read(max_chars + 1)
    except UnicodeDecodeError:
        raise ValueError("file is not UTF-8 text")
    has_more = len(content) > max_chars
    content = content[:max_chars]
    return content, has_more, start_char + len(content)


def read_all_files(home_address: str, start_index: int = 0, max_entries: int = 200):
    """List one directory level without reading file contents."""
    if not isinstance(home_address, str) or not home_address.strip():
        return {"status": "error", "message": "home_address must be a non-empty string"}
    page_error = _validate_page(start_index, max_entries, MAX_DIRECTORY_ENTRIES)
    if page_error:
        return {"status": "error", "message": page_error}

    path = Path(home_address).expanduser()
    if not path.exists():
        return {"status": "error", "message": "please provide a valid home address"}
    if path.is_file():
        metadata = _file_metadata(path)
        return {
            "status": "success", "root": str(path.resolve()),
            "directories": [], "files": [metadata],
            "summary": {
                "scope": "single_file_metadata", "total_entries": 1,
                "returned_entries": 1, "has_more": False, "next_start_index": None,
            },
        }
    if not path.is_dir():
        return {"status": "error", "message": "home_address is not a file or directory"}

    try:
        children = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except OSError as exc:
        return {"status": "error", "message": f"cannot list directory: {type(exc).__name__}"}

    page = children[start_index:start_index + max_entries]
    directories, files = [], []
    for item in page:
        try:
            if item.is_dir():
                record = _directory_metadata(item)
                directories.append(record)
            elif item.is_file():
                record = _file_metadata(item)
                files.append(record)
            else:
                continue
        except OSError:
            continue

    next_index = start_index + len(page)
    has_more = next_index < len(children)
    return {
        "status": "success", "root": str(path.resolve()),
        "directories": directories, "files": files,
        "summary": {
            "scope": "one_directory_level", "total_entries": len(children),
            "returned_entries": len(directories) + len(files),
            "start_index": start_index,
            "has_more": has_more,
            "next_start_index": next_index if has_more else None,
        },
    }


def read_files_content(
        home_address: str,
        start_char: int = 0,
        max_chars: int = MAX_TOTAL_CONTENT_CHARS,
        start_index: int = 0,
        max_entries: int = 20,
):
    """Read a targeted file page or direct files in one directory."""
    if not isinstance(home_address, str) or not home_address.strip():
        return {"status": "error", "message": "home_address must be a non-empty string"}
    if not isinstance(start_char, int) or isinstance(start_char, bool) or start_char < 0:
        return {"status": "error", "message": "start_char must be a non-negative integer"}
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 1 <= max_chars <= 20_000:
        return {"status": "error", "message": "max_chars must be between 1 and 20000"}
    page_error = _validate_page(start_index, max_entries, MAX_CONTENT_DIRECTORY_FILES)
    if page_error:
        return {"status": "error", "message": page_error}

    path = Path(home_address).expanduser()
    if not path.exists():
        return {"status": "error", "message": "please provide a valid home address"}
    if path.is_file():
        reason = _unsafe_content_reason(path)
        if reason:
            return {
                "status": "error", "message": f"file content cannot be read: {reason}",
                "address": str(path.resolve()),
            }
        try:
            content, has_more, end_char = _read_text_page(path, start_char, max_chars)
        except (OSError, ValueError) as exc:
            return {"status": "error", "message": str(exc), "address": str(path.resolve())}
        return {
            "status": "success", "root": str(path.resolve()), "scope": "single_file_content",
            "file": {
                **_file_metadata(path), "content": content,
                "content_status": "content_page" if has_more else "content_complete",
                "start_char": start_char, "end_char": end_char,
                "content_chars": len(content), "has_more": has_more,
                "next_start_char": end_char if has_more else None,
            },
        }
    if not path.is_dir():
        return {"status": "error", "message": "home_address is not a file or directory"}

    try:
        items = list(path.iterdir())
        directories = sorted(
            (_directory_metadata(item) for item in items if item.is_dir()),
            key=lambda item: item["folder_name"].lower(),
        )
        child_files = sorted(
            (item for item in items if item.is_file()), key=lambda item: item.name.lower(),
        )
    except OSError as exc:
        return {"status": "error", "message": f"cannot list directory: {type(exc).__name__}"}

    page = child_files[start_index:start_index + max_entries]
    records, total_chars = [], 0
    content_budget = min(max_chars, MAX_TOTAL_CONTENT_CHARS)
    for item in page:
        metadata = _file_metadata(item)
        reason = _unsafe_content_reason(item)
        if reason:
            records.append({
                **metadata, "content": None, "content_status": reason, "content_chars": 0,
            })
            continue
        content, content_status, content_chars = _read_text_preview(
            item, content_budget - total_chars
        )
        total_chars += content_chars
        records.append({
            **metadata, "content": content, "content_status": content_status,
            "content_chars": content_chars,
        })

    next_index = start_index + len(page)
    has_more = next_index < len(child_files)
    return {
        "status": "success", "root": str(path.resolve()),
        "scope": "direct_directory_file_contents", "directories": directories,
        "files": records,
        "summary": {
            "total_directories": len(directories),
            "total_files": len(child_files), "returned_files": len(records),
            "content_chars": total_chars, "start_index": start_index,
            "has_more": has_more,
            "next_start_index": next_index if has_more else None,
        },
    }


def sort_files_by_suffix(files: dict):
    if not isinstance(files, dict) or files.get("status") != "success":
        return {"status": "error", "message": "I can't get any information from the address you provide "}
    sorted_files = {}
    try:
        for file in files.get("files") or []:
            suffix = file.get("suffix")
            if suffix is None:
                raise Exception(f"{file.get('file_name')} has no suffix")
            sorted_files.setdefault(suffix, []).append(file)
        return {"status": "success", "sorted": sorted_files}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

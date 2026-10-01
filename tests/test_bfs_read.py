import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from MCP_functions.System_Files.Files_function.bfs_read import (
    read_all_files,
    read_files_content,
    sort_files_by_suffix,
)


class BfsReadTests(unittest.TestCase):
    def test_structure_listing_is_single_level_and_contains_no_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pom.xml").write_text("<project/>", encoding="utf-8")
            (root / "application.yaml").write_text("server:\n  port: 8080", encoding="utf-8")
            (root / "config.json").write_text('{"secret": "x"}', encoding="utf-8")
            (root / ".env").write_text("TOKEN=x", encoding="utf-8")
            (root / ".idea").mkdir()
            (root / ".idea" / "workspace.xml").write_text("ignored", encoding="utf-8")
            (root / "target").mkdir()
            (root / "target" / "Main.class").write_bytes(b"binary")
            source = root / "src" / "main" / "java"
            source.mkdir(parents=True)
            (source / "Main.java").write_text("class Main {}", encoding="utf-8")

            result = read_all_files(str(root))

            self.assertEqual(result["status"], "success")
            names = {item["file_name"] for item in result["files"]}
            self.assertEqual(names, {".env", "application.yaml", "config.json", "pom.xml"})
            folders = {item["folder_name"] for item in result["directories"]}
            self.assertEqual(folders, {".idea", "src", "target"})
            self.assertTrue(all("content" not in item for item in result["files"]))
            self.assertNotIn("tree", result)
            self.assertEqual(
                set(result["files"][0]), {"file_name", "address", "suffix"}
            )
            self.assertEqual(
                set(result["directories"][0]), {"folder_name", "address"}
            )

    def test_directory_content_is_non_recursive_and_budgeted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("a" * 10, encoding="utf-8")
            (root / "b.txt").write_text("b" * 10, encoding="utf-8")
            with patch(
                "MCP_functions.System_Files.Files_function.bfs_read.MAX_TOTAL_CONTENT_CHARS",
                12,
            ):
                result = read_files_content(str(root))

            self.assertEqual(result["summary"]["content_chars"], 12)
            self.assertEqual(
                [item["content_status"] for item in result["files"]],
                ["content_read", "content_truncated"],
            )

    def test_single_file_content_can_continue_by_character_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Main.java"
            path.write_text("abcdefghij", encoding="utf-8")

            first = read_files_content(str(path), max_chars=4)
            second = read_files_content(
                str(path), start_char=first["file"]["next_start_char"], max_chars=4
            )

            self.assertEqual(first["file"]["content"], "abcd")
            self.assertTrue(first["file"]["has_more"])
            self.assertEqual(second["file"]["content"], "efgh")
            self.assertEqual(second["file"]["next_start_char"], 8)

    def test_grouping_uses_suffix(self):
        records = {
            "status": "success",
            "files": [
                {"file_name": "A.java", "suffix": ".java", "mother_file": "src/a"},
                {"file_name": "B.java", "suffix": ".java", "mother_file": "src/b"},
            ],
        }
        by_suffix = sort_files_by_suffix(records)

        self.assertEqual(len(by_suffix["sorted"][".java"]), 2)


if __name__ == "__main__":
    unittest.main()

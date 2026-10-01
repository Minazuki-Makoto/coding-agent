import asyncio
import tempfile
import unittest
from pathlib import Path

from MCP_functions.System_Files.Files_function.bfs_read import (
    MAX_FILE_BYTES,
    read_all_files,
    sort_files_by_suffix,
)
from MCP_functions.System_Files.Files_function.java_code import judge_spring_project
from MCP_functions.System_Files.Files_server.mcp_system_server import (
    judge_spring_project_tool,
    sort_files_by_suffix_tool,
)


class JudgeSpringProjectTests(unittest.TestCase):
    def test_complete_large_pom_is_parsed_instead_of_truncated_preview(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wrapper = root / ".mvn" / "wrapper"
            wrapper.mkdir(parents=True)
            (wrapper / "maven-wrapper.properties").write_text(
                "distributionUrl=https://repo.example/apache-maven-3.9.9-bin.zip\n",
                encoding="utf-8",
            )
            # Exceed both the per-file preview and generic large-file limits.
            # A POM must remain indexed so the specialized parser can read it.
            padding = "x" * (MAX_FILE_BYTES + 1)
            (root / "pom.xml").write_text(
                f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.5.1</version>
  </parent>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.2.3</version>
  <properties>
    <java.version>21</java.version>
    <padding>{padding}</padding>
  </properties>
  <prerequisites><maven>[3.9,)</maven></prerequisites>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
  </dependencies>
  <modules><module>child</module></modules>
  <profiles><profile><id>production</id></profile></profiles>
</project>
""",
                encoding="utf-8",
            )

            scanned = read_all_files(str(root))
            pom_record = next(
                item for item in scanned["files"] if item["file_name"] == "pom.xml"
            )
            self.assertNotIn("content", pom_record)

            result = judge_spring_project(sort_files_by_suffix(scanned))

            self.assertEqual(result["status"], "success")
            self.assertTrue(result["is_spring_boot_project"])
            self.assertEqual(result["jdk_version"], 21)
            project = result["maven_projects"][0]
            self.assertEqual(project["project"]["artifact_id"], "demo")
            self.assertEqual(project["maven"]["wrapper_version"], "3.9.9")
            self.assertEqual(project["maven"]["required_version"], "[3.9,)")
            self.assertEqual(project["dependency_count"], 1)
            self.assertEqual(
                project["declared_dependencies"][0]["artifact_id"],
                "spring-boot-starter-web",
            )
            self.assertEqual(project["modules"], ["child"])
            self.assertEqual(project["profiles"], ["production"])

            grouped = asyncio.run(sort_files_by_suffix_tool(str(root)))
            inspected = asyncio.run(judge_spring_project_tool(str(root)))
            self.assertIn(".xml", grouped["sorted"])
            self.assertTrue(inspected["is_spring_boot_project"])


if __name__ == "__main__":
    unittest.main()

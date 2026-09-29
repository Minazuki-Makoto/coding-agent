from ..Files_function.bfs_read import (
    read_all_files,
    read_files_content,
    sort_files_by_suffix,
)

from ..Files_function.java_code import (judge_spring_project,
                                        find_java_exe,
                                        run_java_get_feedback,
                                        package_spring_boot_with_confirmation,
                                        check_spring_boot_startup)

from ..Files_function.write_in import write_in

from ..Files_function.python_code import (find_the_python_editor,
                                          run_code_get_feedback,
                                          download_package_with_confirmation)


from mcp import StdioServerParameters
from mcp.server import FastMCP


mcp = FastMCP("system")

@mcp.tool(
    name="read_all_files_tool",
    description="""
Browse one level of a project path without reading any file content. Returns a
compact tree plus directories, files, and pagination summary. Each directory has
an address that can be passed back to this tool to drill down (for example,
project/src then project/src/main). Use start_index=summary.next_start_index when
has_more is true. max_entries must be 1..200. This tool is for structure discovery
only; use read_files_content_tool for source text. Do not repeatedly call the same
path and page after a successful result.
    """
)
async def read_all_files_tool(
        home_address: str,
        start_index: int = 0,
        max_entries: int = 200,
):
    return read_all_files(
        home_address=home_address,
        start_index=start_index,
        max_entries=max_entries,
    )


@mcp.tool(
    name="read_files_content_tool",
    description="""
Read targeted UTF-8 text after locating it with read_all_files_tool. For a file,
returns at most max_chars characters (1..20000) beginning at start_char; when
file.has_more is true, continue with file.next_start_char so no later portion is
lost. For a directory, reads only its direct files, never descendants, with a
12000-character total response budget; use start_index/next_start_index to page
through at most 50 direct files per call, or pass an exact file path for precise
reading. Sensitive environment files, JSON configured as excluded, and known
binary/archive formats are not returned. For large pom.xml prefer
judge_spring_project_tool, which fully parses it into structured Maven data.
    """
)
async def read_files_content_tool(
        home_address: str,
        start_char: int = 0,
        max_chars: int = 12000,
        start_index: int = 0,
        max_entries: int = 20,
):
    return read_files_content(
        home_address=home_address,
        start_char=start_char,
        max_chars=max_chars,
        start_index=start_index,
        max_entries=max_entries,
    )


@mcp.tool(
    name="sort_files_by_suffix_tool",
    description="""
Group file metadata records by extension, such as .py, .java, or .xml.
Pass the complete successful response from read_all_files_tool as files,
including status and files. Returns status and a sorted dictionary whose keys
are extensions and whose values are lists of the original file records.
    """
)
async def sort_files_by_suffix_tool(files:dict):
    return sort_files_by_suffix(files)


@mcp.tool(
    name="judge_spring_project_tool",
    description="""
Inspect Maven/Gradle build files and Java/Kotlin imports for Spring and
Spring Boot evidence. Pass the complete response from sort_files_by_suffix_tool,
including status and sorted. Returns Spring indicators, build_tools, the declared
jdk_version, evidence, and warnings. Maven POM paths are read and parsed in full
from the metadata address returned by read_all_files_tool. maven_projects
contains project/parent coordinates, Maven wrapper and required versions, Java and
Spring Boot version declarations, modules, profiles, all directly declared and
managed dependencies, and build plugins. Missing evidence or unresolved versions
may produce null; this does not prove that Spring is absent. Does not build or run
the project, resolve external parent POMs, or evaluate dynamic Gradle expressions.
    """
)
async def judge_spring_project_tool(sorted_files_by_suffix:dict):
    return judge_spring_project(sorted_files=sorted_files_by_suffix)


@mcp.tool(
    name="find_java_exe_tool",
    description="""
Search home_address recursively for usable Windows JDK installations.
Defaults to C:/ when omitted; provide a narrower directory when possible.
Only pairs of java.exe and javac.exe in the same directory that both pass
-version are included. Returns java_locations with version strings and executable
paths. Use the containing bin directory as jdk_location for run_java_get_feedback.
    """
)
async def find_java_exe_tool(home_address:str = None):
    return find_java_exe(home_address=home_address)


@mcp.tool(
    name="run_java_get_feedback",
    description="""
Compile and run a standalone .java file without a package declaration.
code_address is the source file path; jdk_location is the bin directory containing
java.exe and javac.exe, not the JDK root. Compilation creates class files beside
the source. Compilation and execution each have a 10-second timeout. Returns
status, output, and the exit code when available. Not intended for Spring Boot
projects or long-running services.
    """
)
async def run_java_get_feedback_tool(code_address:str, jdk_location:str):
    return run_java_get_feedback(code_address=code_address, jdk_location=jdk_location)


@mcp.tool(
    name="package_spring_boot_with_confirmation_tool",
    description="""
Validate a Spring Boot project and request user approval to package it.
project_path is the project directory; temp_path is the destination for the JAR.
This MCP wrapper uses non-interactive mode and returns confirmation_required
when approval is needed; it does not build the project. The host must obtain
explicit user approval and execute the confirmed operation separately. Packaging
can download dependencies, change build output, and overwrite a destination JAR.
    """
)
async def package_spring_boot_with_confirmation_tool(project_path:str, temp_path:str):
    return package_spring_boot_with_confirmation(project_path=project_path, temp_path=temp_path, interactive=True)


@mcp.tool(
    name="check_spring_boot_startup_tool",
    description="""
Test startup of an existing Spring Boot JAR using the specified java.exe.
java_path and jar_path are executable and JAR file paths. Runs with
--server.port=0 and waits up to timeout seconds (default 30), then terminates
the process and inspects captured startup logs. Returns success, error, or unknown
with available logs. Success means startup was observed, not that the service
remains running or that its HTTP endpoints and business functions were verified.
    """
)
async def check_spring_boot_startup_tool(java_path:str, jar_path:str, timeout:int = 30):
    return check_spring_boot_startup(java_path=java_path, jar_path=jar_path, timeout=timeout)


@mcp.tool(
    name="write_in_tool",
    description="""
Request approval to write UTF-8 text to file_address, creating parent directories
and replacing any existing file content. code is the complete intended content.
    """
)
async def write_in_tool(file_address:str, code:str):
    return write_in(file_address,code)


@mcp.tool(
    name="find_the_python_editor_tool",
    description="""
Search home_address recursively for Windows python.exe interpreters.
Defaults to C:/ when omitted; prefer a specific installation directory.
Each candidate is checked by running an isolated Python marker command.
Returns status and python_editor_location containing discovered interpreter paths.
Directory access errors may interrupt the search; this is not a guaranteed
complete inventory of installed Python environments.
    """
)
async def find_the_python_editor_tool(home_address:str = None):
    return find_the_python_editor(home_address=home_address)


@mcp.tool(
    name="run_code_get_feedback_tool",
    description="""
Run a Python file with the specified interpreter and capture execution feedback.
editor_address is the path to python.exe; code_path is the Python source path.
Use absolute paths. Returns status, stdout, stderr, and returncode when available;
execution times out after 10 seconds. The code runs with the server process
working directory and may change files or other resources. This tool does not
install missing dependencies or manage long-running services.
    """
)
async def run_code_get_feedback_tool(editor_address:str, code_path:str):
    return run_code_get_feedback(editor_address=editor_address, code_path=code_path)


@mcp.tool(
    name="download_package_with_confirmation_tool",
    description="""
Validate a Python interpreter and package requirement, then request installation
approval. editor_address is the python.exe path; package_name is one pip package
requirement, such as requests or requests==2.32.3. This MCP wrapper always uses
non-interactive mode and returns confirmation_required when approval is needed;
it does not install packages. The host must obtain explicit user approval before
separately installing into the specified environment. Never infer approval from
a model-generated argument.
    """
)
async def download_package_with_confirmation_tool(editor_address:str, package_name:str):
    return download_package_with_confirmation(editor_address=editor_address, package_name=package_name, interactive=True)


def get_system_stdio_parameters() -> dict[str,StdioServerParameters]:
    return {
        "system":StdioServerParameters(
            command = 'cmd',
            args = [
                "python",
                "-c",
                "MCP_functions/System_Files/Files_server/mcp_system_server.py"
            ]
        )
    }

if __name__ == "__main__":
    mcp.run(transport="stdio")

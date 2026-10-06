import shutil
from collections import deque
from pathlib import Path
import subprocess
from MCP_functions.sandbox import execution_run, check_path
import re
import xml.etree.ElementTree as ET
from .bfs_read import read_all_files,sort_files_by_suffix
import os
import sys


def _xml_text(node, path, default=""):
    value = node.findtext(path)
    return value.strip() if isinstance(value, str) else default


def _resolve_maven_value(value, properties):
    if not value:
        return None
    result = value.strip()
    visited = set()
    while result.startswith("${") and result.endswith("}") and result not in visited:
        visited.add(result)
        result = properties.get(result[2:-1], result)
    return result


def _maven_coordinate(node, properties):
    return {
        "group_id": _resolve_maven_value(_xml_text(node, "groupId"), properties),
        "artifact_id": _resolve_maven_value(_xml_text(node, "artifactId"), properties),
        "version": _resolve_maven_value(_xml_text(node, "version"), properties),
    }


def _read_complete_build_file(file_record, expected_names, warnings):
    """Read a build file in full; directory-scan content is only a preview."""
    address = str(file_record.get("address") or file_record.get("file_name") or "")
    path = check_path(address)
    if path.name not in expected_names or not path.is_file():
        content = file_record.get("content")
        if isinstance(content, str):
            warnings.append(f"{address}: source path unavailable; analyzed scan preview only")
            return content
        warnings.append(f"{address}: missing text content")
        return None
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        warnings.append(f"{address}: build file is not UTF-8 text")
    except OSError as exc:
        warnings.append(f"{address}: cannot read complete build file: {type(exc).__name__}")
    return None


def _parse_maven_project(root, address):
    # ElementTree does not fetch external parent POMs. Only the complete local POM
    # is parsed, so a large dependency section is not lost to scan-preview limits.
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]

    properties = {
        node.tag: (node.text or "").strip()
        for node in root.findall("./properties/*")
    }
    parent_node = root.find("./parent")
    parent = _maven_coordinate(parent_node, properties) if parent_node is not None else None
    project_group = _resolve_maven_value(_xml_text(root, "groupId"), properties)
    project_version = _resolve_maven_value(_xml_text(root, "version"), properties)
    project = {
        "group_id": project_group or (parent or {}).get("group_id"),
        "artifact_id": _resolve_maven_value(_xml_text(root, "artifactId"), properties),
        "version": project_version or (parent or {}).get("version"),
        "packaging": _xml_text(root, "packaging", "jar"),
    }

    dependencies = []
    for dependency in root.findall("./dependencies/dependency"):
        item = _maven_coordinate(dependency, properties)
        item.update({
            "scope": _resolve_maven_value(_xml_text(dependency, "scope"), properties) or "compile",
            "optional": _xml_text(dependency, "optional", "false").lower() == "true",
        })
        dependencies.append(item)

    managed_dependencies = [
        _maven_coordinate(node, properties)
        for node in root.findall("./dependencyManagement/dependencies/dependency")
    ]
    plugins = [
        _maven_coordinate(node, properties)
        for node in root.findall("./build/plugins/plugin")
    ]
    plugin_management = [
        _maven_coordinate(node, properties)
        for node in root.findall("./build/pluginManagement/plugins/plugin")
    ]

    java_declarations = {}
    for key in (
        "java.version", "maven.compiler.release", "maven.compiler.target",
        "maven.compiler.source",
    ):
        if properties.get(key):
            java_declarations[key] = _resolve_maven_value(properties[key], properties)
    for plugin in root.findall("./build/plugins/plugin"):
        if _xml_text(plugin, "artifactId") == "maven-compiler-plugin":
            for key in ("release", "target", "source"):
                value = _xml_text(plugin, f"./configuration/{key}")
                if value:
                    java_declarations[f"maven-compiler-plugin.{key}"] = _resolve_maven_value(
                        value, properties
                    )

    spring_boot_versions = []
    candidates = []
    if parent:
        candidates.append(("parent", parent))
    candidates.extend(("dependency", item) for item in dependencies + managed_dependencies)
    candidates.extend(("plugin", item) for item in plugins + plugin_management)
    for source, item in candidates:
        if item.get("group_id") == "org.springframework.boot" and item.get("version"):
            spring_boot_versions.append({"source": source, "version": item["version"]})
    for key in ("spring-boot.version", "spring.boot.version"):
        if properties.get(key):
            spring_boot_versions.append({
                "source": f"property:{key}",
                "version": _resolve_maven_value(properties[key], properties),
            })

    required_maven = _xml_text(root, "./prerequisites/maven") or None
    for plugin in root.findall("./build/plugins/plugin"):
        if _xml_text(plugin, "artifactId") != "maven-enforcer-plugin":
            continue
        value = plugin.findtext(".//requireMavenVersion/version")
        if value:
            required_maven = _resolve_maven_value(value.strip(), properties)
            break

    wrapper_version = None
    wrapper_file = Path(address).parent / ".mvn" / "wrapper" / "maven-wrapper.properties"
    if wrapper_file.is_file():
        try:
            wrapper_text = wrapper_file.read_text(encoding="utf-8-sig")
            match = re.search(r"apache-maven-([\w.-]+?)-bin\.zip", wrapper_text)
            wrapper_version = match.group(1) if match else None
        except (OSError, UnicodeDecodeError):
            pass

    return {
        "pom_path": address,
        "model_version": _xml_text(root, "modelVersion") or None,
        "project": project,
        "parent": parent,
        "maven": {
            "wrapper_version": wrapper_version,
            "required_version": required_maven,
        },
        "java_declarations": java_declarations,
        "spring_boot_versions": spring_boot_versions,
        "modules": [
            (node.text or "").strip() for node in root.findall("./modules/module")
            if (node.text or "").strip()
        ],
        "profiles": [
            _xml_text(node, "id") for node in root.findall("./profiles/profile")
            if _xml_text(node, "id")
        ],
        "dependency_count": len(dependencies),
        "declared_dependencies": dependencies,
        "managed_dependency_count": len(managed_dependencies),
        "managed_dependencies": managed_dependencies,
        "plugins": plugins,
        "plugin_management": plugin_management,
    }


# 根据一个项目的通过read_all_files()和sort_files_by_suffix()得到的结果来判断是不是spring/spring boot
def judge_spring_project(
        sorted_files: dict
):
    if not isinstance(sorted_files, dict):
        return {"status": "error", "message": "sorted_files must be a dict"}
    if sorted_files.get("status") == "error":
        return {"status": "error", "message": sorted_files.get("message")}

    files = sorted_files.get("sorted")
    if not isinstance(files, dict):
        return {"status": "error", "message": "the format of the file you provide is not correct"}

    evidence = []
    warnings = []
    build_tools = []
    jdk_versions = set()
    unresolved_jdk = False
    is_spring = False
    is_spring_boot = False
    maven_projects = []

    # 保留字符串字面量，去掉行注释和块注释，避免注释中的示例触发判断。
    def remove_comments(content):
        pattern = r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/'
        return re.sub(pattern, lambda m: " " if m.group().startswith(("//", "/*")) else m.group(), content)

    for suffix in (".xml", ".gradle", ".kts", ".java", ".kt"):
        entries = files.get(suffix, [])
        if not isinstance(entries, list):
            return {"status": "error", "message": f"{suffix} entries must be a list"}
        for file in entries:
            if not isinstance(file, dict):
                return {"status": "error", "message": "file entries must be dicts"}
            name = file.get("file_name", "")
            is_maven = name == "pom.xml"
            is_gradle = name in ("build.gradle", "build.gradle.kts")
            if not (is_maven or is_gradle or suffix in (".java", ".kt")):
                continue
            tool = "maven" if is_maven else "gradle" if is_gradle else None
            if tool and tool not in build_tools:
                build_tools.append(tool)
            address = str(file.get("address") or name)
            if is_maven:
                content = _read_complete_build_file(file, {"pom.xml"}, warnings)
            elif is_gradle:
                content = _read_complete_build_file(
                    file, {"build.gradle", "build.gradle.kts"}, warnings
                )
            else:
                content = file.get("content")
            if not isinstance(content, str):
                if not (is_maven or is_gradle):
                    warnings.append(f"{address}: missing text content")
                continue

            matches = []
            version_values = []
            if is_maven:
                try:
                    root = ET.fromstring(content)
                except ET.ParseError:
                    warnings.append(f"{address}: invalid Maven XML")
                    continue
                maven_project = _parse_maven_project(root, address)
                maven_projects.append(maven_project)
                # 去掉 XML 命名空间，按节点读取，不依赖缩进或换行。
                properties = {node.tag: (node.text or "").strip()
                              for node in root.findall("./properties/*")}
                for key in ("java.version", "maven.compiler.release",
                            "maven.compiler.target", "maven.compiler.source"):
                    if properties.get(key):
                        version_values.append(properties[key])
                for plugin in root.findall("./build/plugins/plugin"):
                    if plugin.findtext("artifactId") == "maven-compiler-plugin":
                        for key in ("release", "target", "source"):
                            value = plugin.findtext(f"./configuration/{key}")
                            if value:
                                version_values.append(value.strip())
                # 仅解析当前 POM 的属性引用，不执行构建或读取外部父 POM。
                for index, value in enumerate(version_values):
                    visited = set()
                    while value.startswith("${") and value.endswith("}") and value not in visited:
                        visited.add(value)
                        value = properties.get(value[2:-1], value)
                    version_values[index] = value
                for node in root.iter():
                    if node.tag in ("dependency", "parent", "plugin"):
                        group = (node.findtext("groupId") or "").strip()
                        artifact = (node.findtext("artifactId") or "").strip()
                        if group == "org.springframework" or group.startswith("org.springframework."):
                            matches.append((group, f"{node.tag}: {group}:{artifact}"))

            elif is_gradle:
                content = remove_comments(content)
                # 读取常见的 Groovy/Kotlin 字面量配置；动态表达式不推断。
                version_values.extend(re.findall(
                    r"\bJavaLanguageVersion\.of\(\s*(\d+)\s*\)", content))
                version_values.extend(re.findall(
                    r"\b(?:sourceCompatibility|targetCompatibility)\s*=\s*(?:JavaVersion\.VERSION_([\d_]+)|['\"]?(\d+(?:\.\d+)?)['\"]?)", content))
                version_values = [next((item for item in value if item), "")
                                  if isinstance(value, tuple) else value for value in version_values]

                for match in re.finditer(r"['\"](org\.springframework(?:\.[\w-]+)*):([\w-]+)(?::[^'\"]*)?['\"]", content):
                    matches.append((match.group(1), match.group(0)))
                if re.search(r"\bid\s*(?:\(\s*)?['\"]org\.springframework\.boot['\"]", content):
                    matches.append(("org.springframework.boot", "Spring Boot Gradle plugin"))
                # 支持 Groovy 的 group/name 命名参数形式。
                for match in re.finditer(r"\bgroup\s*[:=]\s*['\"](org\.springframework(?:\.[\w-]+)*)['\"]", content):
                    matches.append((match.group(1), match.group(0)))
            else:
                content = remove_comments(content)
                # 屏蔽字符串中的示例 import；检查真实的 Java/Kotlin 导入。
                content = re.sub(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"', '""', content)
                for match in re.finditer(r"(?m)^\s*import\s+(?:static\s+)?(org\.springframework(?:\.[\w*]+)+)\s*(?:;|$)", content):
                    matches.append((match.group(1), match.group(0).strip()))

            for value in version_values:
                version = re.fullmatch(r"(?:1[._])?([1-9]\d*)", value)
                if version:
                    jdk_versions.add(int(version.group(1)))
                else:
                    unresolved_jdk = True
                    warnings.append(f"{address}: unresolved Java version: {value}")

            for group, detail in matches:
                is_spring = True
                if group == "org.springframework.boot" or group.startswith("org.springframework.boot."):
                    is_spring_boot = True
                evidence.append({"file": address, "detail": detail})

    # 静态特征识别不解析父 POM、Gradle 别名或传递依赖；未命中不等于确定未使用。
    if not is_spring:
        warnings.append("No Spring evidence found; inherited or indirect dependencies are not resolved")
    jdk_version = next(iter(jdk_versions)) if len(jdk_versions) == 1 and not unresolved_jdk else None
    if not jdk_versions:
        warnings.append("No explicit Java version resolved from build files")
    elif len(jdk_versions) > 1:
        warnings.append(f"Different Java versions declared: {sorted(jdk_versions)}; select JDK manually")
    return {
        "status": "success",
        "is_spring_project": True if is_spring else None,
        "is_spring_boot_project": True if is_spring_boot else None,
        "build_tools": build_tools,
        "jdk_version": jdk_version,
        "maven_projects": maven_projects,
        "evidence": evidence,
        "warnings": warnings
    }


# 寻找电脑上的java启动器和java编辑器
def find_java_exe(
        home_address: str = None
):

    root = check_path(home_address or Path.cwd())

    if not root.is_dir():

        return {
            "status": "error",
            "message": "search directory does not exist"
        }

    directories = deque([root])
    visited = set()
    java_locations = []

    while directories:

        current = directories.popleft()

        try:
            resolved = check_path(current)

            if resolved in visited:

                continue

            visited.add(resolved)
            java_path = current / "java.exe"

            javac_path = current / "javac.exe"

            if java_path.is_file() and javac_path.is_file():

                valid = True
                version = ""
                for executable in (java_path, javac_path):
                    try:
                        result = execution_run(
                            [str(executable), "-version"],
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                            text=True,
                            errors="replace",
                            timeout=3,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        )

                        if result.returncode != 0:
                            valid = False
                            break

                        if executable == java_path:
                            output = (result.stdout or "") + "\n" + (result.stderr or "")
                            match = re.search(r'\bversion\s+"([^"\s]+)"', output)
                            version = match.group(1) if match else "unknown"

                    except (OSError, subprocess.SubprocessError):
                        valid = False
                        break

                if valid:
                    java_locations.append(
                        {
                            version: {
                                "javac": str(javac_path),
                                "java": str(java_path)
                            }
                        }
                    )

            for subpath in current.iterdir():
                if subpath.is_dir():
                    try:
                        check_path(subpath)
                    except PermissionError:
                        continue
                    directories.append(subpath)
        except (OSError, RuntimeError):
            # 无权限、路径失效或链接解析失败时，跳过该目录。
            continue
    if not java_locations:
        return {
            "status": "error",
            "message": "no usable java.exe and javac.exe pair found"
        }

    return {
        "status": "success",
        "java_locations": java_locations
    }


#java编译运行项目，并且给出回答
def run_java_get_feedback(
        code_address:str,
        jdk_location:str,
):
    code_address = check_path(code_address)
    jdk_location = check_path(jdk_location)

    if not code_address.is_file() or code_address.suffix != ".java":
        return {
            "status": "error",
            "message": "code address must be an existing .java file"
        }

    if not jdk_location.is_dir():
        return {
            "status": "error",
            "message": "jdk directory does not exist , please try other valid jdk address"
        }

    javac_location = jdk_location / "javac.exe"
    java_location = jdk_location / "java.exe"

    #利用java文件名等于类名的特性，启动
    java_class_name = code_address.stem

    if not javac_location.is_file() or not java_location.is_file():
        return {
            "status": "error",
            "message":"jdk directory must contain java.exe and javac.exe"
        }

    try:
        result = execution_run(
            [
                str(javac_location),
                "-cp",
                str(code_address.parent),
                str(code_address)
            ],
            capture_output=True,
            text=True,
            errors="replace",
            cwd=str(code_address.parent),
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )


        if result.returncode != 0:
            return {
                "status": "error",
                "message": result.stderr,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }

        result = execution_run(
            [str(java_location), "-cp", str(code_address.parent), java_class_name],
            capture_output=True,
            text=True,
            errors="replace",
            cwd=str(code_address.parent),
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

        return {
            "status": "success" if result.returncode == 0 else "error",
            "message": "java program ran successfully" if result.returncode == 0 else result.stderr,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }

    except subprocess.TimeoutExpired:

        return {
            "status": "unknown",
            "message": "java compilation or execution timed out after 10 seconds"
        }

    except OSError as e:

        return {
            "status": "error",
            "message": str(e)
        }


#将一个spring boot项目打包成一个jar并放在temp临时文件夹里面
def package_spring_boot(
        project_path:str,
        temp_path:str,
        confirmed:bool=False
):

    judge_spring_result = judge_spring_project(
        sort_files_by_suffix(
            read_all_files(
                project_path
            )
        )
    )

    if(judge_spring_result.get("status") == "error"):
        return {
            "status": "error",
            "message":"the path you provided is not valid"
        }

    if(not (judge_spring_result.get("is_spring_boot_project") or judge_spring_result.get("is_spring_boot"))):
        return {
            "status": "error",
            "message":"the project you provided is not a spring / spring-boot project"
        }

    project_path = Path(project_path)
    temp_path = Path(temp_path)

    if confirmed is not True:
        return {
            "status": "confirmation_required",
            "message": "需要用户确认是否允许写入临时目录",
            "project_path": str(project_path),
            "temp_path": str(temp_path)
        }


    temp_path.mkdir(
        parents=True,
        exist_ok=True
    )
    try:

        # =========================
        # Maven
        # =========================

        if (project_path / "pom.xml").is_file():

            # 优先使用项目自己的 Maven Wrapper
            if os.name == "nt" and (
                project_path / "mvnw.cmd"
            ).is_file():

                command = [
                    str(project_path / "mvnw.cmd"),
                    "clean",
                    "package",
                    "-DskipTests"
                ]

            elif (
                project_path / "mvnw"
            ).is_file():

                command = [
                    str(project_path / "mvnw"),
                    "clean",
                    "package",
                    "-DskipTests"
                ]

            else:

                command = [
                    "mvn",
                    "clean",
                    "package",
                    "-DskipTests"
                ]

            result = execution_run(
                command,
                cwd=str(project_path),
                capture_output=True,
                text=True,
                timeout=300,
                errors="replace"
            )

            if result.returncode != 0:
                return {
                    "status": "error",
                    "message": "maven package failed",
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode
                }

            # Maven 默认输出目录
            build_dir = (
                project_path
                / "target"
            )


        # =========================
        # Gradle
        # =========================

        elif (
            (project_path / "build.gradle").is_file()
            or
            (project_path / "build.gradle.kts").is_file()
        ):

            if os.name == "nt" and (
                project_path / "gradlew.bat"
            ).is_file():

                command = [
                    str(
                        project_path
                        / "gradlew.bat"
                    ),
                    "bootJar"
                ]

            elif (
                project_path / "gradlew"
            ).is_file():

                command = [
                    str(
                        project_path
                        / "gradlew"
                    ),
                    "bootJar"
                ]

            else:

                command = [
                    "gradle",
                    "bootJar"
                ]

            result = execution_run(
                command,
                cwd=str(project_path),
                capture_output=True,
                text=True,
                timeout=300,
                errors="replace"
            )

            if result.returncode != 0:
                return {
                    "status": "error",
                    "message": "gradle package failed",
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode
                }

            # Gradle 默认 jar 目录
            build_dir = (
                project_path
                / "build"
                / "libs"
            )

        else:

            return {
                "status": "error",
                "message":
                    "cannot find pom.xml or build.gradle"
            }

        # =========================
        # 找生成的 jar
        # =========================

        jars = [
            path
            for path in build_dir.glob("*.jar")
            if not path.name.endswith(
                ".original"
            )
            and not path.name.endswith(
                "-plain.jar"
            )
        ]

        if not jars:
            return {
                "status": "error",
                "message":
                    "build succeeded but no executable jar was found"
            }

        # 一般 Spring Boot 项目只有一个最终 jar
        # 有多个的话先取最新生成的
        jar_path = max(
            jars,
            key=lambda p: p.stat().st_mtime
        )

        destination = (
            temp_path
            / jar_path.name
        )

        shutil.copy2(
            jar_path,
            destination
        )

        return {
            "status": "success",
            "message":
                "spring boot project packaged successfully",
            "jar_path":
                str(destination),
            "stdout":
                result.stdout,
            "stderr":
                result.stderr
        }

    except subprocess.TimeoutExpired:
        return {
            "status": "unknown",
            "message":
                "spring boot packaging timed out"
        }

    except OSError as e:
        return {
            "status": "error",
            "message": str(e)
        }

def package_spring_boot_with_confirmation(
        project_path: str,
        temp_path: str,
        interactive: bool = False
):

    request = package_spring_boot(
        project_path,
        temp_path,
        confirmed=False
    )

    if request.get("status") != "confirmation_required":
        return request

    # 由宿主明确选择控制台交互，不依赖 PyCharm 的 isatty() 结果。
    if interactive is not True or sys.stdin is None:
        return request

    try:
        print(
            "Spring Boot 打包请求",
            file=sys.stderr
        )

        print(
            f"项目目录：{request['project_path']}",
            file=sys.stderr
        )

        print(
            f"目标临时目录：{request['temp_path']}",
            file=sys.stderr
        )

        print(
            "该操作会创建目录并写入构建生成的 jar 文件。",
            file=sys.stderr
        )

        print(
            "是否允许本次写入？[y/N]：",
            end="",
            flush=True,
            file=sys.stderr
        )

        answer = input().strip().lower()

    except (EOFError, KeyboardInterrupt):
        return {
            "status": "cancelled",
            "message": "用户取消操作，未写入临时目录"
        }

    if answer not in {"y", "yes"}:
        return {
            "status": "cancelled",
            "message": "用户未允许写入临时目录"
        }

    return package_spring_boot(
        project_path,
        temp_path,
        confirmed=True
    )


def check_spring_boot_startup(
        java_path: str,
        jar_path: str,
        timeout: int = 30
):
    java_path = Path(java_path)
    jar_path = Path(jar_path)

    if not java_path.is_file():
        return {
            "status": "error",
            "message": "java executable does not exist"
        }

    if not jar_path.is_file():
        return {
            "status": "error",
            "message": "jar does not exist"
        }

    try:
        result = execution_run(
            [
                str(java_path),
                "-jar",
                str(jar_path),
                "--server.port=0"
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout
        )

        logs = (
            (result.stdout or "")
            + "\n"
            + (result.stderr or "")
        )

        # Spring 在超时前自己退出了
        if result.returncode != 0:
            return {
                "status": "error",
                "message": "spring boot startup failed",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }

        return {
            "status": "error",
            "message":
                "spring boot process exited before startup verification",
            "logs": logs
        }

    except subprocess.TimeoutExpired as e:

        stdout = e.stdout or ""
        stderr = e.stderr or ""

        # 某些 Python 版本下可能拿到 bytes
        if isinstance(stdout, bytes):
            stdout = stdout.decode(
                errors="replace"
            )

        if isinstance(stderr, bytes):
            stderr = stderr.decode(
                errors="replace"
            )

        logs = stdout + "\n" + stderr

        if (
            "APPLICATION FAILED TO START" in logs
            or "BeanCreationException" in logs
            or "UnsatisfiedDependencyException" in logs
        ):
            return {
                "status": "unknown",
                "message":
                    "spring boot startup failed",
                "logs": logs
            }

        if re.search(r"\bStarted [\w.$]+ in \d+(?:\.\d+)? seconds\b", logs):
            return {
                "status": "success",
                "message":
                    "spring boot started successfully",
                "logs": logs
            }

        return {
            "status": "unknown",
            "message":
                "spring boot did not finish startup within timeout",
            "logs": logs
        }

    except OSError as e:
        return {
            "status": "error",
            "message": str(e)
        }

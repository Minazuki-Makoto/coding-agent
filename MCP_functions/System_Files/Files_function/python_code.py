import subprocess
from MCP_functions.sandbox import execution_run, check_path
import sys
from pathlib import Path

#要先尝试找到电脑上的python解释器
def find_the_python_editor(home_address: str = None,editor_set:set = None):

    if home_address is None:
        home_address = check_path(Path.cwd())
    else:
        home_address = check_path(home_address)

    if editor_set is None:
        editor_set=set()

    for subpath in home_address.iterdir():
        try:
            check_path(subpath)
        except PermissionError:
            continue

        if subpath.is_file():
            if subpath.name.lower() == "python.exe":
                try:
                    result = execution_run(
                        [
                            str(subpath),
                            "-I",
                            "-c",
                            "print('PYTHON_CHECK_OK')"
                        ],
                        capture_output=True,
                        text=True,
                        timeout=3,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    )

                    if result.returncode == 0 and result.stdout.strip() == "PYTHON_CHECK_OK":
                        editor_set.add(
                            str(subpath)
                        )

                except (
                        OSError,
                        subprocess.SubprocessError
                ):
                    pass

        elif subpath.is_dir():
            find_the_python_editor(
                str(subpath),
                editor_set
            )

    if(len(editor_set) == 0):
        return {
            "status":"error",
            "message":"not find python editor"
        }

    else:
        return {
            "status":"success",
            "python_editor_location":editor_set
        }

#运行python代码
def run_code_get_feedback(
        editor_address: str,
        code_path: str
):
    try:
        editor_address = check_path(editor_address)
        code_path = check_path(code_path)

        if not editor_address.is_file():
            return {
                "status": "error",
                "message": "python interpreter does not exist"
            }

        if not code_path.is_file():
            return {
                "status": "error",
                "message": "code file does not exist"
            }

        result = execution_run(
            [
                str(editor_address),
                str(code_path)
            ],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return {
                "status": "error",
                "message": result.stderr,
                "stdout": result.stdout,
                "returncode": result.returncode
            }

        return {
            "status": "success",
            "message": "program executed successfully",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }

    except subprocess.TimeoutExpired:
        return {
            "status": "unknown",
            "message": "program execution timed out"
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }


def download_package(
        editor_address: str,
        package_name: str,
        confirmed: bool = False
):
    # confirmed 必须由调用方根据用户明确同意的结果传入。
    if not isinstance(editor_address, str) or not editor_address.strip():
        return {
            "status": "error",
            "message": "editor_address does not exist,please provide a valid path"
        }

    editor_address = Path(editor_address)
    if not editor_address.is_file():
        return {
            "status": "error",
            "message": "editor_address does not exist,please provide a valid path"
        }

    if not isinstance(package_name, str) or not package_name.strip():
        return {
            "status": "error",
            "message": "package_name must not be empty"
        }

    package_name = package_name.strip()
    if package_name.startswith("-"):
        return {
            "status": "error",
            "message": "package_name must not be a pip option"
        }

    if confirmed is not True:

        return {
            "status": "confirmation_required",
            "message": "请确认是否允许在指定 Python 环境中安装该依赖，确认后传入 confirmed=True",
            "editor_address": str(editor_address),
            "package_name": package_name
        }

    try:
        result = execution_run(
            [
                str(editor_address),
                "-m", "pip", "install", "--no-input",
                package_name
            ],
            stdin=subprocess.DEVNULL,
            timeout=300,
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

        return {
            "status": "success" if result.returncode == 0 else "error",
            "message": "package installed successfully" if result.returncode == 0 else result.stderr,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }

    except subprocess.TimeoutExpired:
        return {
            "status": "unknown",
            "message": "package installation timed out; the environment may have changed"
        }
    except OSError as e:
        return {
            "status": "error",
            "message": str(e)
        }


#当运行python发现少一个包的时候，向用户申请权限以拉取这个包
def download_package_with_confirmation(editor_address: str, package_name: str,interactive:bool=False):
    """仅供拥有交互终端的调用入口使用，不要注册为 stdio MCP 服务端工具。"""
    request = download_package(editor_address, package_name, confirmed=False)
    if request.get("status") != "confirmation_required":
        return request

    # 非交互调用不读取 stdin，以免阻塞或消费协议数据。
    if sys.stdin is None or not interactive:
        return request

    editor_address = request["editor_address"]
    package_name = request["package_name"]
    try:
        print("安装依赖请求", file=sys.stderr)
        print(f"Python 环境：{editor_address!r}", file=sys.stderr)
        print(f"待安装依赖：{package_name!r}", file=sys.stderr)
        print("这会下载并安装该依赖及其所需依赖到上述环境。", file=sys.stderr)
        print("是否允许本次安装？[Y/N]：", end="", flush=True, file=sys.stderr)
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return {"status": "cancelled", "message": "用户取消安装，未执行 pip"}

    if answer not in {"y", "yes"}:
        return {"status": "cancelled", "message": "用户未同意安装，未执行 pip"}

    return download_package(editor_address, package_name, confirmed=True)

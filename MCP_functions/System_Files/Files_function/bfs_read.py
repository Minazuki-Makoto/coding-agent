from pathlib import Path
from collections import deque

def read_all_files(home_address: str):
    if home_address is None:
        return {
            "status": "error",
            "message": "home_address shouldn't be None, please provide one precise home address"
        }

    home_address = Path(home_address)

    if not home_address.exists():
        return {
            "status": "error",
            "message": "please provide a valid home address"
        }

    if home_address.is_file():
        return {
            "status": "success",
            "files": [
                {
                    "file_name": home_address.name,
                    "address": str(home_address.resolve()),
                    "mother_file": str(home_address.parent.resolve()),
                    "suffix": home_address.suffix
                }
            ]
        }

    dir_deque = deque([home_address])
    files = []

    while dir_deque:
        address = dir_deque.popleft()

        for item in address.iterdir():

            if item.is_file():
                try:
                    with open(item.resolve(), "r", encoding="utf-8-sig") as f:
                        content = f.read()
                except UnicodeDecodeError:
                    content = None

                files.append(
                    {
                        "file_name": item.name,
                        "address": str(item.resolve()),
                        "mother_file": str(item.parent.resolve()),
                        "suffix": item.suffix,
                        "content": content
                    }
                )

            elif item.is_dir():
                dir_deque.append(item)

    return {
        "status": "success",
        "files": files
    }

#根据文件后缀名判断
def sort_files_by_suffix(
        files:dict
):

    status = files.get("status")
    if(status is None or status == "error"):
        return {
            "status": "error",
            "message": "I can't get any information from the address you provide "
        }

    sorted_files ={}


    try:
        for file in files.get("files"):
            file_suffix = file.get("suffix")
            if file_suffix is None :
                raise Exception(f"{file.get('file_name')} has no suffix")

            if file_suffix not in sorted_files:
                sorted_files[file_suffix] = [file]

            else:
                sorted_files[file_suffix].append(file)


        return {
            "status": "success",
            "sorted": sorted_files
        }


    except Exception as e:

        return {
            "status":"error",
            "message":str(e)
        }


def sort_files_by_mother(
    files:dict
):
    status = files.get("status")
    if (status is None or status == "error"):
        return {
            "status": "error",
            "message": "I can't get any information from the address you provide "
        }

    sorted_files = {}

    try:
        for file in files.get("files"):
            file_parent_address = file.get("suffix")
            if file_parent_address is None:
                raise Exception(f"{file.get('file_name')} has no parent address")

            if file_parent_address not in sorted_files:
                sorted_files[file_parent_address] = [file]

            else:
                sorted_files[file_parent_address].append(file)

        return {
            "status": "success",
            "sorted": sorted_files
        }


    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }


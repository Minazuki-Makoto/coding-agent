import json
from pathlib import Path


def get_needed_info(
        needed:str
):
    path = Path(r"D:\pycharmcode\coding_agent\INFORMATION.json")
    if not str:
        return {
            "status":"error",
            "message":"please provide the information you need"
        }

    if path.exists():
        with open(path, "r") as f:
            content = json.loads(
                f.read()
            )

        if not isinstance(content,dict):
            return {
                "status":"error",
                "message":"the INFORMATION.json file has some format problems,you can get the information you need by using existing tools"
            }

        if needed not in content.keys():
            #后续方便写入
            return {
                "status":"error",
                "needed_updating":True,
                "message":"please provide the information you need , you van try to get the information you need by using existing tools",
                "missing_point":needed
            }

        return {
            "status":"success",
            "message":content.get(needed)
        }




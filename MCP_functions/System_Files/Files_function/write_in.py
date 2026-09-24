from pathlib import Path
def write_in(file_address: str, code: str):
    # 创建父目录；已经存在则不报错
    Path(file_address).parent.mkdir(parents=True, exist_ok=True)

    with open(file_address, "a", encoding="utf-8") as f:
        f.write(code)


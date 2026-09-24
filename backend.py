import random
from datetime import datetime

from flask import Flask
from pathlib import Path
from AgentLoop.agent import main
import asyncio
app = Flask(__name__)


def create_session(
        home_directory: Path = Path(r"D:\coding-agent")
):
    if not home_directory.exists():
        raise FileNotFoundError(
            f"未发现路径: {home_directory}"
        )

    random_part = ''.join(
        str(random.randint(0, 9))
        for _ in range(12)
    )

    time_part = datetime.now().strftime("%Y%m%d_%H%M%S")

    session_id = f"{random_part}_{time_part}"

    session_address = home_directory / session_id

    session_address.mkdir(
        parents=True,
        exist_ok=False
    )

    return session_id, session_address


if __name__ == '__main__':
    session_id, session_address = create_session()

    print(f"session_id: {session_id}")
    print(f"session_address: {session_address}")

    chat_id = "".join(
        str(random.randint(0, 9))
        for _ in range(12)
    )

    asyncio.run(
        main(
            query="阅读D:\pycharmcode\ElectricityLLM\llm-back的项目结构，告诉我这是一个什么功能，都有哪些模块，每个模块负责什么",
            session_address=str(session_address),
            chat_id=chat_id
        )
    )


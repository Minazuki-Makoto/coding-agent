from mcp import StdioServerParameters
import os

def get_remote_mcp_link() -> dict[str,StdioServerParameters]:

    return {
    "github": StdioServerParameters(
        command="docker",
        args=[
            "run",
            "-i",
            "--rm",

            "-e",
            "GITHUB_PERSONAL_ACCESS_TOKEN",

            # 建议 coding agent 初期只读
            "-e",
            "GITHUB_READ_ONLY",

            "ghcr.io/github/github-mcp-server"
        ],
        env={
            "GITHUB_PERSONAL_ACCESS_TOKEN":
                os.environ["GITHUB_API"],

            "GITHUB_READ_ONLY": "1"
        }
    ),

    # Microsoft Playwright MCP
    "browser": StdioServerParameters(
        command="cmd",
        args=[
            "/c",
            "npx",
            "-y",
            "@playwright/mcp@latest"
        ]
    ),

    "docker":StdioServerParameters(
        command="uvx",
        args=[
            "--from",
            "git+https://github.com/L337-org/docker-mcp.git",
            "docker-mcp-server"
        ],
        env={
            "DOCKER_MCP_SERVER_NO_DESTRUCTIVE": "1"
        }
    )
    }
from anthropic import Anthropic
import os


def get_claude_api():
    try:
        api = os.environ["ANTHROPIC_API_KEY"]

        if api:
            return {
                "status": "success",
                "message": api
            }

        return {
            "status": "error",
            "message": "can't read Claude API from your computer"
        }

    except KeyError as e:
        return {
            "status": "error",
            "message": str(e)
        }


def claude_chat(
        client: Anthropic,
        model: str,
        query: list[dict],
        tools: list[dict],
        temperature: float | None = None,
        max_tokens: int = 4096
):

    try:
        response = client.messages.create(
            model=model,
            messages=query,
            max_tokens=max_tokens,
            **({"tools": tools, "tool_choice": {"type": "auto"}} if tools else {}),
            extra_body={"temperature": temperature} if temperature is not None else None
        )

        results = ""
        for text in response.content:
            if text.type == "text":
                results += text.text

        return {
            "status": "success",
            "message": results,
            "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
            "tool": [block.model_dump() for block in response.content if block.type == "tool_use"]
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "total_tokens": 0,
            "tool": []
        }

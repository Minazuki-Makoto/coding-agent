from openai import OpenAI
import os

def get_deepseek_api():
    try:
        api = os.environ["DEEPSEEK_API_KEY"]

        if api:
            return {
                "status": "success",
                "message": api
            }

        return {
            "status": "error",
            "message": "can't read DeepSeek API from your computer"
        }

    except KeyError as e:
        return {
            "status": "error",
            "message": str(e)
        }


def deepseek_chat(
        client: OpenAI,
        model: str,
        query: list[dict],
        tools: list[dict],
        temperature: float
):

    try:
        response = client.chat.completions.create(
            model=model,
            messages=query,
            **({"tools": tools, "tool_choice": "auto"} if tools else {}),
            temperature=temperature,
            stream=False
        )

        return {
            "status": "success",
            "message": response.choices[0].message.content,
            "total_tokens": response.usage.total_tokens if response.usage else 0,
            "tool": [
                call.model_dump()
                for call in (response.choices[0].message.tool_calls or [])
            ]
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "total_tokens": 0,
            "tool": []
        }

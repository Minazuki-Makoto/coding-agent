import os
from openai import OpenAI

def get_gpt_api():
    try:
        api = os.environ["OPENAI_API_KEY"]

        if api:
            return {
                "status":"success",
                "message":api
            }

        return {
            "status":"error",
            "message":"can't read chatgpt-api from your computer"
        }

    except KeyError as e:

        return {
            "status":"error",
            "message":str(e)
        }

def chatGpt_chat(
        client: OpenAI,
        query:list[dict],
        model:str,
        expected_temperature,
        tools:list[dict]=None,
):

    try:
        response = client.chat.completions.create(
            messages=query,
            model=model,
            temperature=expected_temperature,
            **({"tools": tools, "tool_choice": "auto"} if tools else {}),
            timeout=90
        )


        return {
            "status":"success",
            "message":response.choices[0].message.content,
            "total_tokens": response.usage.total_tokens if response.usage else 0,
            "tool": [
                call.model_dump()
                for call in (response.choices[0].message.tool_calls or [])
            ]
        }



    except Exception as e:
        return {
            "status":"error",
            "message":str(e),
            "total_tokens": 0,
            "tool": []
        }



from zai import ZhipuAiClient
import os

def get_zhipu_api():
    try:
        api = os.environ["GLM_API_KEY"]

        if api:
            return {
                "status":"success",
                "message":api
            }

        return {
            "status":"error",
            "message":"can't read GLM-api from your computer"
        }

    except KeyError as e:

        return {
            "status":"error",
            "message":str(e)
        }

def zhipu_chat(
        client: ZhipuAiClient,
        model:str,
        query:list[dict],
        tools:list[dict],
        temperature:float
):

    try:
        response = client.chat.completions.create(
            model=model,
            messages=query,
            **({"tools": tools, "tool_choice": "auto"} if tools else {}),
            temperature=temperature,
            timeout=90,
            thinking={
                "type":"disabled"
            }
        )


        return {
            "status": "success",
            "message": response.choices[0].message.content,
            "tool": response["choices"][0]["message"].get("tool_calls") or []
        }

    except Exception as e:
            return {
                "status": "error",
                "message": str(e),
                "tool": []
            }





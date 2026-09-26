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


def _get_completion_message(response):
    if isinstance(response,dict):
        choices = response.get("choices") or []
    else:
        choices = getattr(response,"choices",None) or []

    if not choices:
        raise ValueError("GLM response does not contain choices")

    first_choice = choices[0]
    if isinstance(first_choice,dict):
        message = first_choice.get("message")
    else:
        message = getattr(first_choice,"message",None)

    if message is None:
        raise ValueError("GLM response choice does not contain message")

    return message


def _get_message_field(message,field_name,default=None):
    if isinstance(message,dict):
        return message.get(field_name,default)
    return getattr(message,field_name,default)


def _serialize_tool_call(tool_call):
    if isinstance(tool_call,dict):
        return tool_call

    if hasattr(tool_call,"model_dump"):
        return tool_call.model_dump(exclude_none=True)

    if hasattr(tool_call,"dict"):
        return tool_call.dict(exclude_none=True)

    raise ValueError(
        f"unsupported GLM tool call type: {type(tool_call).__name__}"
    )

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

        message = _get_completion_message(response)
        tool_calls = _get_message_field(message,"tool_calls",[]) or []
        return {
            "status": "success",
            "message": _get_message_field(message,"content"),
            "tool": [
                _serialize_tool_call(tool_call)
                for tool_call in tool_calls
            ]
        }

    except Exception as e:
            return {
                "status": "error",
                "message": str(e),
                "tool": []
            }

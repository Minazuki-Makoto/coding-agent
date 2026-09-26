from pathlib import Path
import json

def save_chat_history(
        chat_id:str,
        session_address:str,
        query_content:str,
        answer_content:str,
        description:str,
        task_number:int,
        seq_number:int,
        supervisor_descriptions:list[dict] | None = None,
):
    session_address = Path(session_address)
    session_address.mkdir(parents=True,exist_ok=True)

    with open(session_address / "chat_history.jsonl",'a',encoding='utf-8') as f:
        f.write(
            json.dumps(
                {
                    "chat_id":chat_id,
                    "task_number":task_number,
                    "seq_number":seq_number,
                    "session_address":str(session_address),
                    "query_content":query_content,
                    "answer_content":answer_content,
                    "description":description,
                    "supervisor_descriptions":supervisor_descriptions or [],
                },ensure_ascii=False,
            )+'\n'
        )

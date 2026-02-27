import json

import requests

# 配置
API_URL = "http://localhost:5000/openapi/chat"
API_KEY = "llmops-v1/SO3Mlx-IRgsGCMUVqc84wHIRPPohMn1QYVenj9vUcOMyr8AEQai6hPUDmH2wnkNm"
APP_ID = "7d76372e-ffbe-477c-ba20-388f1fa16c09"


# 非流式请求
def chat_block(query: str):
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    data = {"app_id": APP_ID, "query": query, "stream": False}

    response = requests.post(API_URL, headers=headers, json=data)
    result = response.json()

    if result["code"] == "success":
        print(result["data"]["answer"])
    else:
        print(f"Error: {result['message']}")


# 流式请求
def chat_stream(query: str):
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    data = {"app_id": APP_ID, "query": query, "stream": True}

    response = requests.post(API_URL, headers=headers, json=data, stream=True)

    for line in response.iter_lines():
        if line:
            line_str = line.decode("utf-8")
            if line_str.startswith("data:"):
                data_str = line_str.split(":", 1)[1].strip()
                event_data = json.loads(data_str)
                if event_data.get("answer"):
                    print(event_data["answer"], end="", flush=True)


# 使用示例
# chat_block("你好，请介绍一下你自己")
chat_stream("请详细介绍一下LLM是什么")

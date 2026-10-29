from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import requests
from pydantic import BaseModel
import time

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
MY_CLIENT_KEY = "my_agent_search_2026"

# --- 防线 1：简单的内存限流器 ---
rate_limit_records = {}
MAX_REQUESTS_PER_MINUTE = 5

def check_rate_limit(client_ip: str):
    now = time.time()
    if client_ip not in rate_limit_records:
        rate_limit_records[client_ip] = []
    rate_limit_records[client_ip] = [t for t in rate_limit_records[client_ip] if now - t < 60]
    if len(rate_limit_records[client_ip]) >= MAX_REQUESTS_PER_MINUTE:
        raise HTTPException(status_code=429, detail="请求太频繁，请1分钟后后再试！")
    rate_limit_records[client_ip].append(now)

class SearchRequest(BaseModel):
    query: str
    count: int = 3

@app.post("/v1/search")
async def agent_search(req: SearchRequest, request: Request, authorization: str = Header(None)):
    if authorization != f"Bearer {MY_CLIENT_KEY}":
        raise HTTPException(status_code=401, detail="Token错误！")
    
    check_rate_limit(request.client.host)

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "query": req.query,
        "count": req.count,
        "need_content": False
    }
    
    # --- 防线 2：多源降级逻辑 ---
    try:
        # 尝试正常请求底层搜索接口
        res = requests.post("https://api.deepseek.com/v1/tools/search", headers=headers, json=payload, timeout=10)
        res.raise_for_status()
        raw_results = res.json().get("results", [])
        
        cleaned_results = []
        for item in raw_results:
            cleaned_results.append({
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("summary", "")[:150] + "...",
                "source": "My-Enhanced-Search"
            })
            
        return {"status": "success", "query": req.query, "total": len(cleaned_results), "results": cleaned_results}

    except Exception as search_error:
        # 如果搜索接口挂掉，立刻触发降级：调用对话模型兜底
        print(f"⚠️ 搜索接口异常，触发降级方案: {search_error}")
        
        fallback_prompt = f"你是一个搜索引擎。请根据你的知识库，简要回答用户的搜索词：{req.query}。字数在100字以内。"
        fallback_payload = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": fallback_prompt}]
        }
        
        try:
            # 尝试用对话模型生成结果
            chat_res = requests.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=fallback_payload, timeout=15)
            chat_res.raise_for_status()
            fallback_text = chat_res.json()["choices"][0]["message"]["content"]
            
            # 包装成正常的搜索结果格式返回
            return {
                "status": "success",
                "query": req.query,
                "total": 1,
                "results": [{
                    "title": f"关于 {req.query} 的备用解答",
                    "url": "https://deepseek.com",
                    "snippet": fallback_text[:150] + "...",
                    "source": "Fallback-Engine"
                }]
            }
        except Exception as fallback_error:
            # 如果连兜底方案都挂了，返回一个友好的提示，而不是直接报错
            return {
                "status": "success",
                "query": req.query,
                "total": 1,
                "results": [{
                    "title": "搜索服务正在维护中",
                    "url": "",
                    "snippet": "由于底层网络波动，当前暂无法获取实时数据，请稍后再试。",
                    "source": "System-Notice"
                }]
            }

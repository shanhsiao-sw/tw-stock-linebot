"""
台股財經戰情室 — LINE 互動式 AI Bot（Vercel Serverless）
========================================================
當你在 LINE 傳訊息給 Bot，它會：
1. 接收你的問題（例如：「輝達供應鏈最近股價如何？」）
2. 呼叫 OpenAI + Web Search 即時搜尋最新資料
3. 用繁體中文回覆分析結果

環境變數（在 Vercel Dashboard 設定）：
  OPENAI_API            — OpenAI API Key
  LINE_CHANNEL_ACCESS_TOKEN — LINE Messaging API Token
  LINE_CHANNEL_SECRET       — LINE Channel Secret（驗證 Webhook 用）
"""

import os
import json
import hashlib
import hmac
import base64
import re
import requests
from http.server import BaseHTTPRequestHandler

# ─── 環境變數 ──────────────────────────────────────────────────
OPENAI_API = os.environ.get("OPENAI_API", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"


# ─── LINE 簽章驗證 ─────────────────────────────────────────────

def verify_signature(body, signature):
    """驗證 LINE Webhook 的簽章，確保訊息來自 LINE 平台"""
    if not LINE_CHANNEL_SECRET:
        return True  # 開發階段可暫時跳過

    mac = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    )
    expected = base64.b64encode(mac.digest()).decode("utf-8")
    return hmac.compare_digest(expected, signature)


# ─── OpenAI 呼叫 ──────────────────────────────────────────────

def ask_ai(user_question):
    """
    用 OpenAI 回答使用者的台股問題。
    優先使用 Responses API + Web Search，fallback 到 gpt-4o。
    """
    system_prompt = """你是一位專業的台股財經分析助理。使用者會問你台股相關問題。
請搜尋最新的即時資料來回答。

回覆規則：
1. 用繁體中文回覆
2. 以條理分明的格式回覆，適當使用 emoji
3. 提到個股時附上股票代碼（例如：台積電(2330)）
4. 提到數字時要具體（股價、漲跌幅、成交量等）
5. 最後加上免責聲明：「⚠️ 以上為 AI 分析，僅供參考，不構成投資建議。」
6. 回覆長度控制在 1500 字以內（LINE 訊息不宜過長）"""

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API}",
    }

    # Debug: 確認 API Key 有讀到
    key_preview = OPENAI_API[:8] + "..." if OPENAI_API else "(空)"
    print(f"[DEBUG] API Key: {key_preview}, 問題: {user_question[:50]}")

    # ── 方法 1：Responses API + Web Search ──
    try:
        payload = {
            "model": "gpt-4o",
            "tools": [
                {
                    "type": "web_search_preview",
                    "search_context_size": "medium",
                    "user_location": {
                        "type": "approximate",
                        "country": "TW",
                        "city": "Taipei",
                        "timezone": "Asia/Taipei",
                    },
                }
            ],
            "input": f"{system_prompt}\n\n使用者問題：{user_question}",
        }

        resp = requests.post(
            OPENAI_RESPONSES_URL, headers=headers, json=payload, timeout=55
        )
        resp.raise_for_status()
        data = resp.json()

        full_text = ""
        if "output_text" in data:
            full_text = data["output_text"]
        elif "output" in data:
            for item in data["output"]:
                if item.get("type") == "message":
                    for content in item.get("content", []):
                        if content.get("type") == "output_text":
                            full_text += content.get("text", "")

        if full_text.strip():
            return clean_response(full_text)

    except Exception as e:
        print(f"[方法1] Responses API 失敗: {e}")
        try:
            print(f"[方法1] 回應: {resp.status_code} {resp.text[:300]}")
        except Exception:
            pass
    try:
        payload = {
            "model": "gpt-4o-search-preview",
            "web_search_options": {},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_question},
            ],
            "max_tokens": 2000,
        }

        resp = requests.post(
            OPENAI_CHAT_URL, headers=headers, json=payload, timeout=55
        )
        resp.raise_for_status()
        data = resp.json()

        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if text.strip():
            return clean_response(text)

    except Exception as e:
        print(f"[方法2] search-preview 失敗: {e}")
        try:
            print(f"[方法2] 回應: {resp.status_code} {resp.text[:300]}")
        except Exception:
            pass
    try:
        payload = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_question},
            ],
            "max_tokens": 2000,
            "temperature": 0.3,
        }

        resp = requests.post(
            OPENAI_CHAT_URL, headers=headers, json=payload, timeout=55
        )
        resp.raise_for_status()
        data = resp.json()

        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if text.strip():
            return clean_response(text)

    except Exception as e:
        print(f"[方法3] gpt-4o 失敗: {e}")
        try:
            print(f"[方法3] 回應: {resp.status_code} {resp.text[:300]}")
        except Exception:
            pass

    return "抱歉，目前 AI 暫時無法處理你的問題，請稍後再試。"


def clean_response(text):
    """清理 AI 回覆：移除 markdown 語法、截斷過長內容"""
    # 移除 markdown 的 ** bold
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    # 移除 markdown 的 ### 標題
    text = re.sub(r"^#{1,4}\s+", "", text, flags=re.MULTILINE)
    # 移除 markdown links [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    # 移除 citation markers 【...】
    text = re.sub(r"【[^】]*】", "", text)

    # LINE 單則訊息上限 5000 字元
    if len(text) > 4800:
        text = text[:4800] + "\n\n...（內容過長已截斷）"

    return text.strip()


# ─── LINE 回覆 ─────────────────────────────────────────────────

def reply_to_line(reply_token, text):
    """透過 LINE Reply API 回覆訊息"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }

    # 如果回覆太長，拆成多則
    MAX_LEN = 4800
    if len(text) <= MAX_LEN:
        messages = [{"type": "text", "text": text}]
    else:
        chunks = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > MAX_LEN:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

        messages = [{"type": "text", "text": c} for c in chunks[:5]]

    payload = {
        "replyToken": reply_token,
        "messages": messages,
    }

    requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=10)


# ─── Vercel Serverless Handler ─────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        """GET 請求回傳簡單狀態頁面（用於確認部署成功）"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "🤖 台股財經戰情室 LINE Bot 運作中！".encode("utf-8")
        )

    def do_POST(self):
        """處理 LINE Webhook POST 請求"""
        try:
            # 讀取 request body
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")

            # 驗證簽章
            signature = self.headers.get("X-Line-Signature", "")
            if not verify_signature(body, signature):
                self.send_response(403)
                self.end_headers()
                return

            # 解析事件
            data = json.loads(body)
            events = data.get("events", [])

            for event in events:
                # 只處理文字訊息
                if event.get("type") != "message":
                    continue
                if event.get("message", {}).get("type") != "text":
                    continue

                user_text = event["message"]["text"].strip()
                reply_token = event["replyToken"]

                # 忽略空訊息
                if not user_text:
                    continue

                # 呼叫 AI 回答
                ai_response = ask_ai(user_text)

                # 回覆 LINE
                reply_to_line(reply_token, ai_response)

            # 回傳 200（LINE 要求必須回 200）
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))

        except Exception as e:
            # 即使出錯也回 200，避免 LINE 持續重試
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"status": "error", "message": str(e)}).encode("utf-8")
            )

#!/usr/bin/env python3
"""
QQ 官方机器人 + 智谱 AI 对接服务（Render/云平台部署版）
支持单聊(C2C)和群聊@机器人消息
内置 HTTP 健康检查服务，适配 Render 等云平台
"""

import asyncio
import json
import logging
import os
import time
from threading import Thread
from typing import Optional

import requests
import websockets
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==================== 配置（从环境变量读取，更安全） ====================
APP_ID = os.environ.get("QQ_APP_ID", "1904363546")
APP_SECRET = os.environ.get("QQ_APP_SECRET", "rhYPH92vpkfbXURPONNNOPRUXbfkpv29")
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "139b41c531f64da8b88aa1f3eb4e3d13.w0hrbh0N5qVlK3T7")
ZHIPU_MODEL = os.environ.get("ZHIPU_MODEL", "glm-4.7-flash")

# API 端点
API_BASE = "https://api.bot.qq.com"
WS_GATEWAY = "wss://api.bot.qq.com/websocket/"
ZHIPU_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# 事件订阅: GROUP_AND_C2C_EVENT (1 << 25)
INTENTS = 1 << 25

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("qqbot")


# ==================== HTTP 健康检查服务（Render 需要） ====================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"QQ Bot is running!")

    def log_message(self, format, *args):
        pass  # 静默健康检查日志


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info(f"健康检查服务已启动，端口: {port}")
    server.serve_forever()


# ==================== 智谱 AI 调用（含限流重试） ====================
_last_call_time = 0
_min_interval = 1.0


def call_zhipu(messages: list, max_tokens: int = 1024) -> str:
    global _last_call_time

    headers = {
        "Authorization": f"Bearer {ZHIPU_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": ZHIPU_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "thinking": {"type": "disabled"},
    }

    max_retries = 4
    for attempt in range(max_retries):
        now = time.time()
        wait = _min_interval - (now - _last_call_time)
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.time()

        try:
            resp = requests.post(ZHIPU_API_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 0))
                delay = retry_after if retry_after > 0 else (2 ** attempt)
                log.warning(f"智谱API限流(429)，第{attempt+1}次重试，等待{delay}秒...")
                time.sleep(delay)
                continue
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            if not content and attempt == 0:
                payload["max_tokens"] = 2048
                log.info("回复内容为空，加大token重试...")
                continue
            return content
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                delay = 2 ** attempt
                log.warning(f"智谱API限流(429)，第{attempt+1}次重试，等待{delay}秒...")
                time.sleep(delay)
                continue
            log.error(f"智谱API调用失败: {e}")
            return "抱歉，AI服务暂时不可用，请稍后再试。"
        except Exception as e:
            log.error(f"智谱API调用异常: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return "抱歉，AI服务暂时不可用，请稍后再试。"

    return "抱歉，当前请求较多，请稍后再试～"


# ==================== QQ Access Token 管理 ====================
class TokenManager:
    def __init__(self):
        self._token: Optional[str] = None
        self._expire_at: float = 0

    def get_token(self) -> str:
        if self._token and time.time() < self._expire_at - 60:
            return self._token
        return self._refresh()

    def _refresh(self) -> str:
        url = f"{API_BASE}/app/getAppAccessToken"
        payload = {"appId": APP_ID, "clientSecret": APP_SECRET}
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise Exception(f"获取Token失败: {data}")
        self._token = data["access_token"]
        self._expire_at = time.time() + int(data.get("expires_in", 7200))
        log.info(f"Access Token 已刷新，有效期至 {time.strftime('%H:%M:%S', time.localtime(self._expire_at))}")
        return self._token


token_mgr = TokenManager()


# ==================== 发送消息 ====================
def send_c2c_message(openid: str, content: str, msg_id: str = None):
    token = token_mgr.get_token()
    url = f"{API_BASE}/v2/users/{openid}/messages"
    headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}
    payload = {"content": content, "msg_type": 0, "msg_seq": int(time.time() * 1000) % 1000000}
    if msg_id:
        payload["msg_id"] = msg_id
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    log.info(f"单聊回复结果: {resp.status_code}")


def send_group_message(group_openid: str, content: str, msg_id: str = None):
    token = token_mgr.get_token()
    url = f"{API_BASE}/v2/groups/{group_openid}/messages"
    headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}
    payload = {"content": content, "msg_type": 0, "msg_seq": int(time.time() * 1000) % 1000000}
    if msg_id:
        payload["msg_id"] = msg_id
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    log.info(f"群聊回复结果: {resp.status_code}")


# ==================== 消息处理 ====================
session_history: dict = {}
MAX_HISTORY = 10


def get_history(key: str) -> list:
    return session_history.get(key, [])


def add_history(key: str, role: str, content: str):
    if key not in session_history:
        session_history[key] = []
    session_history[key].append({"role": role, "content": content})
    if len(session_history[key]) > MAX_HISTORY:
        session_history[key] = session_history[key][-MAX_HISTORY:]


def handle_c2c_message(data: dict):
    author = data.get("author", {})
    user_openid = author.get("user_openid", "")
    msg_id = data.get("id", "")
    content = data.get("content", "").strip()

    if not content or not user_openid:
        return

    log.info(f"[单聊] {user_openid}: {content}")

    if content in ["/clear", "清空", "重置"]:
        session_history.pop(user_openid, None)
        send_c2c_message(user_openid, "对话已清空，我们重新开始吧！", msg_id)
        return

    if content in ["/help", "帮助"]:
        help_text = "我是接入智谱AI的QQ机器人\n直接发送消息即可对话\n发送 /clear 清空对话记录\n发送 /help 查看帮助"
        send_c2c_message(user_openid, help_text, msg_id)
        return

    history = get_history(user_openid)
    messages = [{"role": "system", "content": "你是一个乐于助人的AI助手，用简洁友好的中文回答用户问题。"}]
    messages.extend(history)
    messages.append({"role": "user", "content": content})

    reply = call_zhipu(messages)
    add_history(user_openid, "user", content)
    add_history(user_openid, "assistant", reply)
    send_c2c_message(user_openid, reply, msg_id)


def handle_group_message(data: dict):
    group_openid = data.get("group_openid", "")
    msg_id = data.get("id", "")
    content = data.get("content", "").strip()

    if "@" in content:
        content = content.split("@", 1)[1].strip()
        if " " in content:
            content = content.split(" ", 1)[1].strip()

    if not content or not group_openid:
        return

    log.info(f"[群聊] {group_openid}: {content}")

    if content in ["/clear", "清空", "重置"]:
        session_history.pop(f"group_{group_openid}", None)
        send_group_message(group_openid, "群对话已清空！", msg_id)
        return

    key = f"group_{group_openid}"
    history = get_history(key)
    messages = [{"role": "system", "content": "你是一个乐于助人的AI助手，用简洁友好的中文回答群聊中的问题。"}]
    messages.extend(history)
    messages.append({"role": "user", "content": content})

    reply = call_zhipu(messages)
    add_history(key, "user", content)
    add_history(key, "assistant", reply)
    send_group_message(group_openid, reply, msg_id)


# ==================== WebSocket 客户端 ====================
async def ws_client():
    last_seq = None
    session_id = None
    heartbeat_interval = 45000
    heartbeat_task = None

    while True:
        try:
            log.info("正在连接 QQ 机器人网关...")
            async with websockets.connect(WS_GATEWAY, ping_interval=None) as ws:
                log.info("WebSocket 连接成功")

                hello_msg = json.loads(await ws.recv())
                if hello_msg.get("op") == 10:
                    heartbeat_interval = hello_msg["d"]["heartbeat_interval"]

                token = token_mgr.get_token()

                if session_id and last_seq:
                    identify = {
                        "op": 6,
                        "d": {"token": f"QQBot {token}", "session_id": session_id, "seq": last_seq},
                    }
                    log.info("尝试恢复会话...")
                else:
                    identify = {
                        "op": 2,
                        "d": {
                            "token": f"QQBot {token}",
                            "intents": INTENTS,
                            "shard": [0, 1],
                            "properties": {"$os": "linux", "$browser": "qqbot-zhipu", "$device": "qqbot-zhipu"},
                        },
                    }

                await ws.send(json.dumps(identify))
                log.info("鉴权信息已发送")

                async def heartbeat():
                    while True:
                        await asyncio.sleep(heartbeat_interval / 1000)
                        await ws.send(json.dumps({"op": 1, "d": last_seq}))

                heartbeat_task = asyncio.create_task(heartbeat())

                async for raw_msg in ws:
                    msg = json.loads(raw_msg)
                    op = msg.get("op")
                    seq = msg.get("s")
                    if seq:
                        last_seq = seq

                    if op == 0:
                        event_type = msg.get("t")
                        data = msg.get("d", {})

                        if event_type == "READY":
                            session_id = data.get("session_id")
                            user = data.get("user", {})
                            log.info(f"机器人已就绪! 名称: {user.get('username')}, ID: {user.get('id')}")
                        elif event_type == "RESUMED":
                            log.info("会话已恢复")
                        elif event_type == "C2C_MESSAGE_CREATE":
                            asyncio.create_task(asyncio.to_thread(handle_c2c_message, data))
                        elif event_type == "GROUP_AT_MESSAGE_CREATE":
                            asyncio.create_task(asyncio.to_thread(handle_group_message, data))
                        elif event_type == "FRIEND_ADD":
                            log.info(f"新好友添加: {data}")
                        elif event_type == "GROUP_ADD_ROBOT":
                            log.info(f"机器人被加入群: {data}")

                    elif op == 7:
                        log.warning("服务端要求重连")
                        break
                    elif op == 9:
                        log.error(f"会话无效: {msg}")
                        session_id = None
                        last_seq = None
                        await asyncio.sleep(2)
                        break

        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"WebSocket 连接断开: {e}，5秒后重连...")
        except Exception as e:
            log.error(f"WebSocket 异常: {e}", exc_info=True)
            log.info("5秒后重连...")
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
            await asyncio.sleep(5)


# ==================== 主入口 ====================
def main():
    log.info("=" * 50)
    log.info("QQ 机器人 + 智谱 AI 服务启动（云平台版）")
    log.info(f"AppID: {APP_ID}")
    log.info(f"AI模型: {ZHIPU_MODEL}")
    log.info("=" * 50)

    # 启动 HTTP 健康检查服务（Render 必需）
    health_thread = Thread(target=start_health_server, daemon=True)
    health_thread.start()

    # 测试 Token
    try:
        token_mgr.get_token()
        log.info("Access Token 获取成功")
    except Exception as e:
        log.error(f"启动警告 - 无法获取Access Token: {e}")

    # 启动 WebSocket 机器人
    asyncio.run(ws_client())


if __name__ == "__main__":
    main()

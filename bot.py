import json
import time
import requests
import websocket
import threading
from flask import Flask

# =========配置信息============
APP_ID = "1904363546"
APP_SECRET = "rhYPH92vpkfbXURPONNNOPRUXbfkpv29"
ZHIPU_KEY = "139b41c531f64da8b88aa1f3eb4e3d13.w0hrbh0N5qVlK3T7"
ZHIPU_MODEL = "glm-4.7-flash"
# ==============================

app = Flask(__name__)
access_token = ""
token_expire = 0
conversation_cache = {}

# 保活网页路由
@app.route('/')
def index():
    return "机器人运行中！"

# 获取QQ机器人Token
def get_token():
    global access_token, token_expire
    now = time.time()
    if access_token and token_expire - now > 120:
        return access_token
    try:
        url = f"https://api.q.qq.com/api/getToken?grant_type=client_credential&appid={APP_ID}&secret={APP_SECRET}"
        res = requests.get(url, timeout=15).json()
        access_token = res["access_token"]
        token_expire = now + res["expires_in"]
        print("✅ 获取QQ AccessToken成功")
        return access_token
    except Exception as e:
        print("❌ 获取Token失败", e)
        return None

# 调用智谱AI，自带429限流重试
def chat_with_zhipu(msg_list):
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    header = {"Authorization": f"Bearer {ZHIPU_KEY}", "Content-Type":"application/json"}
    data = {
        "model": ZHIPU_MODEL,
        "messages": msg_list,
        "temperature":0.7
    }
    retry = 0
    max_retry =3
    wait =2
    while retry <= max_retry:
        try:
            resp = requests.post(url, headers=header, json=data, timeout=30)
            if resp.status_code ==429:
                print(f"⚠️智谱限流，等待{wait}s重试")
                time.sleep(wait)
                wait *=2
                retry +=1
                continue
            res_json = resp.json()
            return res_json["choices"][0]["message"]["content"]
        except Exception as e:
            print("AI调用异常",e)
            time.sleep(wait)
            retry +=1
    return "当前AI服务访问繁忙，请稍后重试"

# 发送消息
def send_message(is_group, target_id, msg_id, content):
    token = get_token()
    if not token:
        return
    headers = {"Authorization":f"QQBot {token}", "Content-Type":"application/json"}
    payload = {"content":content,"msg_type":0,"msg_id":msg_id}
    if is_group:
        url = f"https://api.q.qq.com/v2/groups/{target_id}/messages"
    else:
        url = f"https://api.q.qq.com/v2/users/{target_id}/messages"
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print("消息发送失败",e)

# WebSocket消息处理
def on_message(ws, message):
    data = json.loads(message)
    event = data.get("d",{})
    content = event.get("content","").strip()
    if not content:
        return
    group_id = event.get("group_id")
    author = event.get("author",{})
    user_id = author.get("user_openid", author.get("id"))
    msg_id = event.get("id")
    session_id = f"g_{group_id}_{user_id}" if group_id else f"u_{user_id}"

    # 清空对话指令
    if content in ["/clear","重置","清空"]:
        if session_id in conversation_cache:
            del conversation_cache[session_id]
        send_message(bool(group_id), group_id or user_id, msg_id, "✅对话上下文已清空")
        return

    # 加载历史对话
    history = conversation_cache.get(session_id, [])
    history.append({"role":"user","content":content})
    if len(history)>10:
        history = history[-10:]
    ai_text = chat_with_zhipu(history)
    history.append({"role":"assistant","content":ai_text})
    conversation_cache[session_id] = history
    send_message(bool(group_id), group_id or user_id, msg_id, ai_text)

def on_error(ws, error):
    print("ws错误:", error)

def on_close(ws, close_status_code, close_msg):
    print("🔌连接断开，准备重连...")
    time.sleep(5)
    start_ws()

def start_ws():
    token = get_token()
    if not token:
        time.sleep(5)
        start_ws()
        return
    ws_url = f"wss://api.q.qq.com/ws?access_token={token}"
    ws = websocket.WebSocketApp(ws_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    print("🤖机器人WebSocket启动成功！等待消息...")
    ws.run_forever()

# 后台启动websocket机器人
def run_bot():
    while True:
        try:
            start_ws()
        except Exception as e:
            print("程序崩溃重启:",e)
            time.sleep(5)

# 新开线程跑机器人
threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

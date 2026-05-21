#!/usr/bin/env python3
"""
やまさんLINE Bot + Instagram自動投稿システム

フロー：
- やまさんがLINEにメモや写真を送る → AIが投稿文を生成 → OK → Instagram投稿
- 24時間投稿がない場合のみ1日1回だけ通知を送る
- 返信APIを最大限使用（無料・無制限）、プッシュAPIは最小限
"""

import os
import json
import schedule
import time
import threading
import requests
import anthropic
from datetime import datetime, timedelta
from flask import Flask, request, Response
from dotenv import load_dotenv

load_dotenv()

IG_USER_ID = os.getenv("IG_USER_ID", "17841401082943293")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID", "Ubacd4253590620330be7e9dc117d446b")
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY")

app = Flask(__name__)

state = {
    "waiting_for_image": False,
    "waiting_for_ok": False,
    "current_caption": "",
    "current_memo": "",
    "current_image_url": "",
    "last_post_time": None,
    "last_notify_time": None,   # 最後に24h通知を送った時刻
}


# ── LINE送信 ──────────────────────────────────────────

def send_line_reply(reply_token: str, text: str):
    """返信API（無料・無制限）"""
    res = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    )
    print(f"LINE返信: {res.status_code}")
    return res


def send_line_push(text: str):
    """プッシュAPI（月200通まで）― 通知専用で最小限に使う"""
    res = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]}
    )
    print(f"LINEプッシュ: {res.status_code} {res.text[:80]}")
    return res


# ── AI・Instagram ──────────────────────────────────────

SYSTEM_PROMPT = """あなたはやまさん（山崎清治）のSNS投稿文を作成するアシスタントです。

やまさんのプロフィール：
- 教育・啓発活動の専門家・講演者
- NPO法人「SHOSAPO（ショサポ）」代表（「シャオサポ」は絶対に使わない）
- ハッシュタグは必ず「#SHOSAPO」（アルファベット大文字）を使う

投稿文のルール：
- 感情はシンプルに一言だけ
- 意識高い系・自己啓発っぽい表現は絶対に使わない
- 淡々と事実を書いて、感情は短く添える程度にする
- カッコつけず、素直な言葉で書く
- 関西弁をほんの少し混ぜてもOK
- 150〜250文字程度
- 末尾にハッシュタグ5〜8個（#SHOSAPO 必須、#ショサポ/#シャオサポ 禁止）"""


def generate_post_text(memo: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1024, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"以下のメモをもとにInstagram投稿文を作成してください：\n\n{memo}"}]
    )
    return msg.content[0].text


def generate_post_text_from_image(image_url: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1024, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "url", "url": image_url}},
            {"type": "text", "text": "この写真の内容をもとに投稿文を作成してください。"}
        ]}]
    )
    return msg.content[0].text


def revise_post_text(current_caption: str, instruction: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1024,
        messages=[{"role": "user", "content":
            f"以下の投稿文を「{instruction}」という指示で修正してください。#SHOSAPOを必ず含めてください。\n\n{current_caption}"}]
    )
    return msg.content[0].text


def download_line_image(message_id: str) -> str:
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    res = requests.get(f"https://api-data.line.me/v2/bot/message/{message_id}/content", headers=headers)
    if res.status_code != 200:
        return ""
    import base64
    image_b64 = base64.b64encode(res.content).decode("utf-8")
    imgbb_res = requests.post("https://api.imgbb.com/1/upload",
                               data={"key": IMGBB_API_KEY, "image": image_b64})
    if imgbb_res.status_code == 200:
        return imgbb_res.json()["data"]["url"]
    return ""


def post_to_instagram(image_url: str, caption: str) -> str:
    base_url = f"https://graph.instagram.com/v21.0/{IG_USER_ID}"
    try:
        res1 = requests.post(f"{base_url}/media",
                             params={"image_url": image_url, "caption": caption,
                                     "access_token": IG_ACCESS_TOKEN}, timeout=30)
        if not res1.ok:
            return res1.json().get("error", {}).get("message", res1.text[:200])

        container_id = res1.json().get("id")
        if not container_id:
            return "コンテナIDが取得できませんでした"

        for _ in range(6):
            time.sleep(5)
            st = requests.get(f"https://graph.instagram.com/v21.0/{container_id}",
                              params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN},
                              timeout=10).json().get("status_code", "")
            if st == "FINISHED":
                break
            if st == "ERROR":
                return "Instagram画像処理エラー。別の写真で試してください。"

        res2 = requests.post(f"{base_url}/media_publish",
                             params={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN},
                             timeout=30)
        if not res2.ok:
            return res2.json().get("error", {}).get("message", res2.text[:200])

        return ""
    except Exception as e:
        return str(e)


# ── Webhook ───────────────────────────────────────────

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return Response("OK", status=200)

    body = json.loads(request.get_data())
    try:
        for event in body.get("events", []):
            if event.get("type") != "message":
                continue

            reply_token = event.get("replyToken", "")
            msg = event["message"]

            if msg["type"] == "text":
                handle_text(msg["text"].strip(), reply_token)

            elif msg["type"] == "image":
                # 即座に返信（無料）してから処理
                send_line_reply(reply_token, "画像を受信しました！処理中...📤")
                image_url = download_line_image(msg["id"])
                if not image_url:
                    send_line_push("画像のアップロードに失敗しました。もう一度送ってください")
                    continue

                if state["waiting_for_image"]:
                    state["waiting_for_image"] = False
                    state["current_image_url"] = image_url
                    state["waiting_for_ok"] = True
                    send_line_push(
                        f"アップロード完了！\n\n【投稿文】\n{state['current_caption']}\n\n"
                        "「OK」で投稿、「もう一度」で文章を再生成、「修正して→○○」で修正"
                    )
                else:
                    caption = generate_post_text_from_image(image_url)
                    state["current_caption"] = caption
                    state["current_image_url"] = image_url
                    state["waiting_for_ok"] = True
                    send_line_push(
                        f"【生成された投稿文】\n\n{caption}\n\n"
                        "「OK」で投稿、「もう一度」で再生成、「修正して→○○」で修正"
                    )
    except Exception as e:
        print(f"Webhookエラー: {e}")

    return Response("OK", status=200)


def handle_text(text: str, reply_token: str):
    # OKコマンド
    if text.upper() in ["OK", "ＯＫ", "ok", "おけ", "オケ"]:
        if state.get("current_image_url") and state.get("current_caption"):
            send_line_reply(reply_token, "投稿中です...📸")
            error_msg = post_to_instagram(state["current_image_url"], state["current_caption"])
            if not error_msg:
                state["last_post_time"] = datetime.now()
                state["current_image_url"] = ""
                state["current_caption"] = ""
                state["waiting_for_ok"] = False
                send_line_push("✅ Instagramへの投稿が完了しました！")
            else:
                send_line_push(f"❌ 投稿に失敗しました。\nエラー内容：{error_msg}")
        else:
            send_line_reply(reply_token, "投稿する写真がありません。写真を送ってください📸")
        return

    # リセット
    if text in ["リセット", "やめる", "キャンセル", "最初から"]:
        state["waiting_for_image"] = False
        state["waiting_for_ok"] = False
        state["current_caption"] = ""
        state["current_memo"] = ""
        state["current_image_url"] = ""
        send_line_reply(reply_token, "リセットしました！\nいつでもメモや写真を送ってください📸")
        return

    # 再生成・修正（OK待ち or 画像待ち）
    if state["waiting_for_ok"] or state["waiting_for_image"]:
        if text in ["もう一度", "再生成", "やり直し"]:
            send_line_reply(reply_token, "再生成中です...")
            memo = state.get("current_memo", "")
            caption = generate_post_text(memo) if memo else generate_post_text_from_image(state.get("current_image_url", ""))
            state["current_caption"] = caption
            send_line_push(f"【再生成された投稿文】\n\n{caption}\n\n「OK」で投稿、「修正して→○○」で修正")
            return
        if text.startswith("修正して"):
            instruction = text.replace("修正して", "").replace("→", "").strip()
            send_line_reply(reply_token, "修正中です...")
            caption = revise_post_text(state["current_caption"], instruction)
            state["current_caption"] = caption
            send_line_push(f"【修正された投稿文】\n\n{caption}\n\n「OK」で投稿、「修正して→○○」でさらに修正")
            return

    # 通常のメモ → キャプション生成
    send_line_reply(reply_token, "投稿文を生成中です...少し待ってください📝")
    state["current_memo"] = text
    caption = generate_post_text(text)
    state["current_caption"] = caption
    state["waiting_for_image"] = True
    state["waiting_for_ok"] = False
    send_line_push(f"【生成された投稿文】\n\n{caption}\n\n写真をLINEで送ってください📸\n「もう一度」で再生成、「修正して→○○」で修正")


# ── エンドポイント ────────────────────────────────────

@app.route("/ask", methods=["GET"])
def trigger_ask():
    send_line_push("今日どんなことがありましたか？\n一言でOKです！")
    return Response("送信完了", status=200)


@app.route("/reset", methods=["GET"])
def reset_state():
    state["waiting_for_image"] = False
    state["waiting_for_ok"] = False
    state["current_caption"] = ""
    state["current_memo"] = ""
    state["current_image_url"] = ""
    return Response("リセット完了", status=200)


@app.route("/linetest", methods=["GET"])
def line_test():
    res = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": "LINE接続テスト"}]}
    )
    return Response(f"status={res.status_code} body={res.text}", status=200)


@app.route("/webhook", methods=["GET"])
def webhook_check():
    return Response("OK", status=200)


# ── スケジューラー（24時間通知のみ）───────────────────

def check_24h_notify():
    """最後の投稿から24時間経過 かつ 今日まだ通知していない場合のみ送信"""
    last_post = state.get("last_post_time")
    last_notify = state.get("last_notify_time")
    now = datetime.now()

    if last_post is None:
        return

    hours_since_post = (now - last_post).total_seconds() / 3600
    if hours_since_post < 24:
        return

    # 今日すでに通知済みならスキップ
    if last_notify and (now - last_notify).total_seconds() < 86400:
        return

    state["last_notify_time"] = now
    send_line_push("今日はまだ投稿していませんね。\nいつでもメモや写真を送ってください📸")


def run_scheduler():
    schedule.every(1).hours.do(check_24h_notify)
    while True:
        schedule.run_pending()
        time.sleep(60)


scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()

print("=== やまさんLINE Bot起動 ===")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

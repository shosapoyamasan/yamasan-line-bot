#!/usr/bin/env python3
"""
やまさんLINE Bot + Instagram自動投稿システム
- 返信API（無料・無制限）のみ使用
- プッシュAPIは24時間通知のみ（月1回程度）
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
    "last_notify_time": None,
}


def reply(reply_token: str, text: str):
    """返信API（無料・無制限）"""
    res = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    )
    print(f"返信: {res.status_code} {res.text[:80]}")
    return res


def push(text: str):
    """プッシュAPI（月200通）― 24h通知専用"""
    res = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]}
    )
    print(f"プッシュ: {res.status_code}")
    return res


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
    msg = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
        model="claude-sonnet-4-6", max_tokens=1024, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"以下のメモをもとにInstagram投稿文を作成してください：\n\n{memo}"}]
    )
    return msg.content[0].text


def generate_post_text_from_image(image_url: str) -> str:
    msg = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
        model="claude-sonnet-4-6", max_tokens=1024, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "url", "url": image_url}},
            {"type": "text", "text": "この写真の内容をもとに投稿文を作成してください。"}
        ]}]
    )
    return msg.content[0].text


def revise_post_text(caption: str, instruction: str) -> str:
    msg = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
        model="claude-sonnet-4-6", max_tokens=1024,
        messages=[{"role": "user", "content":
            f"以下の投稿文を「{instruction}」という指示で修正してください。#SHOSAPOを必ず含めてください。\n\n{caption}"}]
    )
    return msg.content[0].text


def download_line_image(message_id: str) -> str:
    res = requests.get(
        f"https://api-data.line.me/v2/bot/message/{message_id}/content",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    )
    if res.status_code != 200:
        return ""
    import base64
    imgbb = requests.post("https://api.imgbb.com/1/upload",
                          data={"key": IMGBB_API_KEY,
                                "image": base64.b64encode(res.content).decode("utf-8")})
    return imgbb.json()["data"]["url"] if imgbb.status_code == 200 else ""


def post_to_instagram(image_url: str, caption: str) -> str:
    base = f"https://graph.instagram.com/v21.0/{IG_USER_ID}"
    try:
        r1 = requests.post(f"{base}/media",
                           params={"image_url": image_url, "caption": caption,
                                   "access_token": IG_ACCESS_TOKEN}, timeout=30)
        if not r1.ok:
            return r1.json().get("error", {}).get("message", r1.text[:200])

        cid = r1.json().get("id")
        if not cid:
            return "コンテナIDが取得できませんでした"

        # 画像処理完了を待つ（最大15秒）
        for _ in range(5):
            time.sleep(3)
            st = requests.get(f"https://graph.instagram.com/v21.0/{cid}",
                              params={"fields": "status_code",
                                      "access_token": IG_ACCESS_TOKEN},
                              timeout=10).json().get("status_code", "")
            if st == "FINISHED":
                break
            if st == "ERROR":
                return "Instagram画像処理エラー。別の写真で試してください。"

        r2 = requests.post(f"{base}/media_publish",
                           params={"creation_id": cid,
                                   "access_token": IG_ACCESS_TOKEN}, timeout=30)
        if not r2.ok:
            return r2.json().get("error", {}).get("message", r2.text[:200])
        return ""
    except Exception as e:
        return str(e)


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return Response("OK", status=200)

    body = json.loads(request.get_data())
    try:
        for event in body.get("events", []):
            if event.get("type") != "message":
                continue
            rt = event.get("replyToken", "")
            msg = event["message"]

            if msg["type"] == "text":
                handle_text(msg["text"].strip(), rt)
            elif msg["type"] == "image":
                handle_image(msg["id"], rt)
    except Exception as e:
        print(f"エラー: {e}")

    return Response("OK", status=200)


def handle_text(text: str, rt: str):
    # OKで投稿
    if text.upper() in ["OK", "ＯＫ", "ok", "おけ", "オケ"]:
        if state.get("current_image_url") and state.get("current_caption"):
            err = post_to_instagram(state["current_image_url"], state["current_caption"])
            if not err:
                state["last_post_time"] = datetime.now()
                state["current_image_url"] = ""
                state["current_caption"] = ""
                state["waiting_for_ok"] = False
                reply(rt, "✅ Instagramへの投稿が完了しました！")
            else:
                reply(rt, f"❌ 投稿に失敗しました。\nエラー内容：{err}")
        else:
            reply(rt, "投稿する写真がありません。写真を送ってください📸")
        return

    # リセット
    if text in ["リセット", "やめる", "キャンセル", "最初から"]:
        state.update({"waiting_for_image": False, "waiting_for_ok": False,
                      "current_caption": "", "current_memo": "", "current_image_url": ""})
        reply(rt, "リセットしました！\nいつでもメモや写真を送ってください📸")
        return

    # 再生成
    if text in ["もう一度", "再生成", "やり直し"]:
        if state.get("current_caption"):
            memo = state.get("current_memo", "")
            img = state.get("current_image_url", "")
            caption = generate_post_text(memo) if memo else generate_post_text_from_image(img)
            state["current_caption"] = caption
            reply(rt, f"【再生成された投稿文】\n\n{caption}\n\n「OK」で投稿、「修正して→○○」で修正")
        else:
            reply(rt, "まずメモか写真を送ってください")
        return

    # 修正
    if text.startswith("修正して"):
        if state.get("current_caption"):
            instruction = text.replace("修正して", "").replace("→", "").strip()
            caption = revise_post_text(state["current_caption"], instruction)
            state["current_caption"] = caption
            reply(rt, f"【修正された投稿文】\n\n{caption}\n\n「OK」で投稿、「修正して→○○」でさらに修正")
        else:
            reply(rt, "まずメモか写真を送ってください")
        return

    # テキストメモ → キャプション生成
    state["current_memo"] = text
    caption = generate_post_text(text)
    state["current_caption"] = caption
    state["waiting_for_image"] = True
    state["waiting_for_ok"] = False
    reply(rt, f"【生成された投稿文】\n\n{caption}\n\n写真をLINEで送ってください📸\n「もう一度」で再生成、「修正して→○○」で修正")


def handle_image(message_id: str, rt: str):
    image_url = download_line_image(message_id)
    if not image_url:
        reply(rt, "画像のアップロードに失敗しました。もう一度送ってください")
        return

    if state["waiting_for_image"] and state.get("current_caption"):
        # テキストメモ済み → 写真だけ受け取る
        state["waiting_for_image"] = False
        state["current_image_url"] = image_url
        state["waiting_for_ok"] = True
        reply(rt, f"アップロード完了！\n\n【投稿文】\n{state['current_caption']}\n\n「OK」で投稿、「もう一度」で再生成、「修正して→○○」で修正")
    else:
        # 写真だけ → 画像認識でキャプション生成
        caption = generate_post_text_from_image(image_url)
        state["current_caption"] = caption
        state["current_image_url"] = image_url
        state["waiting_for_image"] = False
        state["waiting_for_ok"] = True
        reply(rt, f"【生成された投稿文】\n\n{caption}\n\n「OK」で投稿、「もう一度」で再生成、「修正して→○○」で修正")


@app.route("/reset", methods=["GET"])
def reset_endpoint():
    state.update({"waiting_for_image": False, "waiting_for_ok": False,
                  "current_caption": "", "current_memo": "", "current_image_url": ""})
    return Response("リセット完了", status=200)


@app.route("/linetest", methods=["GET"])
def line_test():
    res = push("LINE接続テスト")
    return Response(f"status={res.status_code} body={res.text}", status=200)


def check_24h_notify():
    last = state.get("last_post_time")
    if not last:
        return
    now = datetime.now()
    if (now - last).total_seconds() < 86400:
        return
    last_n = state.get("last_notify_time")
    if last_n and (now - last_n).total_seconds() < 86400:
        return
    state["last_notify_time"] = now
    push("今日はまだ投稿していませんね。\nいつでもメモや写真を送ってください📸")


threading.Thread(target=lambda: [schedule.every(1).hours.do(check_24h_notify),
                                  [schedule.run_pending() or time.sleep(60) for _ in iter(int, 1)]],
                 daemon=True).start()

print("=== やまさんLINE Bot起動 ===")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)

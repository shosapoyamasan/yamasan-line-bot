#!/usr/bin/env python3
"""
やまさんLINE Bot + Instagram自動投稿システム

フロー：
1. 朝9時・夜9時にLINEで「今日どんなことがありましたか？」と問いかけ
2. やまさんが返信 → AIが投稿文を生成してLINEに返信
3. やまさんが「OK」と送信 → Instagramに自動投稿
4. やまさんが「もう一度」と送信 → 投稿文を再生成
"""

import os
import json
import requests
import anthropic
from flask import Flask, request, Response
from dotenv import load_dotenv

load_dotenv()

# 設定
IG_USER_ID = os.getenv("IG_USER_ID", "17841401082943293")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID", "Ubacd4253590620330be7e9dc117d446b")

app = Flask(__name__)

# 会話の状態管理
state = {
    "waiting_for_memo": False,      # メモ待ち
    "waiting_for_image": False,     # 画像URL待ち
    "waiting_for_ok": False,        # OK待ち
    "current_caption": "",          # 生成した投稿文
    "current_memo": "",             # やまさんのメモ
}


def send_line_message(text: str):
    """やまさんにLINEメッセージを送る"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    data = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}]
    }
    res = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers=headers,
        json=data
    )
    print(f"LINE送信: {res.status_code} - {text[:30]}...")
    return res


def generate_post_text(memo: str) -> str:
    """AIで投稿文を生成"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_prompt = """あなたはやまさん（山崎清治）のSNS投稿文を作成するアシスタントです。

やまさんのプロフィール：
- 教育・啓発活動の専門家・講演者
- NPO法人「SHOSAPO（ショサポ）」代表（「シャオサポ」は絶対に使わない）
- ハッシュタグは必ず「#SHOSAPO」（アルファベット大文字）を使う
- 啓発活動が営業になっているため、活動を発信することが重要

投稿文のルール：
- 感情はシンプルに一言だけ。例：「若い子たちの熱量、すごかった」
- 意識高い系・自己啓発っぽい表現は絶対に使わない
- 「足元を固める」「内側を整える」など意味深な表現は使わない
- 淡々と事実を書いて、感情は短く添える程度にする
- 自分の団体（SHOSAPO）の活動には自然と熱が入るという人間らしい感情はOK
- カッコつけず、素直な言葉で書く
- 関西弁をほんの少し混ぜてもOK
- 150〜250文字程度（短めでOK）
- 末尾に関連するハッシュタグを5〜8個つける（#SHOSAPO を必ず含める、#ショサポや#シャオサポは絶対に使わない）
- 「臭い」「いきってる」と感じるような表現は使わない"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": f"以下のメモをもとにInstagram投稿文を作成してください：\n\n{memo}"}]
    )
    return message.content[0].text


def download_line_image(message_id: str) -> str:
    """LINEから画像をダウンロードしてImgBBにアップロード、公開URLを返す"""
    # LINEから画像をダウンロード
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    res = requests.get(
        f"https://api-data.line.me/v2/bot/message/{message_id}/content",
        headers=headers
    )
    if res.status_code != 200:
        print(f"❌ LINE画像ダウンロード失敗: {res.status_code}")
        return ""

    # ImgBBにアップロード
    import base64
    image_b64 = base64.b64encode(res.content).decode("utf-8")
    imgbb_res = requests.post(
        "https://api.imgbb.com/1/upload",
        data={"key": os.getenv("IMGBB_API_KEY", "da96db4d1b87398aeddabec81ab6d498"), "image": image_b64}
    )
    if imgbb_res.status_code == 200:
        url = imgbb_res.json()["data"]["url"]
        print(f"✅ ImgBBアップロード完了: {url}")
        return url
    else:
        print(f"❌ ImgBBアップロード失敗: {imgbb_res.text}")
        return ""


def revise_post_text(current_caption: str, instruction: str) -> str:
    """投稿文を指示に従って修正する"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"以下のInstagram投稿文を「{instruction}」という指示に従って修正してください。ハッシュタグは#SHOSAPOを必ず含めてください。\n\n{current_caption}"
        }]
    )
    return message.content[0].text


def post_to_instagram(image_url: str, caption: str) -> bool:
    """Instagramに投稿"""
    base_url = f"https://graph.instagram.com/v21.0/{IG_USER_ID}"
    try:
        container_res = requests.post(
            f"{base_url}/media",
            data={"image_url": image_url, "caption": caption, "access_token": IG_ACCESS_TOKEN}
        )
        container_res.raise_for_status()
        container_id = container_res.json()["id"]

        publish_res = requests.post(
            f"{base_url}/media_publish",
            data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN}
        )
        publish_res.raise_for_status()
        print(f"✅ Instagram投稿完了: {publish_res.json()['id']}")
        return True
    except Exception as e:
        print(f"❌ Instagram投稿エラー: {e}")
        return False


def ask_yamasan():
    """やまさんに問いかけを送る"""
    state["waiting_for_memo"] = True
    state["waiting_for_image"] = False
    state["waiting_for_ok"] = False
    send_line_message("今日どんなことがありましたか？\n一言でOKです！")
    print("問いかけ送信完了")


@app.route("/ask", methods=["GET"])
def trigger_ask():
    """手動で問いかけをトリガーするテスト用エンドポイント"""
    ask_yamasan()
    return Response("問いかけ送信完了", status=200)


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return Response("OK", status=200)

    data = request.get_data()
    try:
        body = json.loads(data)
        for event in body.get("events", []):
            if event.get("type") == "message":
                if event["message"]["type"] == "text":
                    text = event["message"]["text"].strip()
                    handle_message(text)
                elif event["message"]["type"] == "image" and state["waiting_for_image"]:
                    message_id = event["message"]["id"]
                    send_line_message("画像を受信しました！アップロード中...📤")
                    image_url = download_line_image(message_id)
                    if image_url:
                        state["waiting_for_image"] = False
                        state["current_image_url"] = image_url
                        state["waiting_for_ok"] = True
                        send_line_message(f"アップロード完了！\n\nこの内容でInstagramに投稿しますか？\n「OK」で投稿、「もう一度」で文章を再生成します")
                    else:
                        send_line_message("画像のアップロードに失敗しました。もう一度送ってください")
    except Exception as e:
        print(f"エラー: {e}")

    return Response("OK", status=200)


def handle_message(text: str):
    """やまさんからのメッセージを処理"""
    print(f"受信: {text}")

    # メモ待ちの場合 → 投稿文を生成
    if state["waiting_for_memo"]:
        state["waiting_for_memo"] = False
        state["current_memo"] = text
        send_line_message("投稿文を生成中です...少し待ってください📝")
        caption = generate_post_text(text)
        state["current_caption"] = caption
        state["waiting_for_image"] = True
        send_line_message(f"【生成された投稿文】\n\n{caption}\n\n---\n画像のURLを送ってください\n（ImgBBなどで公開したURLを貼り付けてください）")
        return

    # 画像URL待ちの場合
    if state["waiting_for_image"]:
        if text in ["もう一度", "再生成", "やり直し"]:
            send_line_message("投稿文を再生成中です...")
            caption = generate_post_text(state["current_memo"])
            state["current_caption"] = caption
            send_line_message(f"【再生成された投稿文】\n\n{caption}\n\n---\n写真をLINEで送ってください📸\n「修正して→○○」で修正もできます")
        elif text.startswith("修正して"):
            instruction = text.replace("修正して", "").replace("→", "").strip()
            send_line_message("修正中です...")
            caption = revise_post_text(state["current_caption"], instruction)
            state["current_caption"] = caption
            send_line_message(f"【修正された投稿文】\n\n{caption}\n\n---\n写真をLINEで送ってください📸")
        elif text.startswith("この文章で投稿して"):
            custom_caption = text.replace("この文章で投稿して", "").strip()
            if custom_caption:
                state["current_caption"] = custom_caption
            state["waiting_for_image"] = False
            state["waiting_for_ok"] = False
            send_line_message(f"以下の文章で投稿します：\n\n{state['current_caption']}\n\n---\n写真をLINEで送ってください📸")
            state["waiting_for_image"] = True
        elif text.startswith("http"):
            state["waiting_for_image"] = False
            state["current_image_url"] = text
            state["waiting_for_ok"] = True
            send_line_message("この内容でInstagramに投稿しますか？\n「OK」で投稿、「もう一度」で文章を再生成\n「修正して→○○」で修正もできます")
        else:
            send_line_message("写真をLINEで送ってください📸\n\n他にできること：\n・「もう一度」→ 文章を再生成\n・「修正して→○○」→ 文章を修正\n・「この文章で投稿して→○○」→ 直接文章を指定")
        return

    # OK待ちの場合
    if state["waiting_for_ok"]:
        if text.upper() in ["OK", "ＯＫ", "ok", "おけ", "オケ"]:
            state["waiting_for_ok"] = False
            send_line_message("投稿中です...📸")
            success = post_to_instagram(state["current_image_url"], state["current_caption"])
            if success:
                send_line_message("✅ Instagramへの投稿が完了しました！")
            else:
                send_line_message("❌ 投稿に失敗しました。もう一度試してください")
        elif text in ["もう一度", "再生成", "やり直し"]:
            send_line_message("投稿文を再生成中です...")
            caption = generate_post_text(state["current_memo"])
            state["current_caption"] = caption
            send_line_message(f"【再生成された投稿文】\n\n{caption}\n\n---\n「OK」で投稿、「もう一度」で再生成\n「修正して→○○」で修正もできます")
        elif text.startswith("修正して"):
            instruction = text.replace("修正して", "").replace("→", "").strip()
            send_line_message("修正中です...")
            caption = revise_post_text(state["current_caption"], instruction)
            state["current_caption"] = caption
            send_line_message(f"【修正された投稿文】\n\n{caption}\n\n---\n「OK」で投稿、「修正して→○○」でさらに修正")
        return

    # その他のメッセージ → そのままメモとして投稿文を生成
    state["current_memo"] = text
    send_line_message("投稿文を生成中です...少し待ってください📝")
    caption = generate_post_text(text)
    state["current_caption"] = caption
    state["waiting_for_image"] = True
    state["waiting_for_ok"] = False
    send_line_message(f"【生成された投稿文】\n\n{caption}\n\n---\n写真をLINEで送ってください📸\n「もう一度」で再生成、「修正して→○○」で修正できます")


print("=== やまさんLINE Bot起動 ===")
print("スケジュールはGitHub Actionsが管理します")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

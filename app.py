import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import streamlit as st


MAX_CHARS = 15000
SAMPLE_URL = "https://www.aozora.gr.jp/cards/000879/files/92_14545.html"


def read_story(url):
    import requests
    from bs4 import BeautifulSoup

    response = requests.get(url, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    main = soup.select_one(".main_text")
    if main is None:
        raise ValueError("青空文庫の「いますぐXHTML版で読む」のURLを入力してください。")

    for tag in main.select("rt, rp"):
        tag.decompose()

    text = main.get_text(strip=True)
    if not text:
        raise ValueError("本文が空です。別の作品のURLを確認してください。")
    if len(text) > MAX_CHARS:
        raise ValueError(f"本文は{len(text):,}文字です。15,000文字以下の作品を選んでください。")

    title_tag = soup.select_one("h1.title")
    author_tag = soup.select_one("h2.author")
    title = title_tag.get_text(strip=True) if title_tag else "無題の作品"
    author = author_tag.get_text(strip=True) if author_tag else ""
    return title, author, text


def ask_gemini(key, prompt):
    import requests

    models = [
        "gemini-3.8-flash",
        "gemini-3.8-flash-lite",
        "gemini-3-flash-preview",
    ]
    last_error = ""
    for attempt in range(6):
        model = models[attempt % len(models)]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            response = requests.post(
                url,
                headers={"x-goog-api-key": key},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=45,
            )
        except requests.RequestException:
            last_error = "Geminiとの通信がタイムアウトしました。"
            time.sleep(2)
            continue

        if response.ok:
            candidates = response.json().get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                answer = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if answer.strip():
                    return answer.strip()

        last_error = f"{model}（HTTP {response.status_code}）"
        if response.status_code in (401, 403):
            break
        if response.status_code in (429, 500, 502, 503, 504):
            time.sleep(3)

    raise RuntimeError(f"Geminiから回答を取得できませんでした。{last_error}")


def make_script(key, title, text, guide, previous):
    prompt = f"""
作品『{title}』の以下の本文を、自然な日本語のナレーションで
3〜4シーンの短いアニメ脚本にしてください。
設定資料と前のパートから、人物の見た目と物語の流れを引き継いでください。
各シーンに narration（日本語）、image_prompt（英語の具体的な情景）、
scene_summary（日本語の一行要約）を入れてください。
image_promptでは過激な描写を避けてください。
出力はJSON配列のみ。例：
[{{"narration":"…","image_prompt":"…","scene_summary":"…"}}]

設定資料：{guide}
前のパート：{previous}
本文：
{text}
"""
    answer = ask_gemini(key, prompt)
    answer = re.sub(r"^```(?:json)?\s*", "", answer, flags=re.I)
    answer = re.sub(r"\s*```$", "", answer)
    scenes = json.loads(answer)
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("脚本を読み取れませんでした。もう一度お試しください。")
    for scene in scenes:
        if not isinstance(scene, dict) or not all(
            isinstance(scene.get(field), str) and scene[field].strip()
            for field in ("narration", "image_prompt")
        ):
            raise ValueError("脚本にナレーションまたは画像の説明がありません。")
    return scenes


def make_image(account, token, description, path):
    import requests

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/"
        "@cf/black-forest-labs/flux-1-schnell"
    )
    style = ", 2D anime illustration, clean lineart, flat cel shading, soft lighting"
    prompt = description + style

    for attempt in range(4):
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"prompt": prompt, "steps": 4},
            timeout=60,
        )
        if response.ok:
            path.write_bytes(base64.b64decode(response.json()["result"]["image"]))
            return
        if response.status_code == 400 and "NSFW" in response.text:
            prompt = "Quiet landscape, dramatic sky and soft light" + style
            continue
        if response.status_code == 429:
            raise RuntimeError("画像生成の利用上限に達しました。時間をおいてお試しください。")
        if response.status_code < 500:
            break
        if attempt < 3:
            time.sleep(3)

    raise RuntimeError(f"画像生成に失敗しました（HTTP {response.status_code}）。")


@st.cache_data(show_spinner=False)
def font_bytes():
    import requests

    url = "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansjp/NotoSansJP%5Bwght%5D.ttf"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.content


def title_image(title, author, path):
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1280, 720), (18, 20, 28))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 1240, 680), outline=(180, 160, 120), width=2)
    font_data = font_bytes()

    def draw_line(text, y, size, color):
        while True:
            font = ImageFont.truetype(BytesIO(font_data), size)
            box = draw.textbbox((0, 0), text, font=font)
            if box[2] - box[0] <= 1160 or size <= 16:
                break
            size -= 2
        draw.text((640, y), text, font=font, anchor="mm", fill=color)

    draw_line(title, 310, 64, (245, 245, 245))
    if author:
        draw_line(f"作：{author}", 415, 36, (210, 195, 160))
    image.save(path)


def run_ffmpeg(args):
    import imageio_ffmpeg

    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner", "-loglevel", "error", "-y",
        *[str(arg) for arg in args],
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"動画の書き出しに失敗しました。\n{detail}")


def render_scene(image, output, audio=None):
    args = ["-loop", "1", "-framerate", "24", "-i", image]
    if audio is None:
        args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "3.5"]
        video_filter = "setsar=1"
    else:
        args += ["-i", audio, "-shortest"]
        video_filter = (
            "scale=1280:800:force_original_aspect_ratio=increase,"
            "crop=1280:720:x='min(20*t,iw-1280)':y='(ih-720)/2',setsar=1"
        )

    args += [
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", video_filter, "-filter_threads", "1",
        "-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2",
        output,
    ]
    run_ffmpeg(args)


def generate(url, gemini_key, cf_account, cf_token, progress):
    from gtts import gTTS

    progress.progress(3, text="青空文庫から本文を取得しています…")
    title, author, text = read_story(url)
    chunks = [text[i:i + 7500] for i in range(0, len(text), 7500)]

    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        progress.progress(8, text=f"『{title}』のタイトル画面を作っています…")
        cover = folder / "title.png"
        title_image(title, author, cover)
        opening = folder / "000.mp4"
        render_scene(cover, opening)
        videos = [opening]

        progress.progress(15, text="登場人物と時代背景を整理しています…")
        try:
            guide = ask_gemini(
                gemini_key,
                f"作品『{title}』の設定資料を英語120語以内で作成してください。"
                f"時代・場所・登場人物の外見を記載してください。\n{text[:2500]}",
            )
        except RuntimeError:
            guide = f"Classic literature setting for {title}."

        previous = ""
        for i, chunk in enumerate(chunks):
            start = 20 + int(72 * i / len(chunks))
            progress.progress(start, text=f"パート {i + 1}/{len(chunks)} の脚本を作っています…")
            scenes = make_script(gemini_key, title, chunk, guide, previous)
            previous = "／".join(str(s.get("scene_summary", "")) for s in scenes[-3:])

            for j, scene in enumerate(scenes):
                percent = 20 + int(72 * (i + j / len(scenes)) / len(chunks))
                progress.progress(
                    percent,
                    text=f"パート {i + 1}/{len(chunks)}：シーン {j + 1}/{len(scenes)} を作っています…",
                )
                stem = f"scene_{i:02d}_{j:02d}"
                audio = folder / f"{stem}.mp3"
                image = folder / f"{stem}.png"
                video = folder / f"{stem}.mp4"

                gTTS(text=scene["narration"], lang="ja").save(str(audio))
                make_image(cf_account, cf_token, scene["image_prompt"], image)
                render_scene(image, video, audio)
                videos.append(video)
                audio.unlink()
                image.unlink()

        progress.progress(95, text="タイトルと本編をつないでいます…")
        playlist = folder / "list.txt"
        playlist.write_text(
            "".join(f"file '{video.name}'\n" for video in videos),
            encoding="utf-8",
        )
        output = folder / "finished.mp4"
        run_ffmpeg(["-f", "concat", "-safe", "1", "-i", playlist,
                    "-c", "copy", "-movflags", "+faststart", output])
        return title, output.read_bytes()


st.set_page_config(page_title="青空文庫 2Dアニメ自動生成メーカー", page_icon="🎬")
st.title("🎬 青空文庫 2Dアニメ自動生成メーカー")
st.write("15,000文字以下の作品に対応しています。青空文庫の本文URLを入力してください。")
url = st.text_input("📖 青空文庫の本文URL（XHTML版）", value=SAMPLE_URL)

if st.button("🚀 2Dアニメ動画を生成する", type="primary"):
    if not url.strip():
        st.warning("本文URLを入力してください。")
        st.stop()

    try:
        keys = [
            st.secrets[name].strip()
            for name in ("GEMINI_API_KEY", "CF_ACCOUNT_ID", "CF_API_TOKEN")
        ]
        if not all(keys):
            raise ValueError("空の設定値があります。")
    except (FileNotFoundError, KeyError, ValueError):
        st.error("StreamlitのSecretsでGeminiとCloudflareの設定を確認してください。")
        st.stop()

    progress = st.progress(0, text="準備しています…")
    try:
        title, video = generate(url.strip(), *keys, progress)
        progress.progress(100, text=f"『{title}』の動画が完成しました。")
        st.video(video)
        filename = re.sub(r'[\\/*?:"<>|]', "", title) or "青空文庫"
        st.download_button("📥 完成したMP4を保存", video, f"{filename}_2Dアニメ.mp4", "video/mp4")
    except Exception as error:
        progress.empty()
        st.error(str(error))

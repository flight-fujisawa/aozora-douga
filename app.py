import PIL.Image
import PIL.ImageDraw
import PIL.ImageFont
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

import os
import re
import json
import time
import base64
import tempfile
import subprocess
import requests
import imageio_ffmpeg
import streamlit as st
from bs4 import BeautifulSoup
from gtts import gTTS
from google import genai
from moviepy.editor import ImageClip, AudioFileClip, AudioClip, VideoClip, concatenate_videoclips

# ==========================================
# 1. APIキーの安全な読み込み
# ==========================================
try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
    CF_ACCOUNT_ID = st.secrets["CF_ACCOUNT_ID"]
    CF_API_TOKEN = st.secrets["CF_API_TOKEN"]
except Exception:
    GEMINI_API_KEY = ""
    CF_ACCOUNT_ID = ""
    CF_API_TOKEN = ""

MAX_CHARS = 15000

# ==========================================
# 2. 青空文庫の取得と文字数チェック
# ==========================================
def fetch_and_check_aozora(url, chunk_size=7500):
    response = requests.get(url, timeout=15)
    response.encoding = response.apparent_encoding
    soup = BeautifulSoup(response.text, 'html.parser')
    
    title_tag = soup.find('h1', class_='title')
    title = title_tag.get_text(strip=True) if title_tag else "無題の作品"
    
    author_tag = soup.find('h2', class_='author')
    author = author_tag.get_text(strip=True) if author_tag else ""
    
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
    
    main_text_div = soup.find('div', class_='main_text')
    if not main_text_div:
        raise ValueError("本文が見つかりませんでした。青空文庫の「いますぐXHTML版で読む」のURLか確認してください。")
        
    for rt in main_text_div.find_all(['rt', 'rp']):
        rt.decompose()
        
    full_text = main_text_div.get_text(strip=True)
    char_count = len(full_text)
    
    if char_count > MAX_CHARS:
        raise ValueError(
            f"作品『{title}』は総文字数が【{char_count:,}文字】あるため、上限（{MAX_CHARS:,}文字以下）を超えています。\n"
            f"短編作品のURLを入力してください。"
        )
        
    chunks = [full_text[i:i + chunk_size] for i in range(0, char_count, chunk_size)]
    return title, author, safe_title, full_text, chunks

# ==========================================
# 3. オープニングクレジット作成（タイトル＆作者名）
# ==========================================
def get_japanese_font(work_dir, size):
    font_path = os.path.join(work_dir, "NotoSansJP-Bold.ttf")
    if not os.path.exists(font_path):
        domain = "raw.githubusercontent." + "com"
        font_url = f"https://{domain}/google/fonts/main/ofl/notosansjp/NotoSansJP%5Bwght%5D.ttf"
        res = requests.get(font_url, timeout=20)
        if res.status_code == 200:
            with open(font_path, "wb") as f:
                f.write(res.content)
    try:
        return PIL.ImageFont.truetype(font_path, size)
    except Exception:
        return PIL.ImageFont.load_default()

def create_title_clip(title, author, work_dir):
    video_path = os.path.join(work_dir, "part_000_title.mp4")
    img_path = os.path.join(work_dir, "opening_title.png")
    
    width, height = 1280, 720
    img = PIL.Image.new("RGB", (width, height), color=(18, 20, 28))
    draw = PIL.ImageDraw.Draw(img)
    draw.rectangle([40, 40, width - 40, height - 40], outline=(180, 160, 120), width=2)
    
    title_font = get_japanese_font(work_dir, 64 if len(title) <= 16 else 46)
    author_font = get_japanese_font(work_dir, 36)
        
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    title_w = title_bbox[2] - title_bbox[0]
    title_h = title_bbox[3] - title_bbox[1]
    title_x = (width - title_w) // 2
    title_y = (height - title_h) // 2 - 40
    draw.text((title_x, title_y), title, font=title_font, fill=(245, 245, 245))
    
    if author:
        author_text = f"作：{author}"
        author_bbox = draw.textbbox((0, 0), author_text, font=author_font)
        author_w = author_bbox[2] - author_bbox[0]
        author_x = (width - author_w) // 2
        author_y = title_y + title_h + 50
        draw.text((author_x, author_y), author_text, font=author_font, fill=(210, 195, 160))
        
    img.save(img_path)
    
    duration = 3.5
    silent_audio = AudioClip(lambda t: [0, 0], duration=duration, fps=44100)
    clip = ImageClip(img_path).set_duration(duration).set_audio(silent_audio)
    clip.write_videofile(video_path, fps=24, codec="libx264", audio_codec="aac", logger=None)
    clip.close()
    return video_path

# ==========================================
# 4. AI処理（設定資料・脚本・画像生成）
# ==========================================
def create_story_bible(client, title, sample_text):
    prompt = f"""
あなたはアニメ監督です。これから小説『{title}』を2Dアニメ動画化します。
作品名と冒頭テキストをもとに、全編を通してイラストの時代考証やキャラクターの見た目がブレないようにするための「英語の設定資料（ビジュアルガイド）」を120単語以内で作成してください。
1. Era & Setting: 時代背景、国、建築様式、服装の特徴
2. Main Characters Visuals: 登場人物の髪色・服装・年齢感などの固定特徴

作品名: {title}
冒頭テキスト:
{sample_text[:2500]}
"""
    for attempt in range(5):
        try:
            response = client.models.generate_content(model="gemini-3.8-flash", contents=prompt)
            return response.text.strip()
        except Exception:
            time.sleep(5)
    return f"Story: {title}, classic literature setting."

def generate_script(client, text_chunk, title, story_bible, prev_summary):
    prompt = f"""
あなたは優秀なアニメ監督兼脚本家です。以下の【作品設定資料】と【直前までのあらすじ】を守りながら、今回の【対象テキスト】を令和の自然な口語体に翻訳・再構成し、動画用の脚本（3〜4シーン）をJSON形式で作成してください。

【作品名】{title}
【作品設定資料】{story_bible}
【直前までのあらすじ】{prev_summary}

【重要ルール】
1. 出力は必ずJSON形式のリストのみ（```json などは不要）。
2. `image_prompt` は、そのシーンのナレーション内容と一致する具体的な情景（「誰が・どんな服で・どこにいて・何をしているか」）を英語で記述してください。画風指定は不要です。血・死体・拷問などの過激な単語は避けてください。
3. 各シーンの要約を `scene_summary` に入れてください。

[
  {{
    "scene": 1,
    "image_prompt": "具体的なシーンの描写（英語のみ）",
    "narration": "令和の口語体に翻訳したナレーション・セリフ",
    "scene_summary": "このシーンの1行要約（日本語）"
  }}
]

【対象テキスト】
{text_chunk}
"""
    for attempt in range(5):
        try:
            response = client.models.generate_content(model="gemini-3.8-flash", contents=prompt)
            raw_text = response.text.strip()
            raw_text = re.sub(r'^```json\s*', '', raw_text)
            raw_text = re.sub(r'\s*```$', '', raw_text)
            return json.loads(raw_text)
        except Exception:
            time.sleep(8)
    raise RuntimeError("AIサーバーが混雑しています。少し時間を置いてから再度お試しください。")

def generate_image_cloudflare(cf_id, cf_token, scene_description, save_path):
    model_name = "@cf/black-forest-labs/flux-1-schnell"
    domain = "api." + "cloudflare." + "com"
    url = f"https://{domain}/client/v4/accounts/{cf_id}/ai/run/{model_name}"
    headers = {"Authorization": f"Bearer {cf_token}"}
    
    full_prompt = f"{scene_description}, 2D anime illustration style, clean lineart, flat cel shading, light novel illustration, soft lighting"
    payload = {"prompt": full_prompt, "steps": 4}
    
    for attempt in range(4):
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        if response.status_code == 200:
            image_base64 = response.json()["result"]["image"]
            with open(save_path, "wb") as f:
                f.write(base64.b64decode(image_base64))
            return
        elif response.status_code == 400 and "NSFW" in response.text:
            safe_payload = {
                "prompt": "dramatic sky over a classic landscape, symbolic light and shadow, 2D anime illustration style, clean lineart, flat cel shading, light novel illustration",
                "steps": 4
            }
            retry_res = requests.post(url, headers=headers, json=safe_payload, timeout=60)
            if retry_res.status_code == 200:
                image_base64 = retry_res.json()["result"]["image"]
                with open(save_path, "wb") as f:
                    f.write(base64.b64decode(image_base

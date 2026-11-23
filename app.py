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
            response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
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
            response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
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
                    f.write(base64.b64decode(image_base64))
                return
        elif response.status_code == 429 or "limit" in response.text.lower():
            raise RuntimeError("本日の無料画像生成上限に達しました。明日またお試しください！")
        else:
            time.sleep(5)
    raise RuntimeError("画像サーバーの混雑が解消されませんでした。")

def create_part_video(cf_id, cf_token, scenes, part_num, total_parts, work_dir, update_progress):
    part_video_path = os.path.join(work_dir, f"part_{part_num:03d}.mp4")
    clips = []
    
    part_start_pct = 25 + int(65 * ((part_num - 1) / total_parts))
    part_end_pct = 25 + int(65 * (part_num / total_parts))
    
    for idx, scene in enumerate(scenes):
        current_pct = part_start_pct + int((part_end_pct - part_start_pct) * (idx / len(scenes)))
        update_progress(current_pct, f"🎨 パート {part_num}/{total_parts} - シーン {idx+1}/{len(scenes)} の音声と2Dイラストを生成中...")
        
        audio_path = os.path.join(work_dir, f"p{part_num}_s{idx+1}.mp3")
        tts = gTTS(text=scene['narration'], lang='ja')
        tts.save(audio_path)
        
        image_path = os.path.join(work_dir, f"p{part_num}_s{idx+1}.png")
        generate_image_cloudflare(cf_id, cf_token, scene['image_prompt'], image_path)
                
        audio_clip = AudioFileClip(audio_path)
        base_img = ImageClip(image_path).resize(height=800)
        img_array = base_img.get_frame(0)
        w = base_img.w
        
        def make_frame(t, current_img=img_array, max_width=w):
            x = int(20 * t)
            max_x = max_width - 1280
            if x > max_x: x = max_x
            if x < 0: x = 0
            return current_img[40:760, x:x+1280]
            
        video_clip = VideoClip(make_frame, duration=audio_clip.duration).set_audio(audio_clip)
        clips.append(video_clip)
        
    update_progress(part_end_pct, f"💾 パート {part_num}/{total_parts} の動画パーツを書き出し中...")
    part_video = concatenate_videoclips(clips, method="compose")
    part_video.write_videofile(part_video_path, fps=24, codec="libx264", audio_codec="aac", logger=None)
    
    part_video.close()
    for c in clips:
        c.close()
        
    return part_video_path

def merge_videos_fast(chapter_files, output_path, work_dir):
    list_file_path = os.path.join(work_dir, "concat_list.txt")
    with open(list_file_path, "w", encoding="utf-8") as f:
        for file_path in chapter_files:
            abs_path = os.path.abspath(file_path).replace("\\", "/")
            f.write(f"file '{abs_path}'\n")
            
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg_exe, "-y", "-f", "concat", "-safe", "0", "-i", list_file_path, "-c", "copy", output_path]
    subprocess.run(cmd, check=True)

# ==========================================
# 5. Webアプリ画面（UI）
# ==========================================
st.set_page_config(page_title="青空文庫 2Dアニメ自動生成メーカー", page_icon="🎬", layout="centered")

st.title("🎬 青空文庫 2Dアニメ自動生成メーカー")
st.markdown(f"""
青空文庫の短編小説（**{MAX_CHARS:,}文字以下**）の本文URLを入力してボタンを押すと、約1〜2分で2Dアニメ動画が完成します。  
※完成するまではこの画面を開いたままお待ちください。
""")

default_sample_url = "https://" + "www.aozora.gr.jp/cards/000879/files/92_14545.html"
target_url = st.text_input("📖 青空文庫の本文URL（XHTML版）", value=default_sample_url)

if st.button("🚀 2Dアニメ動画を生成する", type="primary", use_container_width=True):
    if not GEMINI_API_KEY or not CF_ACCOUNT_ID or not CF_API_TOKEN:
        st.error("APIキーが設定されていません。管理画面のSecretsを設定してください。")
        st.stop()
        
    if not target_url.strip():
        st.error("URLを入力してください。")
        st.stop()
        
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    progress_bar = st.progress(0, text="準備中... (0%)")
    status_box = st.empty()
    
    def update_progress(percent, message):
        clamped = min(max(int(percent), 0), 100)
        progress_bar.progress(clamped / 100.0, text=f"進行状況: {clamped}% － {message}")
        status_box.info(message)
    
    with tempfile.TemporaryDirectory() as work_dir:
        try:
            update_progress(5, "📖 青空文庫からテキストを取得し、文字数をチェック中...")
            title, author, safe_title, full_text, chunks = fetch_and_check_aozora(target_url.strip(), chunk_size=7500)
            
            author_disp = f"（作：{author}）" if author else ""
            st.success(f"✅ 受付完了：『{title}』{author_disp}（{len(full_text):,}文字 / 全 {len(chunks)} パート）")
            
            update_progress(12, "🎬 オープニングタイトル画面を作成中...")
            title_video = create_title_clip(title, author, work_dir)
            chapter_files = [title_video]
            
            update_progress(20, "📚 作品の世界観・キャラクター設定資料を作成中...")
            story_bible = create_story_bible(client, title, full_text)
            
            current_summary = "物語の始まり。"
            total_parts = len(chunks)
            
            for i in range(total_parts):
                part_num = i + 1
                part_pct = 25 + int(65 * ((part_num - 1) / total_parts))
                update_progress(part_pct, f"🤖 パート {part_num}/{total_parts} の脚本をAIが執筆中...")
                script_data = generate_script(client, chunks[i], title, story_bible, current_summary)
                
                summaries = [s.get('scene_summary', '') for s in script_data if s.get('scene_summary')]
                if summaries:
                    current_summary = " / ".join(summaries[-3:])
                    
                video_file = create_part_video(CF_ACCOUNT_ID, CF_API_TOKEN, script_data, part_num, total_parts, work_dir, update_progress)
                chapter_files.append(video_file)
                
            update_progress(95, "🎬 タイトルと本編を結合して仕上げています...")
            output_filename = f"{safe_title}_2Dアニメ.mp4"
            output_path = os.path.join(work_dir, output_filename)
            merge_videos_fast(chapter_files, output_path, work_dir)
            
            with open(output_path, "rb") as f:
                video_bytes = f.read()
                
            update_progress(100, f"🎉 完成しました！『{title}』{author_disp}")
            status_box.success(f"🎉 完成しました！『{title}』{author_disp}")
            st.video(video_bytes)
            st.download_button(
                label="📥 完成したMP4動画をダウンロード",
                data=video_bytes,
                file_name=output_filename,
                mime="video/mp4",
                use_container_width=True
            )
            
        except ValueError as ve:
            progress_bar.empty()
            status_box.empty()
            st.warning(f"⚠️ {ve}")
        except Exception as e:
            progress_bar.empty()
            status_box.empty()
            st.error(f"❌ エラーが発生しました: {e}")

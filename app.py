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
import hashlib
import threading
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
BASE_CACHE_DIR = "aozora_video_cache"
os.makedirs(BASE_CACHE_DIR, exist_ok=True)

# ==========================================
# 2. 青空文庫の取得と文字数チェック
# ==========================================
def fetch_and_check_aozora(url, chunk_size=4000):
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
    font_path = os.path.join(BASE_CACHE_DIR, "NotoSansJP-Bold.ttf")
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
    if os.path.exists(video_path) and os.path.getsize(video_path) > 5000:
        return video_path

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
# 4. AI処理（設定資料・脚本・画像生成・リトライ強化）
# ==========================================
def get_or_create_story_bible(client, title, sample_text, work_dir):
    bible_path = os.path.join(work_dir, "bible.txt")
    if os.path.exists(bible_path):
        with open(bible_path, "r", encoding="utf-8") as f:
            return f.read()

    prompt = f"""
あなたはアニメ監督です。これから小説『{title}』を2Dアニメ動画化します。
作品名と冒頭テキストをもとに、全編を通してイラストの時代考証やキャラクターの見た目がブレないようにするための「英語の設定資料（ビジュアルガイド）」を150単語以内で作成してください。
1. Era & Setting: 時代背景、国、建築様式、服装の特徴
2. Main Characters Visuals: 登場人物の髪色・服装・年齢感・動物の見た目などの固定特徴

作品名: {title}
冒頭テキスト:
{sample_text[:3000]}
"""
    for attempt in range(8):
        try:
            response = client.models.generate_content(model="gemini-3.8-flash", contents=prompt)
            bible = response.text.strip()
            with open(bible_path, "w", encoding="utf-8") as f:
                f.write(bible)
            return bible
        except Exception:
            time.sleep(5 * (attempt + 1))
    return f"Story: {title}, classic literature setting."

def generate_script(client, text_chunk, title, story_bible, prev_summary):
    prompt = f"""
あなたは優秀なアニメ監督兼脚本家です。以下の【作品設定資料】と【直前までのあらすじ】を守りながら、今回の【対象テキスト】を令和の自然な口語体に翻訳・再構成し、動画用の脚本（4〜6シーン程度）をJSON形式で作成してください。

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
    for attempt in range(8):
        try:
            response = client.models.generate_content(model="gemini-3.8-flash", contents=prompt)
            raw_text = response.text.strip()
            raw_text = re.sub(r'^```json\s*', '', raw_text)
            raw_text = re.sub(r'\s*```$', '', raw_text)
            return json.loads(raw_text)
        except Exception:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("AIサーバーの混雑が続いており、脚本を作成できませんでした。")

def generate_image_cloudflare(cf_id, cf_token, scene_description, save_path):
    if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
        return

    model_name = "@cf/black-forest-labs/flux-1-schnell"
    domain = "api." + "cloudflare." + "com"
    url = f"https://{domain}/client/v4/accounts/{cf_id}/ai/run/{model_name}"
    headers = {"Authorization": f"Bearer {cf_token}"}
    
    full_prompt = f"{scene_description}, 2D anime illustration style, clean lineart, flat cel shading, light novel illustration, soft lighting"
    payload = {"prompt": full_prompt, "steps": 4}
    
    for attempt in range(5):
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
            # 一時的なサーバー混雑時は待機して自動リトライ
            time.sleep(5 * (attempt + 1))
            
    raise RuntimeError("画像サーバーの混雑が解消されませんでした。")

def create_part_video(cf_id, cf_token, scenes, part_num, total_parts, work_dir, update_status):
    part_video_path = os.path.join(work_dir, f"part_{part_num:03d}.mp4")
    if os.path.exists(part_video_path) and os.path.getsize(part_video_path) > 10000:
        return part_video_path

    clips = []
    for idx, scene in enumerate(scenes):
        progress_val = ((part_num - 1) + (idx / len(scenes))) / total_parts
        update_status("running", f"🎨 パート {part_num}/{total_parts} - シーン {idx+1}/{len(scenes)} の音声と2Dイラストを生成中...", progress_val)
        
        audio_path = os.path.join(work_dir, f"p{part_num}_s{idx+1}.mp3")
        if not os.path.exists(audio_path):
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
        
    update_status("running", f"💾 パート {part_num}/{total_parts} の動画データを保存中...", part_num / total_parts)
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
# 5. バックグラウンド自動完走ワーカー（画面を閉じても裏で動き続ける）
# ==========================================
def background_worker(url, work_dir, status_file):
    def update_status(state, message, progress=0.0, output_file="", title="", author=""):
        data = {
            "state": state,
            "message": message,
            "progress": progress,
            "output_file": output_file,
            "title": title,
            "author": author,
            "updated_at": time.time()
        }
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        update_status("running", "📖 青空文庫からテキストを取得・解析中...", 0.02)
        title, author, safe_title, full_text, chunks = fetch_and_check_aozora(url, chunk_size=4000)
        
        output_filename = f"{safe_title}_2Dアニメ.mp4"
        final_output_path = os.path.join(work_dir, output_filename)
        
        # すでに全編完成済みの場合は即完了にする
        if os.path.exists(final_output_path) and os.path.getsize(final_output_path) > 10000:
            update_status("done", f"🎉 完成済みです！『{title}』", 1.0, final_output_path, title, author)
            return

        update_status("running", "🎬 オープニングタイトル画面を作成中...", 0.05, "", title, author)
        title_video = create_title_clip(title, author, work_dir)
        chapter_files = [title_video]
        
        update_status("running", "📚 作品の世界観・キャラクター設定資料を作成中...", 0.08, "", title, author)
        story_bible = get_or_create_story_bible(client, title, full_text, work_dir)
        
        summary_path = os.path.join(work_dir, "last_summary.txt")
        if os.path.exists(summary_path):
            with open(summary_path, "r", encoding="utf-8") as f:
                current_summary = f.read()
        else:
            current_summary = "物語の始まり。"
            
        total_parts = len(chunks)
        for i in range(total_parts):
            part_num = i + 1
            part_video_path = os.path.join(work_dir, f"part_{part_num:03d}.mp4")
            
            # すでにこのパートが完成していればスキップ（途中再開）
            if os.path.exists(part_video_path) and os.path.getsize(part_video_path) > 10000:
                chapter_files.append(part_video_path)
                update_status("running", f"⏭️ パート {part_num}/{total_parts} は作成済みのためスキップ...", part_num / total_parts, "", title, author)
                continue
                
            update_status("running", f"🤖 パート {part_num}/{total_parts} の脚本をAIが執筆中...", (part_num - 0.8) / total_parts, "", title, author)
            script_data = generate_script(client, chunks[i], title, story_bible, current_summary)
            
            summaries = [s.get('scene_summary', '') for s in script_data if s.get('scene_summary')]
            if summaries:
                current_summary = " / ".join(summaries[-3:])
                with open(summary_path, "w", encoding="utf-8") as f:
                    f.write(current_summary)
                    
            video_file = create_part_video(CF_ACCOUNT_ID, CF_API_TOKEN, script_data, part_num, total_parts, work_dir, update_status)
            chapter_files.append(video_file)
            
        update_status("running", "🎬 タイトルと本編を結合して仕上げています...", 0.98, "", title, author)
        merge_videos_fast(chapter_files, final_output_path, work_dir)
        
        update_status("done", f"🎉 完成しました！『{title}』", 1.0, final_output_path, title, author)
        
    except ValueError as ve:
        update_status("warning", str(ve))
    except Exception as e:
        update_status("error", f"エラーが発生しました（再度ボタンを押すと続きから再開できます）: {e}")

# ==========================================
# 6. Webアプリ画面（マルチデバイス・自動復帰UI）
# ==========================================
st.set_page_config(page_title="青空文庫 2Dアニメ自動生成メーカー", page_icon="🎬", layout="centered")

st.title("🎬 青空文庫 2Dアニメ自動生成メーカー")
st.markdown(f"""
青空文庫の短編小説（**{MAX_CHARS:,}文字以下**）のURLを入力してください。  
**バックグラウンド自動生成**に対応しているため、途中で画面を閉じたり、別のスマホ・PCから同じURLを開き直しても、裏側で作成を続けて結果を表示します。
""")

default_sample_url = "https://" + "www.aozora.gr.jp/cards/000879/files/92_14545.html"
target_url = st.text_input("📖 青空文庫の本文URL（XHTML版）", value=default_sample_url)

# URLごとに一意の作業フォルダを決定
clean_url = target_url.strip()
url_hash = hashlib.md5(clean_url.encode("utf-8")).hexdigest()[:12]
work_dir = os.path.join(BASE_CACHE_DIR, url_hash)
os.makedirs(work_dir, exist_ok=True)
status_file = os.path.join(work_dir, "status.json")

# 現在のステータスを確認
current_status = None
if os.path.exists(status_file):
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            current_status = json.load(f)
    except Exception:
        current_status = None

# 実行中が長時間（5分以上）更新されていない場合は通信途絶とみなして再開可能にする
is_running = False
if current_status and current_status.get("state") == "running":
    if time.time() - current_status.get("updated_at", 0) < 300:
        is_running = True

if st.button("🚀 2Dアニメ動画を生成 / 進捗を確認する", type="primary", use_container_width=True):
    if not GEMINI_API_KEY or not CF_ACCOUNT_ID or not CF_API_TOKEN:
        st.error("APIキーが設定されていません。管理画面のSecretsを設定してください。")
        st.stop()
    if not clean_url:
        st.error("URLを入力してください。")
        st.stop()
        
    if not is_running:
        # 裏側でスレッドを起動して自動完走させる
        t = threading.Thread(target=background_worker, args=(clean_url, work_dir, status_file), daemon=True)
        t.start()
        time.sleep(0.5)
        st.rerun()

# ステータスに応じた自動画面更新と結果表示
if os.path.exists(status_file):
    with open(status_file, "r", encoding="utf-8") as f:
        status = json.load(f)
        
    state = status.get("state")
    msg = status.get("message", "")
    prog = float(status.get("progress", 0.0))
    title = status.get("title", "")
    author = status.get("author", "")
    
    if title:
        author_disp = f"（作：{author}）" if author else ""
        st.subheader(f"📖 『{title}』{author_disp}")
        
    if state == "running":
        st.progress(min(max(prog, 0.0), 1.0))
        st.info(f"{msg}\n\n※画面を閉じても裏側で生成は続きます。別のデバイスから同じURLを開いても進捗を確認できます。")
        time.sleep(3)
        st.rerun()
        
    elif state == "done":
        st.progress(1.0)
        st.success(msg)
        out_path = status.get("output_file", "")
        if out_path and os.path.exists(out_path):
            with open(out_path, "rb") as f:
                video_bytes = f.read()
            st.video(video_bytes)
            st.download_button(
                label="📥 完成したMP4動画をダウンロード",
                data=video_bytes,
                file_name=os.path.basename(out_path),
                mime="video/mp4",
                use_container_width=True
            )
            
    elif state == "warning":
        st.warning(f"⚠️ {msg}")
        
    elif state == "error":
        st.error(f"❌ {msg}")

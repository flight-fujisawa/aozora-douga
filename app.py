        generate_image_cloudflare(cf_id, cf_token, scene['image_prompt'], image_path)
                
        scene_file = os.path.join(work_dir, f"p{part_num}_s{idx+1}.mp4")
        render_scene(image_path, scene_file, audio_path)
        scene_files.append(scene_file)

    update_progress(part_end_pct, f"💾 パート {part_num}/{total_parts} の動画を結合中...")
    merge_videos_fast(scene_files, part_video_path, work_dir)
    return part_video_path

def merge_videos_fast(chapter_files, output_path, work_dir):
    import imageio_ffmpeg

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
    try:
        GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"].strip()
        CF_ACCOUNT_ID = st.secrets["CF_ACCOUNT_ID"].strip()
        CF_API_TOKEN = st.secrets["CF_API_TOKEN"].strip()
    except (FileNotFoundError, KeyError):
        st.error("APIキーが設定されていません。管理画面のSecretsを設定してください。")
        st.stop()
    if not GEMINI_API_KEY or not CF_ACCOUNT_ID or not CF_API_TOKEN:
        st.error("APIキーが設定されていません。管理画面のSecretsを設定してください。")
        st.stop()
        
    if not target_url.strip():
        st.error("URLを入力してください。")
        st.stop()
        
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
            
            update_progress(18, "🔍 利用可能な最新AIモデルを特定中...")
            active_model = detect_active_gemini_model(GEMINI_API_KEY)
            
            update_progress(22, f"📚 世界観・キャラクター設定資料を作成中（使用AI: {active_model.replace('models/', '')}）...")
            story_bible = create_story_bible(GEMINI_API_KEY, active_model, title, full_text)
            
            current_summary = "物語の始まり。"
            total_parts = len(chunks)
            
            for i in range(total_parts):
                part_num = i + 1
                part_pct = 25 + int(65 * ((part_num - 1) / total_parts))
                update_progress(part_pct, f"🤖 パート {part_num}/{total_parts} の脚本をAIが執筆中...")
                script_data = generate_script(GEMINI_API_KEY, active_model, chunks[i], title, story_bible, current_summary)
                
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
            st.error(f"❌ {e}")

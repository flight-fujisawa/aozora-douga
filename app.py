def call_gemini_direct(api_key, model_full_name, prompt):
    domain = "generativelanguage.googleapis." + "com"
    models = [model_full_name, "models/gemini-3.8-flash-lite", "models/gemini-3-flash-preview"]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    last_err = ""
    
    for attempt in range(6):
        target_model = models[attempt % len(models)]
        url = f"https://{domain}/v1beta/{target_model}:generateContent?key={api_key}"
        try:
            res = requests.post(url, json=payload, timeout=30)
            if res.status_code == 200:
                data = res.json()
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            elif res.status_code in (429, 500, 502, 503, 504):
                last_err = f"[{target_model} HTTP {res.status_code}]"
                time.sleep(4)
            else:
                last_err = f"[{target_model} HTTP {res.status_code}] {res.text[:200]}"
        except Exception as e:
            last_err = str(e)
            time.sleep(3)
            
    raise RuntimeError(f"AIサーバーの混雑が続いています: {last_err}")

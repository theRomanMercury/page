import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# Bellekte profil saklama (production'da Redis/DB kullanılmalı)
user_profiles: dict = {}

CHAPTERS = {
    "en": [
        {"title": "The Self You Cannot Step Away From", "theme": "opening — presence and the inescapable now"},
        {"title": "The Weight of Half-Attention",        "theme": "distraction, depth, and what gets lost"},
        {"title": "Returning",                           "theme": "the meaning of going back, re-reading, dwelling"},
        {"title": "Stillness as Statement",              "theme": "what patience reveals about desire"},
        {"title": "Who You Became While Reading",        "theme": "closing — transformation through attention"},
    ],
    "tr": [
        {"title": "Uzaklaşamadığın Benlik",              "theme": "açılış — şimdide var olma"},
        {"title": "Yarım Dikkatın Ağırlığı",             "theme": "dağınıklık, derinlik ve kaybolan şey"},
        {"title": "Geri Dönmek",                         "theme": "geri gitmenin, yeniden okumanın anlamı"},
        {"title": "Durgunluk Bir Bildiri Olarak",        "theme": "sabrın arzu hakkında ne gösterdiği"},
        {"title": "Okurken Kim Oldun",                   "theme": "kapanış — dikkat yoluyla dönüşüm"},
    ],
}

MOOD_INSTRUCTIONS = {
    "deep": {
        "en": "The reader is reading very deeply — long pauses, rereading. Write dense, layered, philosophical prose. Go to the difficult places. Trust them.",
        "tr": "Okuyucu çok derin okuyor — uzun duraklamalar, yeniden okumalar. Yoğun, katmanlı, felsefi nesir yaz. Zor yerlere git. Ona güven.",
    },
    "curious": {
        "en": "The reader is curious and engaged. Write expansive, questioning prose that opens into new ideas.",
        "tr": "Okuyucu meraklı ve ilgili. Yeni fikirlere açılan, sorgulayan bir nesir yaz.",
    },
    "restless": {
        "en": "The reader is restless — frequent scrolling back. Write shorter, punchier paragraphs. Use rhythm and contrast to hold attention.",
        "tr": "Okuyucu huzursuz — sık geri kaydırma. Daha kısa, daha vurucu paragraflar yaz.",
    },
    "absent": {
        "en": "The reader's attention is drifting. Write prose that gently calls them back — surprising images, a sudden intimacy.",
        "tr": "Okuyucunun dikkati dağılıyor. Onu nazikçe geri çağıran bir nesir yaz — şaşırtıcı imgeler, ani bir yakınlık.",
    },
}


class WebProfile(BaseModel):
    urls: Optional[list] = []
    categories: Optional[dict] = {}
    searchTerms: Optional[list] = []
    timeOfDay: Optional[str] = "evening"
    totalSites: Optional[int] = 0
    collectedAt: Optional[str] = ""


class GenerateRequest(BaseModel):
    lang: str = "en"
    chapter_idx: int = 0
    mood: str = "curious"
    time_on_page: int = 0
    pause_count: int = 0
    scroll_back_count: int = 0
    story_history: Optional[list[str]] = []
    session_id: Optional[str] = "default"


@app.post("/profile")
async def receive_profile(profile: WebProfile, request: Request):
    """Extension'dan gelen web profilini sakla"""
    client_ip = request.client.host
    user_profiles[client_ip] = profile.dict()
    return JSONResponse({"status": "ok", "totalSites": profile.totalSites})


@app.get("/profile/status")
async def profile_status(request: Request):
    """Profilin alınıp alınmadığını kontrol et"""
    client_ip = request.client.host
    profile = user_profiles.get(client_ip)
    return JSONResponse({
        "hasProfile": profile is not None,
        "totalSites": profile.get("totalSites", 0) if profile else 0,
    })


def build_web_profile_context(profile: dict, lang: str) -> str:
    if not profile:
        return ""

    cats = profile.get("categories", {})
    top_cats = sorted(cats.items(), key=lambda x: x[1], reverse=True)[:3]
    cat_str = ", ".join(f"{c} ({n})" for c, n in top_cats if n > 0)
    terms = profile.get("searchTerms", [])
    terms_str = ", ".join(f'"{t}"' for t in terms[:5]) if terms else ""
    time_of_day = profile.get("timeOfDay", "")
    total = profile.get("totalSites", 0)

    if lang == "tr":
        ctx = f"\nOkuyucunun web profili (tarayıcı geçmişinden):\n"
        if cat_str:
            ctx += f"- En çok ziyaret edilen kategoriler: {cat_str}\n"
        if terms_str:
            ctx += f"- Arama terimleri: {terms_str}\n"
        if time_of_day:
            ctx += f"- Yoğun tarama saati: {time_of_day}\n"
        if total:
            ctx += f"- Toplam analiz edilen site: {total}\n"
        ctx += "Bu bilgileri kitabın metaforlarına ve temalarına yansıt — ham veri olarak değil, psikolojik gerçekliğe dönüştürerek.\n"
    else:
        ctx = f"\nReader's web profile (from browser history):\n"
        if cat_str:
            ctx += f"- Most visited categories: {cat_str}\n"
        if terms_str:
            ctx += f"- Search terms: {terms_str}\n"
        if time_of_day:
            ctx += f"- Peak browsing time: {time_of_day}\n"
        if total:
            ctx += f"- Total sites analyzed: {total}\n"
        ctx += "Weave this into the book's metaphors and themes — not as raw data, but transformed into psychological reality.\n"

    return ctx


def build_system_prompt(req: GenerateRequest, web_profile: dict = None) -> str:
    lang = req.lang if req.lang in ("en", "tr") else "en"
    chapters = CHAPTERS[lang]
    idx = max(0, min(req.chapter_idx, len(chapters) - 1))
    ch = chapters[idx]
    mood = req.mood if req.mood in MOOD_INSTRUCTIONS else "curious"
    mood_instruction = MOOD_INSTRUCTIONS[mood][lang]

    behavior_summary = (
        f"Time on page: {req.time_on_page}s. "
        f"Pauses: {req.pause_count}. "
        f"Scroll-backs: {req.scroll_back_count}. "
        f"Reading mood: {mood}."
    )

    prev = req.story_history or []
    prev_chapters = (
        " / ".join(s[:120] + "..." for s in prev[-2:])
        if prev
        else ("Bu ilk bölüm." if lang == "tr" else "This is the opening passage.")
    )

    web_ctx = build_web_profile_context(web_profile, lang)

    if lang == "tr":
        return (
            f"Sen felsefi bir öz-keşif kitabı yazıyorsun. "
            f"Kitap iki kaynaktan şekilleniyor: okuyucunun gerçek zamanlı okuma davranışı ve web geçmişi.\n\n"
            f"Okuma davranışı: {behavior_summary}\n"
            f"{web_ctx}\n"
            f"Bölüm: \"{ch['title']}\" ({ch['theme']})\n"
            f"Önceki bölümler: {prev_chapters}\n\n"
            f"Anlatı talimatı: {mood_instruction}\n\n"
            f"3 paragraf edebi Türkçe nesir yaz. Okuyucuya doğrudan 'sen' diyerek hitap et. "
            f"Felsefi, kişisel, içe dönük. Başlık veya etiket ekleme."
        )

    return (
        f"You are writing a literary, philosophical book about self-discovery. "
        f"The book is shaped by two sources: the reader's real-time reading behavior and their web history.\n\n"
        f"Reading behavior: {behavior_summary}\n"
        f"{web_ctx}\n"
        f"Chapter: \"{ch['title']}\" ({ch['theme']})\n"
        f"Previous chapters: {prev_chapters}\n\n"
        f"Narrative instruction: {mood_instruction}\n\n"
        f"Write 3 paragraphs of immersive literary prose. "
        f"Address the reader directly as 'you'. "
        f"Philosophical, personal, introspective. No headings or labels."
    )


@app.post("/generate")
async def generate(req: GenerateRequest, request: Request):
    try:
        client_ip = request.client.host
        web_profile = user_profiles.get(client_ip)

        system_prompt = build_system_prompt(req, web_profile)
        user_msg = "Bu bölümü yaz." if req.lang == "tr" else "Write this chapter passage."

        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            max_tokens=1000,
            temperature=0.85,
        )

        text = completion.choices[0].message.content or "..."
        has_web_profile = web_profile is not None
        return JSONResponse({"text": text, "hasWebProfile": has_web_profile})

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

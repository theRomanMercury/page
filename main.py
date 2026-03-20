import os, httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from groq import Groq
from dotenv import load_dotenv
from jose import jwt, JWTError
import databases
import sqlalchemy

load_dotenv()

app = FastAPI()

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Config ---
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI", "https://page-production-468d.up.railway.app/auth/callback")
JWT_SECRET           = os.environ.get("JWT_SECRET", "change-me-in-production")
FRONTEND_URL         = os.environ.get("FRONTEND_URL", "https://theromanmercury.github.io/page")
DATABASE_URL         = os.environ.get("DATABASE_URL", "")

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# --- Database ---
database = databases.Database(DATABASE_URL) if DATABASE_URL else None
metadata = sqlalchemy.MetaData()

profiles_table = sqlalchemy.Table(
    "profiles", metadata,
    sqlalchemy.Column("user_id",    sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("email",      sqlalchemy.String),
    sqlalchemy.Column("name",       sqlalchemy.String),
    sqlalchemy.Column("categories", sqlalchemy.JSON,   nullable=True),
    sqlalchemy.Column("search_terms", sqlalchemy.JSON, nullable=True),
    sqlalchemy.Column("time_of_day",  sqlalchemy.String, nullable=True),
    sqlalchemy.Column("total_sites",  sqlalchemy.Integer, nullable=True),
    sqlalchemy.Column("updated_at",   sqlalchemy.String, nullable=True),
)

# Fallback: DB yoksa memory
memory_profiles: dict = {}

@app.on_event("startup")
async def startup():
    if database:
        await database.connect()
        engine = sqlalchemy.create_engine(DATABASE_URL.replace("postgresql+asyncpg", "postgresql").replace("postgres://", "postgresql://"))
        metadata.create_all(engine)

@app.on_event("shutdown")
async def shutdown():
    if database:
        await database.disconnect()

# --- Auth helpers ---
def make_jwt(user_id: str, email: str, name: str) -> str:
    return jwt.encode({"sub": user_id, "email": email, "name": name}, JWT_SECRET, algorithm="HS256")

def verify_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

def get_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get("token")

# --- Google OAuth ---
@app.get("/auth/login")
async def auth_login():
    params = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        "&response_type=code"
        "&scope=openid%20email%20profile"
        "&access_type=offline"
        "&prompt=select_account"
    )
    return RedirectResponse(params)

@app.get("/auth/callback")
async def auth_callback(code: str, request: Request):
    async with httpx.AsyncClient() as client:
        # Token al
        token_res = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        tokens = token_res.json()
        if "error" in tokens:
            raise HTTPException(400, tokens["error"])

        # Kullanıcı bilgisi al
        userinfo_res = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        userinfo = userinfo_res.json()

    user_id = userinfo["sub"]
    email   = userinfo.get("email", "")
    name    = userinfo.get("name", "")

    token = make_jwt(user_id, email, name)

    # Frontend'e yönlendir, token query param ile
    return RedirectResponse(f"{FRONTEND_URL}/?token={token}&name={name}")

@app.get("/auth/verify")
async def auth_verify(request: Request):
    token = get_token(request)
    if not token:
        return JSONResponse({"authenticated": False})
    try:
        payload = verify_jwt(token)
        return JSONResponse({"authenticated": True, "user": payload})
    except:
        return JSONResponse({"authenticated": False})

# --- Profile ---
class WebProfile(BaseModel):
    categories:   Optional[dict] = {}
    searchTerms:  Optional[list] = []
    timeOfDay:    Optional[str]  = "evening"
    totalSites:   Optional[int]  = 0
    collectedAt:  Optional[str]  = ""

async def save_profile(user_id: str, email: str, name: str, profile: dict):
    if database:
        existing = await database.fetch_one(
            profiles_table.select().where(profiles_table.c.user_id == user_id)
        )
        if existing:
            await database.execute(
                profiles_table.update()
                .where(profiles_table.c.user_id == user_id)
                .values(
                    categories=profile.get("categories"),
                    search_terms=profile.get("searchTerms"),
                    time_of_day=profile.get("timeOfDay"),
                    total_sites=profile.get("totalSites"),
                    updated_at=profile.get("collectedAt"),
                )
            )
        else:
            await database.execute(
                profiles_table.insert().values(
                    user_id=user_id, email=email, name=name,
                    categories=profile.get("categories"),
                    search_terms=profile.get("searchTerms"),
                    time_of_day=profile.get("timeOfDay"),
                    total_sites=profile.get("totalSites"),
                    updated_at=profile.get("collectedAt"),
                )
            )
    else:
        memory_profiles[user_id] = profile

async def load_profile(user_id: str) -> Optional[dict]:
    if database:
        row = await database.fetch_one(
            profiles_table.select().where(profiles_table.c.user_id == user_id)
        )
        if row:
            return {
                "categories":  row["categories"],
                "searchTerms": row["search_terms"],
                "timeOfDay":   row["time_of_day"],
                "totalSites":  row["total_sites"],
            }
        return None
    return memory_profiles.get(user_id)

@app.post("/profile")
async def receive_profile(profile: WebProfile, request: Request):
    token = get_token(request)
    if not token:
        raise HTTPException(401, "No token")
    payload = verify_jwt(token)
    await save_profile(payload["sub"], payload.get("email",""), payload.get("name",""), profile.dict())
    return JSONResponse({"status": "ok", "totalSites": profile.totalSites})

@app.get("/profile/status")
async def profile_status(request: Request):
    token = get_token(request)
    if not token:
        return JSONResponse({"hasProfile": False, "fresh": False})
    try:
        payload = verify_jwt(token)
        p = await load_profile(payload["sub"])
        if not p:
            return JSONResponse({"hasProfile": False, "fresh": False})
        # Check if profile was updated within last 24 hours
        from datetime import datetime, timezone, timedelta
        updated_at = p.get("updated_at") or p.get("collectedAt", "")
        fresh = False
        if updated_at:
            try:
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                fresh = (datetime.now(timezone.utc) - updated) < timedelta(hours=24)
            except:
                fresh = False
        return JSONResponse({"hasProfile": True, "fresh": fresh, "totalSites": p.get("totalSites", 0)})
    except:
        return JSONResponse({"hasProfile": False, "fresh": False})

# --- Book generation ---
CHAPTERS = {
    "en": [
        {"title": "The Self You Cannot Step Away From", "theme": "opening — presence and the inescapable now"},
        {"title": "The Weight of Half-Attention",        "theme": "distraction, depth, and what gets lost"},
        {"title": "Returning",                           "theme": "the meaning of going back, re-reading, dwelling"},
        {"title": "Stillness as Statement",              "theme": "what patience reveals about desire"},
        {"title": "Who You Became While Reading",        "theme": "closing — transformation through attention"},
    ],
    "tr": [
        {"title": "Uzaklaşamadığın Benlik",           "theme": "açılış — şimdide var olma"},
        {"title": "Yarım Dikkatın Ağırlığı",          "theme": "dağınıklık, derinlik ve kaybolan şey"},
        {"title": "Geri Dönmek",                      "theme": "geri gitmenin, yeniden okumanın anlamı"},
        {"title": "Durgunluk Bir Bildiri Olarak",     "theme": "sabrın arzu hakkında ne gösterdiği"},
        {"title": "Okurken Kim Oldun",                "theme": "kapanış — dikkat yoluyla dönüşüm"},
    ],
}

MOOD_INSTRUCTIONS = {
    "deep":     {"en": "The reader is reading very deeply — long pauses, rereading. Write dense, layered, philosophical prose.",
                 "tr": "Okuyucu çok derin okuyor. Yoğun, katmanlı, felsefi nesir yaz."},
    "curious":  {"en": "The reader is curious and engaged. Write expansive, questioning prose that opens into new ideas.",
                 "tr": "Okuyucu meraklı ve ilgili. Yeni fikirlere açılan, sorgulayan bir nesir yaz."},
    "restless": {"en": "The reader is restless. Write shorter, punchier paragraphs. Use rhythm and contrast.",
                 "tr": "Okuyucu huzursuz. Daha kısa, daha vurucu paragraflar yaz."},
    "absent":   {"en": "The reader's attention is drifting. Write prose that gently calls them back.",
                 "tr": "Okuyucunun dikkati dağılıyor. Onu nazikçe geri çağıran bir nesir yaz."},
}

def build_web_context(profile: dict, lang: str) -> str:
    if not profile:
        return ""
    cats  = profile.get("categories", {})
    top   = sorted(cats.items(), key=lambda x: x[1], reverse=True)[:3]
    terms = profile.get("searchTerms", [])[:5]
    tod   = profile.get("timeOfDay", "")
    total = profile.get("totalSites", 0)
    if lang == "tr":
        lines = ["Okuyucunun web profili:"]
        if top:   lines.append("- Kategoriler: " + ", ".join(f"{c}({n})" for c,n in top if n>0))
        if terms: lines.append("- Aramalar: " + ", ".join(f'"{t}"' for t in terms))
        if tod:   lines.append(f"- Tarama zamanı: {tod}")
        if total: lines.append(f"- Toplam site: {total}")
        lines.append("Bu veriyi psikolojik gerçekliğe dönüştür, ham olarak verme.")
    else:
        lines = ["Reader's web profile:"]
        if top:   lines.append("- Categories: " + ", ".join(f"{c}({n})" for c,n in top if n>0))
        if terms: lines.append("- Searches: " + ", ".join(f'"{t}"' for t in terms))
        if tod:   lines.append(f"- Browsing time: {tod}")
        if total: lines.append(f"- Total sites: {total}")
        lines.append("Transform this into psychological reality, not raw data.")
    return "\n".join(lines)

class GenerateRequest(BaseModel):
    lang:               str            = "en"
    chapter_idx:        int            = 0
    mood:               str            = "curious"
    time_on_page:       int            = 0
    pause_count:        int            = 0
    scroll_back_count:  int            = 0
    story_history:      Optional[list[str]] = []

@app.post("/generate")
async def generate(req: GenerateRequest, request: Request):
    try:
        web_profile = None
        token = get_token(request)
        if token:
            try:
                payload     = verify_jwt(token)
                web_profile = await load_profile(payload["sub"])
            except:
                pass

        lang     = req.lang if req.lang in ("en","tr") else "en"
        ch       = CHAPTERS[lang][max(0, min(req.chapter_idx, len(CHAPTERS[lang])-1))]
        mood     = req.mood if req.mood in MOOD_INSTRUCTIONS else "curious"
        prev     = req.story_history or []
        prev_str = " / ".join(s[:120]+"..." for s in prev[-2:]) if prev else ("Bu ilk bölüm." if lang=="tr" else "This is the opening passage.")
        web_ctx  = build_web_context(web_profile, lang)
        behavior = f"Time: {req.time_on_page}s, Pauses: {req.pause_count}, Scrollbacks: {req.scroll_back_count}, Mood: {mood}"

        if lang == "tr":
            system = (f"Sen felsefi bir öz-keşif kitabı yazıyorsun. İki kaynaktan şekilleniyor: okuma davranışı ve web geçmişi.\n"
                      f"Okuma davranışı: {behavior}\n{web_ctx}\n"
                      f"Bölüm: \"{ch['title']}\" ({ch['theme']})\nÖnceki: {prev_str}\n"
                      f"Talimat: {MOOD_INSTRUCTIONS[mood]['tr']}\n\n"
                      f"3 paragraf edebi Türkçe nesir yaz. 'Sen' diyerek hitap et. Başlık ekleme.")
        else:
            system = (f"You write a literary philosophical book about self-discovery, shaped by reading behavior and web history.\n"
                      f"Reading behavior: {behavior}\n{web_ctx}\n"
                      f"Chapter: \"{ch['title']}\" ({ch['theme']})\nPrevious: {prev_str}\n"
                      f"Instruction: {MOOD_INSTRUCTIONS[mood]['en']}\n\n"
                      f"Write 3 paragraphs of immersive literary prose. Address reader as 'you'. No headings.")

        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":system},{"role":"user","content":"Write this passage."}],
            max_tokens=1000, temperature=0.85,
        )
        return JSONResponse({"text": completion.choices[0].message.content or "...", "hasWebProfile": web_profile is not None})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


# Extension'dan gelen Google token'ı doğrula ve JWT ver
class ExtensionAuthRequest(BaseModel):
    google_token: str
    user_id: str
    email: str
    name: str

@app.post("/auth/extension")
async def auth_extension(req: ExtensionAuthRequest):
    # Google token'ı doğrula
    async with httpx.AsyncClient() as client:
        res = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {req.google_token}"},
        )
        if res.status_code != 200:
            raise HTTPException(401, "Invalid Google token")
        userinfo = res.json()

    if userinfo.get("sub") != req.user_id:
        raise HTTPException(401, "Token mismatch")

    token = make_jwt(req.user_id, req.email, req.name)
    return JSONResponse({"token": token, "name": req.name})

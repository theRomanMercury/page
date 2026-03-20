import os, httpx, psycopg2, psycopg2.extras, json
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from groq import Groq
from dotenv import load_dotenv
from jose import jwt, JWTError

load_dotenv()

app = FastAPI()

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI", "https://page-2gnb.onrender.com/auth/callback")
JWT_SECRET           = os.environ.get("JWT_SECRET", "change-me")
FRONTEND_URL         = os.environ.get("FRONTEND_URL", "https://theromanmercury.github.io/page")
DATABASE_URL         = os.environ.get("DATABASE_URL", "")

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
memory_profiles: dict = {}

# --- DB helpers ---
def get_conn():
    url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace("postgres://", "postgresql://")
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    if not DATABASE_URL:
        return
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id      TEXT PRIMARY KEY,
                email        TEXT,
                name         TEXT,
                categories   JSONB,
                search_terms JSONB,
                time_of_day  TEXT,
                total_sites  INTEGER,
                updated_at   TEXT
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB init error: {e}")

@app.on_event("startup")
async def startup():
    init_db()

# --- Auth helpers ---
def make_jwt(user_id, email, name):
    return jwt.encode({"sub": user_id, "email": email, "name": name}, JWT_SECRET, algorithm="HS256")

def verify_jwt(token):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

def get_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get("token")

# --- Google OAuth ---
@app.get("/auth/login")
async def auth_login():
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        "&response_type=code&scope=openid%20email%20profile"
        "&access_type=offline&prompt=select_account"
    )
    return RedirectResponse(url)

@app.get("/auth/callback")
async def auth_callback(code: str):
    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://oauth2.googleapis.com/token",
            data={"code": code, "client_id": GOOGLE_CLIENT_ID,
                  "client_secret": GOOGLE_CLIENT_SECRET,
                  "redirect_uri": GOOGLE_REDIRECT_URI, "grant_type": "authorization_code"},
        )
        tokens = token_res.json()
        if "error" in tokens:
            raise HTTPException(400, tokens["error"])
        userinfo = (await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )).json()

    token = make_jwt(userinfo["sub"], userinfo.get("email",""), userinfo.get("name",""))
    return RedirectResponse(f"{FRONTEND_URL}/?token={token}&name={userinfo.get('name','')}")

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

# --- Extension auth ---
class ExtensionAuthRequest(BaseModel):
    google_token: str
    user_id: str
    email: str
    name: str

@app.post("/auth/extension")
async def auth_extension(req: ExtensionAuthRequest):
    async with httpx.AsyncClient() as client:
        res = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {req.google_token}"},
        )
        if res.status_code != 200:
            raise HTTPException(401, "Invalid Google token")
        if res.json().get("sub") != req.user_id:
            raise HTTPException(401, "Token mismatch")
    return JSONResponse({"token": make_jwt(req.user_id, req.email, req.name), "name": req.name})

# --- Profile ---
def db_save_profile(user_id, email, name, profile):
    if not DATABASE_URL:
        memory_profiles[user_id] = profile
        return
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT user_id FROM profiles WHERE user_id = %s", (user_id,))
        if cur.fetchone():
            cur.execute("""
                UPDATE profiles SET categories=%s, search_terms=%s, time_of_day=%s,
                total_sites=%s, updated_at=%s WHERE user_id=%s
            """, (json.dumps(profile.get("categories")), json.dumps(profile.get("searchTerms")),
                  profile.get("timeOfDay"), profile.get("totalSites"),
                  profile.get("collectedAt"), user_id))
        else:
            cur.execute("""
                INSERT INTO profiles (user_id,email,name,categories,search_terms,time_of_day,total_sites,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (user_id, email, name, json.dumps(profile.get("categories")),
                  json.dumps(profile.get("searchTerms")), profile.get("timeOfDay"),
                  profile.get("totalSites"), profile.get("collectedAt")))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB save error: {e}")
        memory_profiles[user_id] = profile

def db_load_profile(user_id):
    if not DATABASE_URL:
        return memory_profiles.get(user_id)
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT * FROM profiles WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        return {
            "categories":  row["categories"] if isinstance(row["categories"], dict) else json.loads(row["categories"] or "{}"),
            "searchTerms": row["search_terms"] if isinstance(row["search_terms"], list) else json.loads(row["search_terms"] or "[]"),
            "timeOfDay":   row["time_of_day"],
            "totalSites":  row["total_sites"],
            "updated_at":  row["updated_at"],
        }
    except Exception as e:
        print(f"DB load error: {e}")
        return memory_profiles.get(user_id)

class WebProfile(BaseModel):
    categories:  Optional[dict] = {}
    searchTerms: Optional[list] = []
    timeOfDay:   Optional[str]  = "evening"
    totalSites:  Optional[int]  = 0
    collectedAt: Optional[str]  = ""

@app.post("/profile")
async def receive_profile(profile: WebProfile, request: Request):
    token = get_token(request)
    if not token:
        raise HTTPException(401, "No token")
    payload = verify_jwt(token)
    db_save_profile(payload["sub"], payload.get("email",""), payload.get("name",""), profile.dict())
    return JSONResponse({"status": "ok", "totalSites": profile.totalSites})

@app.get("/profile/status")
async def profile_status(request: Request):
    token = get_token(request)
    if not token:
        return JSONResponse({"hasProfile": False, "fresh": False})
    try:
        payload = verify_jwt(token)
        p = db_load_profile(payload["sub"])
        if not p:
            return JSONResponse({"hasProfile": False, "fresh": False})
        updated_at = p.get("updated_at") or p.get("collectedAt", "")
        fresh = False
        if updated_at:
            try:
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                fresh = (datetime.now(timezone.utc) - updated) < timedelta(hours=24)
            except:
                pass
        return JSONResponse({"hasProfile": True, "fresh": fresh, "totalSites": p.get("totalSites", 0)})
    except:
        return JSONResponse({"hasProfile": False, "fresh": False})

# --- Book ---
CHAPTERS = [
    {"title": "The Self You Cannot Step Away From", "theme": "opening — presence and the inescapable now"},
    {"title": "The Weight of Half-Attention",        "theme": "distraction, depth, and what gets lost"},
    {"title": "Returning",                           "theme": "the meaning of going back, re-reading, dwelling"},
    {"title": "Stillness as Statement",              "theme": "what patience reveals about desire"},
    {"title": "Who You Became While Reading",        "theme": "closing — transformation through attention"},
]

MOOD_INSTRUCTIONS = {
    "deep":     "The reader is reading very deeply — long pauses, rereading. Write dense, layered, philosophical prose.",
    "curious":  "The reader is curious and engaged. Write expansive, questioning prose that opens into new ideas.",
    "restless": "The reader is restless. Write shorter, punchier paragraphs. Use rhythm and contrast to hold attention.",
    "absent":   "The reader's attention is drifting. Write prose that gently calls them back — surprising images, sudden intimacy.",
}

def build_web_context(profile):
    if not profile:
        return ""
    cats  = profile.get("categories", {})
    top   = sorted(cats.items(), key=lambda x: x[1], reverse=True)[:3]
    terms = profile.get("searchTerms", [])[:5]
    tod   = profile.get("timeOfDay", "")
    total = profile.get("totalSites", 0)
    lines = ["Reader's web profile:"]
    if top:   lines.append("- Categories: " + ", ".join(f"{c}({n})" for c,n in top if n>0))
    if terms: lines.append("- Searches: "   + ", ".join(f'"{t}"' for t in terms))
    if tod:   lines.append(f"- Browsing time: {tod}")
    if total: lines.append(f"- Total sites: {total}")
    lines.append("Transform this into psychological reality, not raw data.")
    return "\n".join(lines)

class GenerateRequest(BaseModel):
    lang:              str = "en"
    chapter_idx:       int = 0
    mood:              str = "curious"
    time_on_page:      int = 0
    pause_count:       int = 0
    scroll_back_count: int = 0
    story_history:     Optional[list[str]] = []

@app.post("/generate")
async def generate(req: GenerateRequest, request: Request):
    try:
        web_profile = None
        token = get_token(request)
        if token:
            try:
                payload     = verify_jwt(token)
                web_profile = db_load_profile(payload["sub"])
            except:
                pass

        ch       = CHAPTERS[max(0, min(req.chapter_idx, len(CHAPTERS)-1))]
        mood     = req.mood if req.mood in MOOD_INSTRUCTIONS else "curious"
        prev     = req.story_history or []
        prev_str = " / ".join(s[:120]+"..." for s in prev[-2:]) if prev else "This is the opening passage."
        web_ctx  = build_web_context(web_profile)
        behavior = f"Time: {req.time_on_page}s, Pauses: {req.pause_count}, Scrollbacks: {req.scroll_back_count}, Mood: {mood}"

        system = (
            f"You write a literary philosophical book about self-discovery, shaped by reading behavior and web history.\n"
            f"Reading behavior: {behavior}\n{web_ctx}\n"
            f"Chapter: \"{ch['title']}\" ({ch['theme']})\nPrevious: {prev_str}\n"
            f"Instruction: {MOOD_INSTRUCTIONS[mood]}\n\n"
            f"Write 3 paragraphs of immersive literary prose. Address reader as 'you'. No headings."
        )

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

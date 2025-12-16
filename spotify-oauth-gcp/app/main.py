import os
import json
import urllib.parse
import logging
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
import httpx

from app.secrets_manager import SecretManagerClient

load_dotenv()

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")
SCOPES = os.getenv("SCOPES", "user-read-private user-read-email user-read-recently-played user-top-read user-library-read playlist-read-private playlist-read-collaborative user-follow-read")
APP_URL = os.getenv("APP_URL")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
GCP_PROJECT = os.getenv("GCP_PROJECT")
SPOTIFY_SECRET_PREFIX = os.getenv("SPOTIFY_SECRET_PREFIX", "spotify1-refresh-")

if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and GCP_PROJECT and REDIRECT_URI):
    raise Exception("Missing required env variables.")

logger = logging.getLogger("spotify-oauth")
logging.basicConfig(level=logging.INFO)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

sm = SecretManagerClient(project_id=GCP_PROJECT)
app = FastAPI()

SPOTIFY_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_ME_URL = "https://api.spotify.com/v1/me"


def build_auth_url(state: str = None):
    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "show_dialog": "true",
        "response_mode": "query",
        "prompt": "consent",
    }
    params["state"] = state or f"st-{uuid.uuid4().hex[:8]}"

    return f"{SPOTIFY_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


@app.get("/", response_class=RedirectResponse)
async def root_redirect():
    return "/connect"


@app.get("/connect")
async def index(request: Request):
    login_url = build_auth_url()
    return templates.TemplateResponse("index.html", {"request": request, "login_url": login_url})


@app.get("/auth/login")
def login():
    return RedirectResponse(build_auth_url())


@app.get("/auth/callback")
async def callback(request: Request, code: str = None, state: str = None):
    if not code:
        raise HTTPException(status_code=400, detail="Missing code parameter")

    try:
        logger.info("Callback invoked - url=%s user_agent=%s state=%s",
                    str(request.url), request.headers.get("user-agent"), state)
    except Exception:
        pass

    auth = (SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }

    try:
        async with httpx.AsyncClient() as client:
            token_res = await client.post(SPOTIFY_TOKEN_URL, data=data, auth=auth, timeout=30)
    except Exception:
        logger.exception("Token exchange network error")
        return templates.TemplateResponse("error.html", {"request": request, "message": "Spotify token exchange failed."})

    if token_res.status_code != 200:
        return templates.TemplateResponse("error.html", {"request": request, "message": "Failed to exchange code."})

    token_json = token_res.json()
    access_token = token_json.get("access_token")
    refresh_token = token_json.get("refresh_token")
    scope = token_json.get("scope")

    if not access_token or not refresh_token:
        return templates.TemplateResponse("error.html", {"request": request, "message": "Missing tokens."})

    try:
        async with httpx.AsyncClient() as client:
            me_res = await client.get(
                SPOTIFY_ME_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
    except Exception:
        logger.exception("Error fetching profile")
        return templates.TemplateResponse("error.html", {"request": request, "message": "Failed to fetch Spotify profile."})

    if me_res.status_code != 200:
        return templates.TemplateResponse("error.html", {"request": request, "message": "Spotify profile error."})

    me = me_res.json()
    spotify_user_id = me.get("id")
    display_name = me.get("display_name", "")

    if not spotify_user_id:
        return templates.TemplateResponse("error.html", {"request": request, "message": "Profile missing ID."})

    secret_name = f"{SPOTIFY_SECRET_PREFIX}{spotify_user_id}"

    payload = json.dumps(
        {
            "spotify_user_id": spotify_user_id,
            "display_name": display_name,
            "refresh_token": refresh_token,
            "scope": scope,
            "created_at": datetime.utcnow().isoformat(),
        }
    )

    try:
        sm.create_or_update_secret(secret_id=secret_name, payload=payload)
    except Exception:
        logger.exception("Failed to save refresh token")
        return templates.TemplateResponse("error.html", {"request": request, "message": "Failed to store credentials."})

    return templates.TemplateResponse(
        "success.html",
        {"request": request, "display_name": display_name, "spotify_user_id": spotify_user_id},
    )


@app.get("/admin/users")
def admin_users(x_api_key: str = Header(None)):
    if x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    secrets = sm.list_spotify_secrets(prefix=SPOTIFY_SECRET_PREFIX)

    users = []
    for secret_name in secrets:
        payload = sm.get_secret_payload(secret_name)
        if payload:
            users.append({
                "spotify_user_id": payload.get("spotify_user_id"),
                "display_name": payload.get("display_name")
            })

    return users


@app.get("/internal/get-token/{spotify_user_id}")
def get_token(spotify_user_id: str, x_api_key: str = Header(None)):
    if x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = sm.get_secret_payload(f"{SPOTIFY_SECRET_PREFIX}{spotify_user_id}")

    if not payload:
        raise HTTPException(status_code=404, detail="User not found")

    return payload

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import engine, SessionLocal, Base
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from groq import Groq
import requests
from bs4 import BeautifulSoup
import difflib
from difflib import SequenceMatcher
import pdfplumber
import json
import os
import uuid
import urllib.parse
import models
from auth import hash_password, verify_password, create_access_token, decode_access_token

Base.metadata.create_all(bind=engine)

app = FastAPI()

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

security_scheme = HTTPBearer()
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback")
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    db: Session = Depends(get_db),
):
    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.query(models.User).filter(models.User.id == payload.get("user_id")).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


class SignupInput(BaseModel):
    name: str
    email: str
    password: str


class LoginInput(BaseModel):
    email: str
    password: str


class ProfileUpdateInput(BaseModel):
    name: str
    email: str
    education_level: str | None = None
    branch: str | None = None
    graduation_year: int | None = None
    goals: str | None = None


class SourceInput(BaseModel):
    url: str
    category: str


class EmailWatchInput(BaseModel):
    sender_filter: str | None = None
    keywords: str | None = None


def is_meaningful_change(old_text: str, new_text: str, threshold: float = 0.985) -> bool:
    if old_text == new_text:
        return False
    ratio = SequenceMatcher(None, old_text, new_text).ratio()
    return ratio < threshold


def extract_pdf_text(file_path: str) -> str:
    text_parts = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n".join(text_parts)


# ---------- Auth ----------

@app.post("/signup")
def signup(data: SignupInput, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.email == data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    new_user = models.User(
        name=data.name,
        email=data.email,
        hashed_password=hash_password(data.password),
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    token = create_access_token({"user_id": new_user.id})
    return {"access_token": token, "token_type": "bearer"}


@app.post("/login")
def login(data: LoginInput, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user or not user.hashed_password or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token({"user_id": user.id})
    return {"access_token": token, "token_type": "bearer"}


@app.get("/users/me")
def get_me(current_user: models.User = Depends(get_current_user)):
    return current_user


@app.put("/users/me")
def update_me(
    data: ProfileUpdateInput,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    for key, value in data.model_dump().items():
        setattr(current_user, key, value)
    db.commit()
    db.refresh(current_user)
    return current_user


# ---------- Google OAuth / Gmail ----------

@app.get("/auth/google/login")
def google_login(current_user: models.User = Depends(get_current_user)):
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "access_type": "offline",
        "prompt": "consent",
        "state": str(current_user.id),
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return {"auth_url": auth_url}


@app.get("/auth/google/callback")
def google_callback(code: str, state: str, db: Session = Depends(get_db)):
    token_response = requests.post(GOOGLE_TOKEN_URL, data={
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": GOOGLE_REDIRECT_URI,
    })
    token_data = token_response.json()
    print("Token exchange response:", token_data)

    if "refresh_token" not in token_data:
        return {"error": "No refresh token received", "details": token_data}

    user_id = int(state)
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        return {"error": f"No user found with id {user_id}"}

    user.google_refresh_token = token_data["refresh_token"]
    user.gmail_connected = True
    db.commit()
    db.refresh(user)

    return RedirectResponse(url=f"{FRONTEND_URL}/profile?gmail=connected")


def get_fresh_access_token(refresh_token: str) -> str:
    response = requests.post(GOOGLE_TOKEN_URL, data={
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    data = response.json()
    if "access_token" not in data:
        raise HTTPException(status_code=401, detail=f"Failed to refresh Google token: {data}")
    return data["access_token"]


def fetch_recent_gmail_messages(access_token: str, max_results: int = 15):
    headers = {"Authorization": f"Bearer {access_token}"}
    list_response = requests.get(
        f"{GMAIL_API_BASE}/users/me/messages",
        headers=headers,
        params={"maxResults": max_results},
    )
    message_ids = [m["id"] for m in list_response.json().get("messages", [])]

    results = []
    for mid in message_ids:
        detail_response = requests.get(
            f"{GMAIL_API_BASE}/users/me/messages/{mid}",
            headers=headers,
            params={"format": "metadata", "metadataHeaders": ["Subject", "From"]},
        )
        detail = detail_response.json()
        headers_list = detail.get("payload", {}).get("headers", [])
        subject = next((h["value"] for h in headers_list if h["name"] == "Subject"), "")
        sender = next((h["value"] for h in headers_list if h["name"] == "From"), "")
        results.append({
            "id": mid,
            "subject": subject,
            "sender": sender,
            "snippet": detail.get("snippet", ""),
        })
    return results


@app.get("/gmail/messages")
def get_gmail_messages(current_user: models.User = Depends(get_current_user)):
    if not current_user.gmail_connected or not current_user.google_refresh_token:
        raise HTTPException(status_code=400, detail="Gmail is not connected for this user")
    access_token = get_fresh_access_token(current_user.google_refresh_token)
    messages = fetch_recent_gmail_messages(access_token)
    return {"messages": messages}


def check_email_watches_for_user(user: models.User, db: Session):
    if not user.gmail_connected or not user.google_refresh_token:
        return

    watches = db.query(models.EmailWatch).filter(models.EmailWatch.user_id == user.id).all()
    if not watches:
        return

    try:
        access_token = get_fresh_access_token(user.google_refresh_token)
    except Exception as e:
        print(f"Failed to refresh Gmail token for user {user.id}: {e}")
        return

    messages = fetch_recent_gmail_messages(access_token)

    for watch in watches:
        keyword_list = []
        if watch.keywords:
            keyword_list = [k.strip().lower() for k in watch.keywords.split(",") if k.strip()]

        sender_list = []
        if watch.sender_filter:
            sender_list = [s.strip().lower() for s in watch.sender_filter.split(",") if s.strip()]

        for msg in messages:
            already_seen = (
                db.query(models.EmailMatch)
                .filter(
                    models.EmailMatch.watch_id == watch.id,
                    models.EmailMatch.gmail_message_id == msg["id"],
                )
                .first()
            )
            if already_seen:
                continue

            sender_matches = any(s in msg["sender"].lower() for s in sender_list)
            text = f"{msg['subject']} {msg['snippet']}".lower()
            keyword_matches = any(kw in text for kw in keyword_list)

            if sender_matches or keyword_matches:
                new_match = models.EmailMatch(
                    watch_id=watch.id,
                    gmail_message_id=msg["id"],
                    sender=msg["sender"],
                    subject=msg["subject"],
                    snippet=msg["snippet"],
                )
                db.add(new_match)

    db.commit()


@app.post("/email-watches")
def create_email_watch(
    data: EmailWatchInput,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not data.sender_filter and not data.keywords:
        raise HTTPException(status_code=400, detail="Provide a sender, keywords, or both")

    new_watch = models.EmailWatch(
        user_id=current_user.id,
        sender_filter=data.sender_filter,
        keywords=data.keywords,
    )
    db.add(new_watch)
    db.commit()
    db.refresh(new_watch)
    return new_watch


@app.get("/email-watches")
def list_email_watches(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return db.query(models.EmailWatch).filter(models.EmailWatch.user_id == current_user.id).all()


@app.delete("/email-watches/{watch_id}")
def delete_email_watch(
    watch_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    watch = (
        db.query(models.EmailWatch)
        .filter(models.EmailWatch.id == watch_id, models.EmailWatch.user_id == current_user.id)
        .first()
    )
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    db.query(models.EmailMatch).filter(models.EmailMatch.watch_id == watch_id).delete()
    db.delete(watch)
    db.commit()
    return {"message": "deleted"}


@app.get("/email-watches/{watch_id}/matches")
def get_email_matches(
    watch_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    watch = (
        db.query(models.EmailWatch)
        .filter(models.EmailWatch.id == watch_id, models.EmailWatch.user_id == current_user.id)
        .first()
    )
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    return (
        db.query(models.EmailMatch)
        .filter(models.EmailMatch.watch_id == watch_id)
        .order_by(models.EmailMatch.matched_at.desc())
        .all()
    )


# ---------- Sources ----------

@app.get("/sources")
def get_sources(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return (
        db.query(models.Source)
        .filter(models.Source.user_id == current_user.id)
        .order_by(models.Source.created_at.desc())
        .all()
    )


@app.post("/sources")
def add_source(
    source: SourceInput,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    new_source = models.Source(
        url=source.url,
        category=source.category,
        user_id=current_user.id,
    )
    db.add(new_source)
    db.commit()
    db.refresh(new_source)
    return new_source


@app.get("/sources/{source_id}")
def get_source(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    source = (
        db.query(models.Source)
        .filter(models.Source.id == source_id, models.Source.user_id == current_user.id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@app.delete("/sources/{source_id}")
def delete_source(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    source = (
        db.query(models.Source)
        .filter(models.Source.id == source_id, models.Source.user_id == current_user.id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    db.query(models.Snapshot).filter(models.Snapshot.source_id == source_id).delete()
    db.query(models.Assessment).filter(models.Assessment.source_id == source_id).delete()
    db.delete(source)
    db.commit()
    return {"message": "deleted"}


@app.post("/sources/upload")
def upload_pdf_source(
    file: UploadFile = File(...),
    category: str = Form(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    unique_name = f"{uuid.uuid4().hex}_{file.filename}"
    saved_path = os.path.join(UPLOAD_DIR, unique_name)

    with open(saved_path, "wb") as f:
        f.write(file.file.read())

    extracted_text = extract_pdf_text(saved_path)
    if not extracted_text.strip():
        os.remove(saved_path)
        raise HTTPException(status_code=400, detail="Could not extract text from this PDF (it may be a scanned image)")

    new_source = models.Source(
        url=file.filename,
        category=category,
        user_id=current_user.id,
        file_path=saved_path,
    )
    db.add(new_source)
    db.commit()
    db.refresh(new_source)

    first_snapshot = models.Snapshot(source_id=new_source.id, content=extracted_text)
    db.add(first_snapshot)
    db.commit()
    db.refresh(new_source)

    return new_source


@app.post("/sources/{source_id}/reupload")
def reupload_pdf_source(
    source_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    source = (
        db.query(models.Source)
        .filter(models.Source.id == source_id, models.Source.user_id == current_user.id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    if not source.file_path:
        raise HTTPException(status_code=400, detail="This source isn't a PDF upload")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    unique_name = f"{uuid.uuid4().hex}_{file.filename}"
    saved_path = os.path.join(UPLOAD_DIR, unique_name)
    with open(saved_path, "wb") as f:
        f.write(file.file.read())

    extracted_text = extract_pdf_text(saved_path)
    if not extracted_text.strip():
        os.remove(saved_path)
        raise HTTPException(status_code=400, detail="Could not extract text from this PDF")

    old_path = source.file_path
    source.file_path = saved_path
    source.url = file.filename
    db.commit()

    if old_path and os.path.exists(old_path):
        os.remove(old_path)

    new_snapshot = models.Snapshot(source_id=source_id, content=extracted_text)
    db.add(new_snapshot)
    db.commit()
    db.refresh(new_snapshot)

    return {"message": "new version uploaded", "snapshot_id": new_snapshot.id}


# ---------- Change detection ----------

def perform_check(source_id: int, db: Session):
    source = db.query(models.Source).filter(models.Source.id == source_id).first()
    if not source:
        return None
    try:
        if source.url.lower().endswith(".pdf"):
            response = requests.get(source.url, timeout=15)
            temp_path = os.path.join(UPLOAD_DIR, f"temp_{source_id}.pdf")
            with open(temp_path, "wb") as f:
                f.write(response.content)
            current_text = extract_pdf_text(temp_path)
            os.remove(temp_path)
        else:
            response = requests.get(source.url, timeout=10)
            soup = BeautifulSoup(response.text, "html.parser")

            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"]):
                tag.decompose()
            for ad_element in soup.select('[class*="ad"], [id*="ad"], [class*="banner"], [class*="cookie"], [class*="popup"]'):
                ad_element.decompose()

            current_text = soup.get_text(separator=" ", strip=True)

        new_snapshot = models.Snapshot(source_id=source_id, content=current_text)
        db.add(new_snapshot)
        db.commit()
        db.refresh(new_snapshot)
        return new_snapshot
    except Exception as e:
        print(f"Failed to check source {source_id}: {e}")
        return None


def run_impact_assessment(source_id: int, snapshot_id: int, older_content: str, newer_content: str, user: models.User, db: Session):
    diff_text = "\n".join(difflib.unified_diff(older_content.split(), newer_content.split(), lineterm=""))

    profile_summary = f"""
Education level: {user.education_level or "not specified"}
Branch: {user.branch or "not specified"}
Graduation year: {user.graduation_year or "not specified"}
Goals: {user.goals or "not specified"}
"""

    prompt = f"""You are analyzing a detected change on a webpage for a specific user, to decide if it's relevant to them.

User profile:
{profile_summary}

Detected change (diff format, + means added, - means removed):
{diff_text}

Respond with ONLY valid JSON, no other text, in exactly this shape:
{{"category": "one of: Urgent action needed, New opportunity, Eligibility may have changed, Deadline changed, Requirement changed, Information only, Low-confidence possible relevance", "explanation": "one or two sentences explaining why, referencing the user's specific profile details", "confidence": a number between 0 and 1}}"""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"Impact assessment failed for source {source_id}: {e}")
        return

    new_assessment = models.Assessment(
        source_id=source_id,
        snapshot_id=snapshot_id,
        category=result.get("category"),
        explanation=result.get("explanation"),
        confidence=str(result.get("confidence")),
    )
    db.add(new_assessment)
    db.commit()


def scheduled_check_all_sources():
    db = SessionLocal()
    try:
        sources = db.query(models.Source).filter(models.Source.file_path.is_(None)).all()
        for source in sources:
            snapshots_before = (
                db.query(models.Snapshot)
                .filter(models.Snapshot.source_id == source.id)
                .order_by(models.Snapshot.captured_at.desc())
                .first()
            )
            new_snapshot = perform_check(source.id, db)
            if new_snapshot and snapshots_before and is_meaningful_change(snapshots_before.content, new_snapshot.content):
                owner = db.query(models.User).filter(models.User.id == source.user_id).first()
                if owner:
                    run_impact_assessment(
                        source.id, new_snapshot.id,
                        snapshots_before.content, new_snapshot.content,
                        owner, db,
                    )
        print(f"Scheduled check completed for {len(sources)} sources.")

        users_with_gmail = db.query(models.User).filter(models.User.gmail_connected == True).all()
        for user in users_with_gmail:
            check_email_watches_for_user(user, db)
        print(f"Scheduled email check completed for {len(users_with_gmail)} Gmail-connected users.")
    finally:
        db.close()


scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_check_all_sources, "interval", minutes=2)


@app.on_event("startup")
def start_scheduler():
    scheduler.start()


@app.post("/sources/{source_id}/check")
def check_source(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    source = (
        db.query(models.Source)
        .filter(models.Source.id == source_id, models.Source.user_id == current_user.id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    snapshot = perform_check(source_id, db)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Fetch failed")
    return {"message": "snapshot saved", "snapshot_id": snapshot.id, "length": len(snapshot.content)}


@app.get("/sources/{source_id}/changes")
def get_changes(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    source = (
        db.query(models.Source)
        .filter(models.Source.id == source_id, models.Source.user_id == current_user.id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    snapshots = (
        db.query(models.Snapshot)
        .filter(models.Snapshot.source_id == source_id)
        .order_by(models.Snapshot.captured_at.desc())
        .limit(2)
        .all()
    )
    if len(snapshots) < 2:
        return {"message": "Not enough snapshots yet. Call /check at least twice."}

    newer, older = snapshots[0], snapshots[1]
    if not is_meaningful_change(older.content, newer.content):
        return {"changed": False, "message": "No meaningful change detected."}

    diff = list(difflib.unified_diff(older.content.split(), newer.content.split(), lineterm=""))
    return {
        "changed": True,
        "previous_captured_at": older.captured_at,
        "current_captured_at": newer.captured_at,
        "diff": diff,
    }


@app.get("/sources/{source_id}/impact")
def get_impact(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    source = (
        db.query(models.Source)
        .filter(models.Source.id == source_id, models.Source.user_id == current_user.id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    snapshots = (
        db.query(models.Snapshot)
        .filter(models.Snapshot.source_id == source_id)
        .order_by(models.Snapshot.captured_at.desc())
        .limit(2)
        .all()
    )
    if len(snapshots) < 2:
        return {"message": "Not enough snapshots yet. Check this source at least twice first."}

    newer, older = snapshots[0], snapshots[1]
    if not is_meaningful_change(older.content, newer.content):
        return {"changed": False, "message": "No meaningful change detected, nothing to assess."}

    diff_text = "\n".join(difflib.unified_diff(older.content.split(), newer.content.split(), lineterm=""))

    profile_summary = f"""
Education level: {current_user.education_level or "not specified"}
Branch: {current_user.branch or "not specified"}
Graduation year: {current_user.graduation_year or "not specified"}
Goals: {current_user.goals or "not specified"}
"""

    prompt = f"""You are analyzing a detected change on a webpage for a specific user, to decide if it's relevant to them.

User profile:
{profile_summary}

Detected change (diff format, + means added, - means removed):
{diff_text}

Respond with ONLY valid JSON, no other text, in exactly this shape:
{{"category": "one of: Urgent action needed, New opportunity, Eligibility may have changed, Deadline changed, Requirement changed, Information only, Low-confidence possible relevance", "explanation": "one or two sentences explaining why, referencing the user's specific profile details", "confidence": a number between 0 and 1}}"""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = response.choices[0].message.content

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"error": "Model did not return valid JSON", "raw": raw_text}

    return {
        "changed": True,
        "category": result.get("category"),
        "explanation": result.get("explanation"),
        "confidence": result.get("confidence"),
    }


# ---------- Activity feed ----------

@app.get("/activity")
def get_activity(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    assessments = (
        db.query(models.Assessment)
        .join(models.Source, models.Assessment.source_id == models.Source.id)
        .filter(models.Source.user_id == current_user.id)
        .order_by(models.Assessment.created_at.desc())
        .limit(20)
        .all()
    )

    watch_ids = [w.id for w in db.query(models.EmailWatch).filter(models.EmailWatch.user_id == current_user.id).all()]
    email_matches = (
        db.query(models.EmailMatch)
        .filter(models.EmailMatch.watch_id.in_(watch_ids))
        .order_by(models.EmailMatch.matched_at.desc())
        .limit(20)
        .all()
    ) if watch_ids else []

    activity = []
    for a in assessments:
        source = db.query(models.Source).filter(models.Source.id == a.source_id).first()
        activity.append({
            "type": "source_change",
            "timestamp": a.created_at,
            "title": source.url if source else "Unknown source",
            "category": a.category,
            "explanation": a.explanation,
            "confidence": a.confidence,
        })

    for m in email_matches:
        activity.append({
            "type": "email_match",
            "timestamp": m.matched_at,
            "title": m.subject or "(no subject)",
            "sender": m.sender,
            "snippet": m.snippet,
        })

    activity.sort(key=lambda x: x["timestamp"], reverse=True)
    return activity[:25]
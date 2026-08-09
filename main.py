from fastapi import FastAPI, Depends, HTTPException

from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import engine, SessionLocal, Base
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from groq import Groq
import requests
from bs4 import BeautifulSoup
import difflib
import json
import os
import models
from auth import hash_password, verify_password, create_access_token, decode_access_token

Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security_scheme = HTTPBearer()
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))


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


@app.get("/sources")
def get_sources(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return db.query(models.Source).filter(models.Source.user_id == current_user.id).all()


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
    db.delete(source)
    db.commit()
    return {"message": "deleted"}


def perform_check(source_id: int, db: Session):
    source = db.query(models.Source).filter(models.Source.id == source_id).first()
    if not source:
        return None
    try:
        response = requests.get(source.url, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        current_text = soup.get_text(separator=" ", strip=True)

        new_snapshot = models.Snapshot(source_id=source_id, content=current_text)
        db.add(new_snapshot)
        db.commit()
        db.refresh(new_snapshot)
        return new_snapshot
    except Exception as e:
        print(f"Failed to check source {source_id}: {e}")
        return None


def scheduled_check_all_sources():
    db = SessionLocal()
    try:
        sources = db.query(models.Source).all()
        for source in sources:
            perform_check(source.id, db)
        print(f"Scheduled check completed for {len(sources)} sources.")
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
    if newer.content == older.content:
        return {"changed": False, "message": "No change detected."}

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
    if newer.content == older.content:
        return {"changed": False, "message": "No change detected, nothing to assess."}

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
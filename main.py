from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import engine, SessionLocal, Base
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from bs4 import BeautifulSoup
import difflib
import models

Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SourceInput(BaseModel):
    url: str
    category: str
    user_id: int


class UserInput(BaseModel):
    name: str
    email: str
    education_level: str | None = None
    branch: str | None = None
    graduation_year: int | None = None
    goals: str | None = None


def perform_check(source_id: int, db: Session):
    """Fetch a source's URL, extract text, save it as a new snapshot.
    Used by both the manual /check endpoint and the scheduled job."""
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
    """Runs automatically on a timer. Opens its own DB session,
    since no HTTP request is happening to provide one via Depends(get_db)."""
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


@app.get("/sources")
def get_sources(db: Session = Depends(get_db)):
    return db.query(models.Source).all()


@app.post("/sources")
def add_source(source: SourceInput, db: Session = Depends(get_db)):
    new_source = models.Source(
        url=source.url,
        category=source.category,
        user_id=source.user_id,
    )
    db.add(new_source)
    db.commit()
    db.refresh(new_source)
    return new_source


@app.get("/sources/{source_id}")
def get_source(source_id: int, db: Session = Depends(get_db)):
    source = db.query(models.Source).filter(models.Source.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@app.delete("/sources/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db)):
    source = db.query(models.Source).filter(models.Source.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    db.query(models.Snapshot).filter(models.Snapshot.source_id == source_id).delete()
    db.delete(source)
    db.commit()
    return {"message": "deleted"}


@app.post("/sources/{source_id}/check")
def check_source(source_id: int, db: Session = Depends(get_db)):
    snapshot = perform_check(source_id, db)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Source not found or fetch failed")
    return {"message": "snapshot saved", "snapshot_id": snapshot.id, "length": len(snapshot.content)}


@app.get("/sources/{source_id}/changes")
def get_changes(source_id: int, db: Session = Depends(get_db)):
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

    diff = list(difflib.unified_diff(
        older.content.split(),
        newer.content.split(),
        lineterm=""
    ))

    return {
        "changed": True,
        "previous_captured_at": older.captured_at,
        "current_captured_at": newer.captured_at,
        "diff": diff,
    }


@app.post("/users")
def add_user(user: UserInput, db: Session = Depends(get_db)):
    new_user = models.User(**user.model_dump())
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@app.get("/users/{user_id}")
def get_user(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.put("/users/{user_id}")
def update_user(user_id: int, user: UserInput, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.id == user_id).first()
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")
    for key, value in user.model_dump().items():
        setattr(existing, key, value)
    db.commit()
    db.refresh(existing)
    return existing


@app.get("/users/{user_id}/sources")
def get_user_sources(user_id: int, db: Session = Depends(get_db)):
    return db.query(models.Source).filter(models.Source.user_id == user_id).all()
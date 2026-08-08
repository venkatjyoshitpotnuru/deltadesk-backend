from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import engine, SessionLocal, Base
import models

Base.metadata.create_all(bind=engine)

app = FastAPI()


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
    db.delete(source)
    db.commit()
    return {"message": "deleted"}


@app.post("/users")
def add_user(user: UserInput, db: Session = Depends(get_db)):
    new_user = models.User(name=user.name, email=user.email)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@app.get("/users/{user_id}/sources")
def get_user_sources(user_id: int, db: Session = Depends(get_db)):
    return db.query(models.Source).filter(models.Source.user_id == user_id).all()
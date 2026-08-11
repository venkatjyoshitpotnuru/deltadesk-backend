from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, unique=True)
    education_level = Column(String, nullable=True)
    branch = Column(String, nullable=True)
    graduation_year = Column(Integer, nullable=True)
    goals = Column(String, nullable=True)
    hashed_password = Column(String, nullable=True)
    google_refresh_token = Column(String, nullable=True)
    gmail_connected = Column(Boolean, nullable=False, default=False)

    sources = relationship("Source", back_populates="owner")


class Source(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, nullable=False)
    category = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    file_path = Column(String, nullable=True)

    owner = relationship("User", back_populates="sources")


class Snapshot(Base):
    __tablename__ = "snapshots"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False)
    content = Column(String, nullable=False)
    captured_at = Column(DateTime(timezone=True), server_default=func.now())


class EmailWatch(Base):
    __tablename__ = "email_watches"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    sender_filter = Column(String, nullable=True)
    keywords = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EmailMatch(Base):
    __tablename__ = "email_matches"

    id = Column(Integer, primary_key=True, index=True)
    watch_id = Column(Integer, ForeignKey("email_watches.id"), nullable=False)
    gmail_message_id = Column(String, nullable=False)
    sender = Column(String, nullable=True)
    subject = Column(String, nullable=True)
    snippet = Column(String, nullable=True)
    matched_at = Column(DateTime(timezone=True), server_default=func.now())

class Assessment(Base):
    __tablename__ = "assessments"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False)
    snapshot_id = Column(Integer, ForeignKey("snapshots.id"), nullable=False)
    category = Column(String, nullable=True)
    explanation = Column(String, nullable=True)
    confidence = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
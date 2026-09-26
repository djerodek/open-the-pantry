from sqlalchemy import (
    Column, Integer, String, Text, ForeignKey, DateTime, Table, UniqueConstraint, Float, Boolean
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base

recipe_tags = Table(
    "recipe_tags",
    Base.metadata,
    Column("recipe_id", Integer, ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Recipe(Base):
    __tablename__ = "recipes"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    source_url = Column(String, nullable=True)
    # 'url' | 'pdf' | 'screenshot' | 'manual' | 'email'
    source_type = Column(String, nullable=False)
    servings = Column(String, nullable=True)
    prep_time = Column(String, nullable=True)
    cook_time = Column(String, nullable=True)
    total_time = Column(String, nullable=True)
    image_path = Column(String, nullable=True)
    raw_text = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)  # free-form user notes, separate from any source content
    # Denormalized, space-joined tag names -- kept in sync by the app whenever
    # tags are set (see main.py). Lets FTS5 search match on tags without
    # complex triggers across the many-to-many recipe_tags table.
    tags_text = Column(Text, nullable=True, default="")
    # Average OCR word confidence (0-100), NULL if not OCR-derived
    ocr_confidence = Column(Float, nullable=True)

    favorite = Column(Boolean, nullable=False, default=False)
    tastiness_rating = Column(Integer, nullable=True)  # 1-5
    cook_time_rating = Column(String, nullable=True)  # 'quick' | 'moderate' | 'long'
    difficulty_rating = Column(String, nullable=True)  # 'easy' | 'medium' | 'hard'
    # Real-world time the user logged actually making it (distinct from any
    # prep/cook/total time pulled from a source). Stored as total minutes for
    # simple bucket filtering; dd:hh:mm is a display/input concern only.
    actual_cook_time_minutes = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    ingredients = relationship(
        "Ingredient", back_populates="recipe", order_by="Ingredient.position",
        cascade="all, delete-orphan"
    )
    steps = relationship(
        "Step", back_populates="recipe", order_by="Step.position",
        cascade="all, delete-orphan"
    )
    tags = relationship("Tag", secondary=recipe_tags, back_populates="recipes")

    @property
    def actual_cook_time(self):
        from .time_utils import minutes_to_ddhhmm
        return minutes_to_ddhhmm(self.actual_cook_time_minutes)


class Ingredient(Base):
    __tablename__ = "ingredients"

    id = Column(Integer, primary_key=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id", ondelete="CASCADE"))
    position = Column(Integer, default=0)
    raw_line = Column(Text, nullable=False)
    quantity = Column(String, nullable=True)
    unit = Column(String, nullable=True)
    name = Column(String, nullable=True)

    recipe = relationship("Recipe", back_populates="ingredients")


class Step(Base):
    __tablename__ = "steps"

    id = Column(Integer, primary_key=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id", ondelete="CASCADE"))
    position = Column(Integer, default=0)
    text = Column(Text, nullable=False)

    recipe = relationship("Recipe", back_populates="steps")


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("name", "category", name="uq_tag_name_category"),)

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    # 'meal_type' | 'cooking_style' | 'main_ingredient' | 'custom'
    category = Column(String, nullable=False)
    # e.g. 'cocktail' for shaken/stirred/built/blended under cooking_style; NULL otherwise
    subgroup = Column(String, nullable=True)

    recipes = relationship("Recipe", secondary=recipe_tags, back_populates="tags")


class EmailIngestSettings(Base):
    """Singleton row (always id=1) holding the optional email-ingest
    feature's configuration. One account/login is used for both IMAP
    (reading) and SMTP (sending) -- covers the common case of using one
    email account for both; a dedicated inbox/alias for this feature is
    recommended (see README) rather than a primary personal account."""
    __tablename__ = "email_ingest_settings"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)

    imap_host = Column(String, nullable=True)
    imap_port = Column(Integer, nullable=False, default=993)
    imap_use_ssl = Column(Boolean, nullable=False, default=True)

    smtp_host = Column(String, nullable=True)
    smtp_port = Column(Integer, nullable=False, default=587)
    smtp_use_tls = Column(Boolean, nullable=False, default=True)

    username = Column(String, nullable=True)
    # Fernet-encrypted (see crypto.py) -- never stored or returned as plaintext.
    password_encrypted = Column(Text, nullable=True)

    notify_email = Column(String, nullable=True)  # where success/failure notifications are sent
    subject_keyword = Column(String, nullable=False, default="[RECIPE]")
    # Comma-separated addresses ("me@example.com") and/or domains
    # ("@example.com"). Empty = accept any sender, as before.
    allowed_senders = Column(Text, nullable=False, default="")
    daily_scan_hour = Column(Integer, nullable=False, default=3)  # 0-23, container-local time
    cooldown_minutes = Column(Integer, nullable=False, default=30)

    last_notification_sent_at = Column(DateTime(timezone=True), nullable=True)
    last_scan_at = Column(DateTime(timezone=True), nullable=True)


class EmailNotificationQueueItem(Base):
    """One pending result line (a successful ingest or a parse failure)
    awaiting the next notification email. Batched together and cleared
    once actually sent -- see main.py's cooldown/flush logic. Never
    dropped: if sending fails, items stay queued and are retried on the
    next scan rather than lost."""
    __tablename__ = "email_notification_queue"

    id = Column(Integer, primary_key=True)
    success = Column(Boolean, nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

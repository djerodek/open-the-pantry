from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Literal, Optional

from .file_validation import is_safe_stored_filename


def _validate_stored_filename(v):
    """Filename fields accept only names this app generated. Without this,
    os.path.join() would let an absolute path or a traversal escape the
    uploads directory -- see file_validation.is_safe_stored_filename."""
    if v is None or v == "":
        return None
    if not is_safe_stored_filename(v):
        raise ValueError("invalid image reference")
    return v


class IngredientIn(BaseModel):
    raw_line: str
    quantity: Optional[str] = None
    unit: Optional[str] = None
    name: Optional[str] = None


class TagIn(BaseModel):
    name: str
    category: str
    subgroup: Optional[str] = None


class RecipeCreate(BaseModel):
    title: str
    source_type: str  # 'url' | 'pdf' | 'screenshot' | 'manual'
    source_url: Optional[str] = None
    servings: Optional[str] = None
    prep_time: Optional[str] = None
    cook_time: Optional[str] = None
    total_time: Optional[str] = None
    image_path: Optional[str] = None
    raw_text: Optional[str] = None
    # notes are deliberately NOT here: PUT is a full replace, so including
    # them would mean any client that omits the current value silently
    # wipes them. Notes are managed exclusively through PATCH /notes.
    ocr_confidence: Optional[float] = None
    actual_cook_time: Optional[str] = None  # 'dd:hh:mm', optional, user-entered
    favorite: bool = False
    tastiness_rating: Optional[int] = Field(default=None, ge=1, le=5)
    cook_time_rating: Optional[Literal["quick", "moderate", "long"]] = None
    difficulty_rating: Optional[Literal["easy", "medium", "hard"]] = None
    ingredients: list[IngredientIn] = []
    steps: list[str] = []
    tags: list[TagIn] = []

    _check_image_path = field_validator("image_path")(_validate_stored_filename)


class UrlIngestRequest(BaseModel):
    url: str


class IngredientOut(IngredientIn):
    id: int

    model_config = ConfigDict(from_attributes=True)


class StepOut(BaseModel):
    id: int
    position: int
    text: str

    model_config = ConfigDict(from_attributes=True)


class TagOut(TagIn):
    id: int

    model_config = ConfigDict(from_attributes=True)


class RecipeOut(BaseModel):
    id: int
    title: str
    source_type: str
    source_url: Optional[str] = None
    servings: Optional[str] = None
    prep_time: Optional[str] = None
    cook_time: Optional[str] = None
    total_time: Optional[str] = None
    image_path: Optional[str] = None
    raw_text: Optional[str] = None
    notes: Optional[str] = None
    ocr_confidence: Optional[float] = None
    actual_cook_time: Optional[str] = None
    favorite: bool = False
    tastiness_rating: Optional[int] = Field(default=None, ge=1, le=5)
    cook_time_rating: Optional[Literal["quick", "moderate", "long"]] = None
    difficulty_rating: Optional[Literal["easy", "medium", "hard"]] = None
    ingredients: list[IngredientOut] = []
    steps: list[StepOut] = []
    tags: list[TagOut] = []

    model_config = ConfigDict(from_attributes=True)


class RecipeSummaryOut(BaseModel):
    id: int
    title: str
    source_type: str
    source_url: Optional[str] = None
    image_path: Optional[str] = None
    ocr_confidence: Optional[float] = None
    total_time: Optional[str] = None
    actual_cook_time: Optional[str] = None
    favorite: bool = False
    tastiness_rating: Optional[int] = Field(default=None, ge=1, le=5)
    cook_time_rating: Optional[Literal["quick", "moderate", "long"]] = None
    difficulty_rating: Optional[Literal["easy", "medium", "hard"]] = None
    tags: list[TagOut] = []
    matched_via: list[str] = []  # 'text' and/or 'tag', only populated on search

    model_config = ConfigDict(from_attributes=True)


class RatingUpdate(BaseModel):
    favorite: Optional[bool] = None
    tastiness_rating: Optional[int] = Field(default=None, ge=1, le=5)
    cook_time_rating: Optional[Literal["quick", "moderate", "long"]] = None
    difficulty_rating: Optional[Literal["easy", "medium", "hard"]] = None
    actual_cook_time: Optional[str] = None  # 'dd:hh:mm' or '' to clear


class NotesUpdate(BaseModel):
    notes: Optional[str] = None  # '' or None both clear notes


class ImageUpdate(BaseModel):
    image_path: Optional[str] = None  # TMP_DIR draft filename to promote as the new showcase image, or None to remove the current one

    _check_image_path = field_validator("image_path")(_validate_stored_filename)


class BatchDeleteRequest(BaseModel):
    ids: list[int]


class BatchUrlIngestRequest(BaseModel):
    urls: list[str]


class EmailSettingsIn(BaseModel):
    """Password is optional here: omit it to leave the currently-stored
    credential unchanged (standard 'leave blank to keep current' pattern
    for a secret field that's never sent back to the client to prefill)."""
    enabled: bool = False
    imap_host: Optional[str] = None
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_use_ssl: bool = True
    smtp_host: Optional[str] = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_use_tls: bool = True
    username: Optional[str] = None
    password: Optional[str] = None
    notify_email: Optional[str] = None
    subject_keyword: str = "[RECIPE]"
    daily_scan_hour: int = Field(default=3, ge=0, le=23)
    cooldown_minutes: int = Field(default=30, ge=0)


class EmailSettingsOut(BaseModel):
    """Never includes the password, encrypted or otherwise -- only whether
    one is currently set, so the frontend can render the field correctly
    (e.g. a 'credential saved' placeholder) without the secret ever
    round-tripping back to the browser."""
    enabled: bool
    imap_host: Optional[str] = None
    imap_port: int
    imap_use_ssl: bool
    smtp_host: Optional[str] = None
    smtp_port: int
    smtp_use_tls: bool
    username: Optional[str] = None
    password_set: bool = False
    notify_email: Optional[str] = None
    subject_keyword: str
    daily_scan_hour: int
    cooldown_minutes: int
    last_scan_at: Optional[str] = None
    encryption_configured: bool = True  # whether RECIPE_APP_ENCRYPTION_KEY is set at all


class EmailTestResult(BaseModel):
    success: bool
    message: str


class EmailScanResult(BaseModel):
    scanned: int
    succeeded: int
    failed: int
    messages: list[str]

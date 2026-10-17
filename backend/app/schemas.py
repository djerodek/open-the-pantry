from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Annotated, Literal, Optional

from fastapi import Path

# SQLite stores integers in 64 bits. A larger number in a URL or body
# (?meal_type=99999999999999999999) passed validation as a Python int and
# then raised OverflowError inside the query: a 500 and a long traceback in
# the log, twice (Claude review round 8). Ids and other integers that reach
# the database are held to that range, so they get a 422 instead. The full
# range keeps 0 and negative ids a plain 404.
SQL_INT_MIN, SQL_INT_MAX = -2**63, 2**63 - 1
DbInt = Annotated[int, Field(ge=SQL_INT_MIN, le=SQL_INT_MAX)]        # body fields, list items
DbId = Annotated[int, Path(ge=SQL_INT_MIN, le=SQL_INT_MAX)]          # path parameters

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



SERVINGS_MAX = 100


def _strip_servings(v):
    """Trimmed; blank is none."""
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _validate_cook_time(v):
    from .time_utils import ddhhmm_error
    err = ddhhmm_error(v)
    if err:
        raise ValueError(err)
    return v

# Caps on what a recipe save accepts (DeepSeek review of 0073): generous for
# any real recipe, so a pasted book or a scripted 10 MB title stops here.
class IngredientIn(BaseModel):
    raw_line: str = Field(..., max_length=2000)
    quantity: Optional[str] = Field(default=None, max_length=100)
    unit: Optional[str] = Field(default=None, max_length=100)
    name: Optional[str] = Field(default=None, max_length=2000)


class TagIn(BaseModel):
    # Bounded like the Settings tag form (TagDefIn), with room for older
    # tags typed on a recipe (Claude review round 6: unbounded tags fed the
    # tag learning).
    name: str = Field(..., min_length=1, max_length=100)
    category: str = Field(..., min_length=1, max_length=60)
    subgroup: Optional[str] = Field(default=None, max_length=60)


class RecipeCreate(BaseModel):
    title: str = Field(..., max_length=500)
    source_type: str = Field(..., max_length=20)  # 'url' | 'pdf' | 'screenshot' | 'manual' | 'email'
    source_url: Optional[str] = Field(default=None, max_length=2000)
    # Same cap as the Servings field on the recipe page (RatingUpdate).
    servings: Optional[str] = Field(default=None, max_length=SERVINGS_MAX)
    _trim_servings = field_validator("servings", mode="before")(_strip_servings)
    prep_time: Optional[str] = Field(default=None, max_length=200)
    cook_time: Optional[str] = Field(default=None, max_length=200)
    total_time: Optional[str] = Field(default=None, max_length=200)
    image_path: Optional[str] = None
    raw_text: Optional[str] = Field(default=None, max_length=1_000_000)   # a 60-page scan is ~250k
    # notes are deliberately NOT here: PUT is a full replace, so including
    # them would mean any client that omits the current value silently
    # wipes them. Notes are managed exclusively through PATCH /notes.
    ocr_confidence: Optional[float] = None
    actual_cook_time: Optional[str] = None  # 'dd:hh:mm', optional, user-entered
    _check_cook_time = field_validator("actual_cook_time")(_validate_cook_time)
    favorite: bool = False
    tastiness_rating: Optional[int] = Field(default=None, ge=1, le=5)
    cook_time_rating: Optional[Literal["quick", "moderate", "long"]] = None
    difficulty_rating: Optional[Literal["easy", "medium", "hard"]] = None
    ingredients: list[IngredientIn] = Field(default_factory=list, max_length=500)
    steps: list[Annotated[str, Field(max_length=20_000)]] = Field(default_factory=list, max_length=500)
    tags: list[TagIn] = Field(default_factory=list, max_length=50)

    _check_image_path = field_validator("image_path")(_validate_stored_filename)


class ClientErrorReport(BaseModel):
    """An uncaught error from the browser, for the server log. Every field
    is capped: this endpoint writes whatever it's given to disk."""
    message: str = Field("", max_length=1000)
    source: str = Field("", max_length=300)
    line: Optional[int] = None
    column: Optional[int] = None
    stack: str = Field("", max_length=4000)
    page: str = Field("", max_length=300)
    user_agent: str = Field("", max_length=300)


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
    keywords: str = ""
    rules_out_vegetarian: bool = False
    user_defined: bool = False

    model_config = ConfigDict(from_attributes=True)


class AutoTagChange(BaseModel):
    id: int
    title: str
    added: list[str]


class AutoTagSelection(BaseModel):
    id: int
    tags: list[str] = Field(default_factory=list, max_length=50)


class AutoTagApply(BaseModel):
    # Omitted (None) = add every suggestion. A list = only these; an empty
    # list adds nothing. Anything not currently suggested for that recipe is
    # ignored.
    selections: Optional[list[AutoTagSelection]] = Field(default=None, max_length=5000)


class TagGroupOut(BaseModel):
    key: str
    label: str
    builtin: bool
    model_config = ConfigDict(from_attributes=True)


class TagGroupIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=40)


class TagDefIn(BaseModel):
    """A tag added (or updated) in Settings -> Tag groups."""
    name: str = Field(..., min_length=1, max_length=40)
    category: str = Field(..., min_length=1, max_length=60)
    keywords: str = Field("", max_length=1000)
    rules_out_vegetarian: bool = False


class TagDefUpdate(BaseModel):
    keywords: str = Field("", max_length=1000)
    rules_out_vegetarian: bool = False


class AutoTagIgnore(BaseModel):
    ignored: bool


class TagSuggestRequest(BaseModel):
    """What's in the edit form right now, saved or not."""
    title: str = Field("", max_length=500)
    ingredients: list[str] = Field(default_factory=list, max_length=500)
    steps: list[str] = Field(default_factory=list, max_length=500)
    current_tags: list[str] = Field(default_factory=list, max_length=200)


class AutoTagResult(BaseModel):
    dry_run: bool
    recipes_ignored: int = 0
    recipes_scanned: int
    recipes_changed: int
    tags_added: int
    changes: list[AutoTagChange]


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
    autotag_ignored: bool = False
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
    has_notes: bool = False      # for the card's notes icon; the notes themselves aren't in the list

    model_config = ConfigDict(from_attributes=True)


class RatingUpdate(BaseModel):
    favorite: Optional[bool] = None
    tastiness_rating: Optional[int] = Field(default=None, ge=1, le=5)
    cook_time_rating: Optional[Literal["quick", "moderate", "long"]] = None
    difficulty_rating: Optional[Literal["easy", "medium", "hard"]] = None
    actual_cook_time: Optional[str] = None  # 'dd:hh:mm'; '' or null clears
    # How many it serves, as written ("4", "4-6", "12 cookies"). Shown on
    # the recipe only: no filter, sort or search uses it.
    servings: Optional[str] = Field(default=None, max_length=SERVINGS_MAX)
    _trim_servings = field_validator("servings", mode="before")(_strip_servings)
    _check_cook_time = field_validator("actual_cook_time")(_validate_cook_time)


class NotesUpdate(BaseModel):
    # '' or None both clear notes. Capped like every other free-text field
    # (50,000 characters is pages of notes; a stray paste of a file isn't).
    notes: Optional[str] = Field(default=None, max_length=50_000)


class ImageUpdate(BaseModel):
    image_path: Optional[str] = None  # TMP_DIR draft filename to promote as the new showcase image, or None to remove the current one

    _check_image_path = field_validator("image_path")(_validate_stored_filename)


class BatchDeleteRequest(BaseModel):
    ids: list[DbInt]


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
    allowed_senders: str = Field(default="", max_length=2000)
    daily_scan_hour: int = Field(default=3, ge=0, le=23)
    cooldown_minutes: int = Field(default=30, ge=0, le=7 * 24 * 60)  # timedelta overflowed on huge values


class RecipeEmailRequest(BaseModel):
    """Share -> Email PDF. Addresses are checked in main.py so the error
    can name the one that's wrong."""
    to: list[str] = Field(default_factory=list, max_length=20)
    message: str = Field(default="", max_length=2000)
    include_notes: bool = False


class RecentRecipients(BaseModel):
    recipients: list[str] = Field(default_factory=list, max_length=50)


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
    # True only on the response to the save that just cleared it (changing
    # the host or username without a new password) -- lets the frontend
    # show that plainly instead of the only visible sign being the
    # password field's placeholder going from "saved" to empty.
    password_cleared: bool = False
    notify_email: Optional[str] = None
    subject_keyword: str
    allowed_senders: str = ""
    daily_scan_hour: int
    cooldown_minutes: int
    last_scan_at: Optional[str] = None
    last_problem: Optional[str] = None       # see models.EmailIngestSettings.last_problem
    last_problem_at: Optional[str] = None
    pending_notifications: int = 0           # result lines waiting for the next email
    encryption_configured: bool = True  # whether RECIPE_APP_ENCRYPTION_KEY is set at all
    # "env", "file", "env_invalid", or None -- see crypto.key_source().
    encryption_source: Optional[str] = None
    # The zone the daily scan hour is interpreted in, e.g. "EDT" or "UTC".
    server_timezone: str = ""
    # Share -> Email PDF: shown when sending can work (an SMTP host, a
    # username and a saved password), whether or not the daily scan is on.
    can_send: bool = False
    recent_recipients: list[str] = []


class EmailTestResult(BaseModel):
    success: bool
    message: str


class EmailScanResult(BaseModel):
    scanned: int
    succeeded: int
    failed: int
    messages: list[str]


class HttpsSetupIn(BaseModel):
    domain: str = Field(default="", max_length=253)  # empty = PANTRY_DOMAIN
    lan_address: str = Field(max_length=45)
    email: str = Field(max_length=254)
    port: int = 8443


class AdminPasswordIn(BaseModel):
    password: str = Field(max_length=256)


class AdminPasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class HttpsLoginIn(BaseModel):
    username: str = Field(default="", max_length=200)
    base_url: str = Field(default="", max_length=300)
    domain: str = Field(default="", max_length=253)
    # Empty keeps the saved token (unless the username or address changes).
    token: Optional[str] = Field(default=None, max_length=500)


class LearningSettingsIn(BaseModel):
    enabled: bool


class PickerSettingsIn(BaseModel):
    enabled: bool


class RestoreRequest(BaseModel):
    token: str = Field(..., max_length=64)

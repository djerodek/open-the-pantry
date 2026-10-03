"""Whether an extracted recipe is whole: it needs both ingredients and
steps. A PDF or a photo that gave only one of them is treated like a half
recipe from a URL (url_ingest.require_complete): reported as a failed
import, with other ways to add it, instead of opening a review screen with
an empty field."""

SOURCES = {
    "pdf": "This PDF",
    "image": "This image",
    "images": "These images",
}


def missing_parts(ingredients, steps) -> list[str]:
    missing = []
    if not ingredients:
        missing.append("ingredients")
    if not steps:
        missing.append("steps")
    return missing


def incomplete_message(source: str, missing: list[str]) -> str:
    """One sentence naming what's missing. The ways to add the recipe
    instead are shown by the screen (or added by the caller), not here."""
    return f"{SOURCES[source]} didn't give a complete recipe (no {' or '.join(missing)} found)."


def incomplete_info(source: str, ingredients, steps):
    """None for a whole recipe; otherwise the `incomplete` object the
    single-item ingest endpoints return beside the draft."""
    missing = missing_parts(ingredients, steps)
    if not missing:
        return None
    return {"missing": missing, "message": incomplete_message(source, missing)}

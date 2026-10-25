"""Which language a recipe is written in: English or French.

A rough count of common words in each. Used by the import corpus and by
What to eat?'s Language filter (0125). A recipe's own Language field, set
in Edit recipe, overrides the guess.
"""
import re
from typing import Optional

LANGUAGES = ("en", "fr")

_FR_WORDS = re.compile(r"\b(?:et|de|du|des|la|le|les|une|avec|tasse|tasses|cuillère|cuillères|c\. à|pincée|"
                       r"faire|ajouter|mélanger|cuire|four|minutes? environ)\b", re.IGNORECASE)
_EN_WORDS = re.compile(r"\b(?:and|the|of|with|cup|cups|tablespoons?|teaspoons?|tbsp|tsp|pinch|add|mix|stir|"
                       r"bake|oven|until)\b", re.IGNORECASE)


def guess_language(text: str) -> str:
    """'fr' when French words outnumber English ones, otherwise 'en'."""
    return "fr" if len(_FR_WORDS.findall(text or "")) > len(_EN_WORDS.findall(text or "")) else "en"


def recipe_language(language: Optional[str], title: Optional[str], content_text: Optional[str]) -> str:
    """The language a recipe counts as: its own field when set, otherwise
    the guess from its title, ingredients and steps (content_text is kept
    current by the database's triggers)."""
    if language in LANGUAGES:
        return language
    return guess_language(" ".join([title or "", content_text or ""]))

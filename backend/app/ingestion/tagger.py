import re

# category -> tag name -> keywords that trigger it (matched against combined
# ingredient text + title, case-insensitive, word-boundary aware)
KEYWORD_MAP = {
    "main_ingredient": {
        "Chicken": ["chicken", "poultry", "hen"],
        "Pork": ["pork", "bacon", "ham", "prosciutto", "sausage", "chorizo"],
        "Beef": ["beef", "steak", "brisket", "ground beef", "short rib"],
        "Fish": ["salmon", "tuna", "cod", "halibut", "tilapia", "shrimp", "fish", "shellfish", "crab", "lobster"],
        "Game": ["venison", "elk", "rabbit", "duck", "pheasant", "boar", "bison"],
        "Vegetarian": [],  # handled specially: assigned when no meat/fish keyword matches
    },
    "cooking_style": {
        "Oven": ["oven", "bake", "baked", "baking", "roast", "roasted", "broil"],
        "Stovetop": ["saut\u00e9", "saute", "simmer", "stovetop", "skillet", "pan-fry", "pan fry"],
        "Grill": ["grill", "grilled", "grilling"],
        "Barbecue/Smoker": ["smoker", "smoked", "barbecue", "bbq", "low and slow"],
        "Deep Fryer": ["deep fry", "deep-fry", "deep fried", "fryer"],
        "Pressure Cooker": ["pressure cook", "instant pot", "instapot"],
        "Griddle": ["griddle", "flat top"],
        "Shaken": ["shake well", "shaken", "cocktail shaker"],
        "Stirred": ["stir well", "stirred", "stir with ice", "bar spoon"],
        "Built": ["build in glass", "build the drink", "straight into the glass"],
        "Blended": ["blender", "blended", "frozen drink"],
    },
    "meal_type": {
        "Breakfast": ["breakfast", "pancake", "omelette", "omelet", "waffle"],
        "Brunch": ["brunch"],
        "Lunch": ["lunch"],
        "Dinner": ["dinner", "entree", "entr\u00e9e", "main course"],
        "Snack": ["snack", "appetizer", "finger food"],
        "Cocktails": ["cocktail", "gin", "vodka", "whiskey", "whisky", "rum", "tequila", "bitters", "vermouth", "liqueur"],
    },
}

MEAT_FISH_TAGS = {"Chicken", "Pork", "Beef", "Fish", "Game"}


def _text_contains(haystack: str, keyword: str) -> bool:
    if " " in keyword:
        return keyword in haystack
    return re.search(rf"\b{re.escape(keyword)}\b", haystack) is not None


def suggest_tags(title: str, ingredient_names: list[str], step_text: str = "") -> list[tuple[str, str, str | None]]:
    """
    Returns a list of (tag_name, category, subgroup) suggestions based on simple
    keyword matching. No LLM. Intended as a starting point the user can accept,
    remove, or add to via the manual-review step -- not treated as authoritative.
    """
    haystack = " ".join([title or "", " ".join(ingredient_names), step_text or ""]).lower()

    suggestions: list[tuple[str, str, str | None]] = []
    matched_meat_or_fish = False

    for category, tag_map in KEYWORD_MAP.items():
        for tag_name, keywords in tag_map.items():
            if tag_name == "Vegetarian":
                continue  # decided below, after checking meat/fish matches
            for kw in keywords:
                if _text_contains(haystack, kw):
                    subgroup = "cocktail" if tag_name in {"Shaken", "Stirred", "Built", "Blended"} else None
                    suggestions.append((tag_name, category, subgroup))
                    if tag_name in MEAT_FISH_TAGS:
                        matched_meat_or_fish = True
                    break

    if not matched_meat_or_fish:
        # No meat/fish keyword found anywhere in the recipe text -- suggest
        # Vegetarian as a starting point. Still user-editable; not authoritative
        # (doesn't know about e.g. fish sauce, gelatin, lard as an ingredient name
        # unless those specific words appear).
        suggestions.append(("Vegetarian", "main_ingredient", None))

    return suggestions

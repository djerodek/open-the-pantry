import re

# category -> tag name -> keywords that trigger it (matched against combined
# ingredient text + title, case-insensitive, word-boundary aware)
KEYWORD_MAP = {
    # English and French (Quebec usage). Matching ignores accents, case and
    # oe/œ, so "boeuf" and "bœuf", "poele" and "poêle" are the same word.
    "main_ingredient": {
        "Chicken": ["chicken", "poultry", "hen",
                    "poulet", "volaille", "poule", "chapon", "pilons", "haut de cuisse", "hauts de cuisse"],
        "Pork": ["pork", "bacon", "ham", "prosciutto", "sausage", "chorizo", "pancetta",
                 "porc", "jambon", "lardons", "saucisse", "saucisses", "cotelettes de porc", "filet de porc"],
        "Beef": ["beef", "steak", "brisket", "ground beef", "short rib", "oxtail", "chuck", "blade roast",
                 "pot roast", "prime rib", "ribeye", "rib eye", "sirloin", "flank steak",
                 "boeuf", "bavette", "surlonge", "contre-filet", "faux-filet", "rosbif", "queue de boeuf",
                 "pointe de poitrine"],
        "Fish": ["salmon", "tuna", "cod", "halibut", "tilapia", "shrimp", "fish", "shellfish", "crab", "lobster",
                 "trout", "mackerel", "sardine", "sardines", "anchovy", "anchovies", "prawn", "prawns",
                 "mussel", "mussels", "moules", "clam", "clams", "oyster", "oysters", "scallop", "scallops",
                 "squid", "calamari", "octopus", "haddock", "sea bass", "snapper",
                 "poisson", "poissons", "saumon", "thon", "morue", "fletan", "crevette", "crevettes", "crabe",
                 "homard", "truite", "maquereau", "anchois", "palourde", "palourdes", "huitre", "huitres",
                 "petoncle", "petoncles", "calmar", "calmars", "pieuvre", "aiglefin", "vivaneau",
                 "fruits de mer"],   # not "dore" (walleye): "until golden" is "jusqu'a ce que dore"
        "Game": ["venison", "elk", "rabbit", "duck", "pheasant", "boar", "bison",
                 "gibier", "chevreuil", "cerf", "wapiti", "orignal", "lapin", "canard", "faisan", "sanglier"],
        "Vegetarian": [],  # handled specially: assigned when no meat/fish keyword matches
    },
    "cooking_style": {
        "Oven": ["oven", "bake", "baked", "baking", "roast", "roasted", "broil",
                 # Not bare "four": "four eggs". French needs the article.
                 "au four", "le four", "du four", "enfourner", "rotir", "gratiner", "gratin"],
        "Stovetop": ["saut\u00e9", "saute", "simmer", "stovetop", "skillet", "pan-fry", "pan fry",
                     "poele", "poelon", "mijoter", "faire revenir", "faire sauter", "a feu moyen",
                     "a feu vif", "a feu doux", "chaudron", "cuisiniere"],
        "Grill": ["grill", "grilled", "grilling", "gril", "griller", "grille"],
        "Barbecue/Smoker": ["smoker", "smoked", "barbecue", "bbq", "low and slow", "fumoir", "fume", "fumee"],
        "Deep Fryer": ["deep fry", "deep-fry", "deep fried", "fryer", "friteuse", "grande friture"],
        "Pressure Cooker": ["pressure cook", "instant pot", "instapot", "autocuiseur", "cocotte-minute", "presto"],
        "Griddle": ["griddle", "flat top", "plaque chauffante", "plancha"],
        "Shaken": ["shake well", "shaken", "cocktail shaker", "agiter", "bien agiter", "au shaker"],
        "Stirred": ["stir well", "stirred", "stir with ice", "bar spoon", "remuer avec de la glace",
                    "cuillere a melange"],
        "Built": ["build in glass", "build the drink", "straight into the glass", "directement dans le verre"],
        "Blended": ["blender", "blended", "frozen drink", "melangeur", "boisson glacee"],
    },
    "meal_type": {
        # Quebec: dejeuner = breakfast, diner = lunch, souper = supper.
        # Plain "diner" isn't used for Dinner: in English it's a restaurant,
        # and in Quebec French it's lunch.
        "Breakfast": ["breakfast", "pancake", "omelette", "omelet", "waffle",
                      "dejeuner", "petit-dejeuner", "petit dejeuner", "crepes", "gaufres"],
        "Brunch": ["brunch"],
        "Lunch": ["lunch", "diner du midi", "boite a lunch"],
        "Dinner": ["dinner", "entree", "entr\u00e9e", "main course", "souper", "plat principal"],
        "Snack": ["snack", "appetizer", "finger food", "collation", "grignotines", "bouchees", "amuse-gueule"],
        "Cocktails": ["cocktail", "gin", "vodka", "whiskey", "whisky", "rum", "tequila", "bitters", "vermouth",
                      "liqueur", "rhum"],
    },
}

MEAT_FISH_TAGS = {"Chicken", "Pork", "Beef", "Fish", "Game"}

# Meat with no main-ingredient tag of its own. Matching one assigns no tag,
# but does stop the Vegetarian suggestion: "Roast Leg of Lamb" and
# "Thanksgiving Turkey" were tagged Vegetarian, and batch and email imports
# save tags without review.
NOT_VEGETARIAN = ["lamb", "mutton", "veal", "goat", "turkey", "quail", "goose", "venison",
                  "gelatin", "gelatine", "lard", "suet", "fish sauce", "oyster sauce", "bone broth",
                  # "bone marrow", not "marrow": vegetable marrow is a squash.
                  "bone marrow",
                  # French. Not plain "chevre": goat cheese is "fromage de chevre".
                  "agneau", "mouton", "veau", "cabri", "dinde", "caille", "oie", "saindoux",
                  "sauce de poisson", "sauce aux huitres", "bouillon d'os", "moelle"]


# Stocks, broths and sauces made from an animal flavour a dish; they aren't
# its main ingredient. Found in real recipes: "chicken stock" tagged an
# Irish beef stew and a pork noodle dish Chicken, and "oyster sauce" tagged
# a pork stir-fry Fish. These phrases are taken out before the main
# ingredient is looked for -- but still rule out Vegetarian.
_FLAVOURING = re.compile(
    r"\b(chicken|beef|pork|veal|turkey|ham|bone|fish|oyster|clam|shrimp|prawn|anchovy|crab|lobster)"
    r"\s+(stock|broth|bouillon|base|sauce|paste|cubes?|consomme|fat|drippings|juice)\b"
    # French, in its own word order: "bouillon de poulet", "fond de veau",
    # "sauce aux huitres", "graisse de canard" (text is accent-folded first).
    r"|\b(bouillon|fond|fumet|consomme|sauce|graisse|gras|jus|cube|cubes)\s+(?:de\s+|d'|aux\s+|au\s+|a\s+la\s+)?"
    r"(poulet|volaille|boeuf|veau|porc|jambon|poisson|huitres?|crevettes?|homard|palourdes?|canard|bacon|os)\b"
)

# "un moule a gateau", "deux moules a muffins": in French a moule is also a
# baking tin. Removed before looking for Fish.
_BAKING_TIN = re.compile(r"\bmoules?\s+(?:a|de|en)\s+\w+")


def _fold(text: str) -> str:
    """Lowercase, accents removed, oe/ae ligatures spelled out, curly
    apostrophes straightened -- so French matches however it was typed or
    OCR'd ("Bœuf", "boeuf", "BOEUF"; "poêle", "poele")."""
    import unicodedata
    text = (text or "").lower().replace("\u0153", "oe").replace("\u00e6", "ae")
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def _text_contains(haystack: str, keyword: str) -> bool:
    """Whole-word match on folded text. Multi-word keywords used to be a
    plain substring test; "au four" would then match inside "beau four..."."""
    kw = _fold(keyword)
    return re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", haystack) is not None


# Keywords added in Settings -> Tag groups: (name, category, subgroup,
# [keywords], rules_out_vegetarian). Loaded from the tags table by
# main.refresh_user_keywords() at startup and after every change there;
# replaced as a whole, so readers never see a half-built list.
_user_keywords: list[tuple[str, str, str | None, list[str], bool]] = []


def set_user_keywords(entries):
    global _user_keywords
    _user_keywords = list(entries)


def meat_tag_names() -> set[str]:
    """Main-ingredient tags that rule out Vegetarian: the built-in meats
    and fish plus any tag marked that way in Settings."""
    return set(MEAT_FISH_TAGS) | {n for n, c, _, _, rov in _user_keywords if rov and c == "main_ingredient"}


def suggest_tags(title: str, ingredient_names: list[str], step_text: str = "") -> list[tuple[str, str, str | None]]:
    """
    Returns a list of (tag_name, category, subgroup) suggestions based on simple
    keyword matching. No LLM. Intended as a starting point the user can accept,
    remove, or add to via the manual-review step -- not treated as authoritative.
    """
    haystack = _fold(" ".join([title or "", " ".join(ingredient_names), step_text or ""]))

    suggestions: list[tuple[str, str, str | None]] = []
    matched_meat_or_fish = False
    animal_flavouring = _FLAVOURING.search(haystack) is not None
    main_haystack = _BAKING_TIN.sub(" ", _FLAVOURING.sub(" ", haystack))

    for category, tag_map in KEYWORD_MAP.items():
        text = main_haystack if category == "main_ingredient" else haystack
        for tag_name, keywords in tag_map.items():
            if tag_name == "Vegetarian":
                continue  # decided below, after checking meat/fish matches
            for kw in keywords:
                if _text_contains(text, kw):
                    subgroup = "cocktail" if tag_name in {"Shaken", "Stirred", "Built", "Blended"} else None
                    suggestions.append((tag_name, category, subgroup))
                    if tag_name in MEAT_FISH_TAGS:
                        matched_meat_or_fish = True
                    break

    # Keywords added in Settings. A built-in tag can get extra words too, so
    # skip any tag already suggested above.
    have = {(n, c) for n, c, _ in suggestions}
    for name, category, subgroup, keywords, rules_out_veg in _user_keywords:
        if (name, category) in have:
            continue
        text = main_haystack if category == "main_ingredient" else haystack
        if any(_text_contains(text, kw) for kw in keywords):
            suggestions.append((name, category, subgroup))
            have.add((name, category))
            if rules_out_veg or name in MEAT_FISH_TAGS:
                matched_meat_or_fish = True

    if not matched_meat_or_fish and (animal_flavouring or any(_text_contains(haystack, kw) for kw in NOT_VEGETARIAN)):
        matched_meat_or_fish = True

    if not matched_meat_or_fish:
        # No meat/fish keyword found anywhere in the recipe text -- suggest
        # Vegetarian as a starting point. Still user-editable; not authoritative
        # (doesn't know about e.g. fish sauce, gelatin, lard as an ingredient name
        # unless those specific words appear).
        suggestions.append(("Vegetarian", "main_ingredient", None))

    return suggestions

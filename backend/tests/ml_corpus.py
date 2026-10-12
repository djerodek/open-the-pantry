"""A made-up but realistic set of imported recipes for the learning tests.

Each item is what a user ends up saving (title, ingredients, steps) plus the
text an import would have read for it, in one of the layouts the rules meet
in real use: a printed blog page, an app screenshot through OCR, an email,
a cookbook scan, and French versions. The layouts are the hard part for the
rules (blog chatter before the card, step numbers OCR'd as symbols, steps
wrapped over several lines), so the learned model has something to learn.

Deterministic for a given seed.
"""
import random

ING_POOL = [
    ("1 cup", "all-purpose flour"), ("2 tbsp", "olive oil"), ("3", "garlic cloves, minced"),
    ("1", "onion, finely chopped"), ("500 g", "ground beef"), ("1 tsp", "salt"), ("½ tsp", "black pepper"),
    ("2 cups", "chicken stock"), ("1 can (400 ml)", "coconut milk"), ("2", "carrots, diced"),
    ("1 lb", "boneless chicken thighs (cut into bite-size pieces)"), ("3 tbsp", "soy sauce"),
    ("1 tbsp", "brown sugar"), ("2", "eggs"), ("¾ cup", "milk"), ("4 oz", "cheddar cheese, grated"),
    ("1 ¼ teaspoons", "smoked paprika"), ("2 cups", "basmati rice"), ("1 bunch", "cilantro"),
    ("200 g", "bacon, chopped"), ("1 tbsp", "tomato paste"), ("2", "bay leaves"), ("1 cup", "frozen peas"),
    ("3 tbsp", "butter"), ("1 tsp", "ground cumin"), ("½ cup", "heavy cream"), ("1", "lemon, juiced"),
]
ING_TAILS = ["Salt and pepper to taste", "Fresh parsley, for serving", "Chopped chives, to garnish"]
STEP_POOL = [
    "Preheat the oven to 375°F and line a baking sheet with parchment paper.",
    "Heat the oil in a large skillet over medium-high heat until shimmering.",
    "Add the onion and garlic and cook, stirring often, until softened, about 5 minutes.",
    "Stir in the spices and cook for one minute more, until fragrant.",
    "Pour in the stock, bring to a boil, then reduce the heat and simmer for 20 minutes.",
    "Whisk the eggs and milk together in a bowl until smooth.",
    "Transfer the mixture to the prepared dish and spread it into an even layer.",
    "Bake for 25 to 30 minutes, until golden and bubbling at the edges.",
    "Season with salt and pepper, then stir in the cream.",
    "Remove from the heat and let rest for 10 minutes before serving.",
    "Meanwhile, cook the rice according to the package directions.",
    "Serve hot, garnished with the herbs.",
]
TITLES = ["Weeknight Chicken Curry", "Cheesy Baked Pasta", "Smoky Bean Chili", "Lemon Herb Rice",
          "Classic Beef Stew", "Garlic Butter Noodles", "Sheet Pan Vegetables", "Creamy Tomato Soup",
          "Coconut Lentil Dal", "Bacon Pea Risotto", "Spiced Carrot Soup", "Golden Egg Bake"]

FR_ING = [("1 tasse", "farine tout usage"), ("2 c. à soupe", "huile d'olive"), ("3", "gousses d'ail, hachées"),
          ("1", "oignon, haché finement"), ("500 g", "bœuf haché"), ("1 c. à thé", "sel"),
          ("2 tasses", "bouillon de poulet"), ("2", "carottes en dés"), ("3 c. à soupe", "beurre"),
          ("1/2 tasse", "crème 35 %")]
FR_STEPS = ["Préchauffer le four à 190 °C.", "Dans une grande poêle, chauffer l'huile à feu moyen-vif.",
            "Ajouter l'oignon et l'ail et cuire en remuant jusqu'à ce qu'ils soient tendres, environ 5 minutes.",
            "Verser le bouillon, porter à ébullition puis laisser mijoter 20 minutes.",
            "Incorporer la crème et assaisonner de sel et de poivre.", "Servir chaud, garni de persil."]
FR_TITLES = ["Pâté chinois de semaine", "Soupe aux carottes", "Ragoût de bœuf", "Pâtes crémeuses à l'ail"]


def _wrap(text, width, rng):
    words, lines, cur = text.split(), [], ""
    width = width + rng.randint(-8, 8)
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def _recipe(rng, french=False):
    pool, steps, titles = (FR_ING, FR_STEPS, FR_TITLES) if french else (ING_POOL, STEP_POOL, TITLES)
    ings = [f"{q} {n}" for q, n in rng.sample(pool, rng.randint(5, min(9, len(pool))))]
    if not french and rng.random() < 0.5:
        ings.append(rng.choice(ING_TAILS))
    st = rng.sample(steps, rng.randint(4, min(7, len(steps))))
    return rng.choice(titles), ings, st


def _blog(title, ings, steps, rng):
    lines = ["2026-09-25, 1:44 PM", f"{title} - Kitchen Diaries", "Home / Recipes / Dinner / " + title,
             "Jump to Recipe", title,
             "This is one of those dishes I make every week. The ingredients are simple and you probably",
             "have most of them in your pantry already. Here's what you'll need:",
             f"{ings[0].split(' ', 1)[-1].capitalize()} - the star of the show.",
             "Let's get cooking!", title, "By: Jamie Cook", "Ingredients", "1X 2X 3X"]
    for ing in ings:
        lines += _wrap(ing, 34, rng) if len(ing) > 34 else [ing]
    lines.append("Instructions")
    for i, s in enumerate(steps, 1):
        wrapped = _wrap(s, 48, rng)
        lines.append(f"{i}. {wrapped[0]}")
        lines += wrapped[1:]
    lines += ["Notes", "Leftovers keep for 3 days in the fridge.", "Nutrition", "Calories: 420kcal",
              "kitchendiaries.com", "Page 1 of 2"]
    return lines


def _screenshot(title, ings, steps, rng):
    lines = ["7:38 a 7 @®", title, "45 min · 4 servings"]
    lines += [f"{rng.choice(['LJ', 'L', ''])} {ing}".strip() for ing in ings]
    marks = ["@", "9", "©", "faa)", "3)", "®"]
    for s in steps:
        wrapped = _wrap(s, 40, rng)
        lines.append(f"{rng.choice(marks)} {wrapped[0]}")
        lines += wrapped[1:]
    lines += ["Save Share", "Rate this recipe"]
    return lines


def _email(title, ings, steps, rng):
    lines = [f"Fwd: {title}", "Hi! Here's the recipe I promised.", title, "Ingredients:"]
    lines += [f"   - {ing}" for ing in ings]
    lines.append("")
    for i, s in enumerate(steps, 1):
        lines += _wrap(f"{i}. {s}", 70, rng)
    lines += ["Enjoy!", "Sent from my iPhone"]
    return lines


def _cookbook(title, ings, steps, rng):
    lines = [title.upper(), "Serves 4", "A family favourite that comes together with very little fuss and",
             "tastes even better the next day."]
    half = len(ings) // 2
    lines += ["FOR THE BASE"] + ings[:half] + ["FOR THE TOPPING"] + ings[half:]
    lines.append("Method")
    for s in steps:
        lines += _wrap(s, 55, rng)
    lines += ["TIP", "Swap the stock for water if you need to.", "84"]
    return lines


def _french(title, ings, steps, rng):
    lines = ["Recettes du Québec", title, "Portions : 4", "Ingrédients"]
    lines += ings
    lines.append("Préparation")
    for i, s in enumerate(steps, 1):
        wrapped = _wrap(s, 45, rng)
        lines.append(f"Étape {i}")
        lines += wrapped
    lines += ["Astuces", "Se congèle bien."]
    return lines


LAYOUTS = {"blog": _blog, "screenshot": _screenshot, "email": _email, "cookbook": _cookbook, "french": _french}


def corpus(n=40, seed=7, layouts=None):
    """[(raw_text, title, ingredients, steps, layout)]"""
    rng = random.Random(seed)
    layouts = layouts or list(LAYOUTS)
    out = []
    for i in range(n):
        layout = layouts[i % len(layouts)]
        title, ings, steps = _recipe(rng, french=layout == "french")
        if layout == "cookbook":
            half = len(ings) // 2
            saved_ings = ["For the base:"] + ings[:half] + ["For the topping:"] + ings[half:]
        else:
            saved_ings = list(ings)
        raw = "\n".join(LAYOUTS[layout](title, ings, steps, rng))
        out.append((raw, title, saved_ings, steps, layout))
    return out

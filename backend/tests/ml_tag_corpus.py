"""Made-up tagged recipes for the tag-learning tests: a user with two tags
of their own ("Pasta night", "Curry") that no keyword list covers, applied
by what's in the recipe, plus noise. Deterministic for a given seed."""
import random

PASTAS = ["spaghetti", "penne", "rigatoni", "orzo", "linguine", "fusilli", "macaroni"]
CURRY_SPICES = ["garam masala", "turmeric", "curry paste", "cumin seeds", "coriander powder"]
OTHER = ["potatoes", "carrots", "kale", "chickpeas", "rice", "tofu", "mushrooms", "zucchini", "spinach",
         "lentils", "bell pepper", "corn", "cabbage", "quinoa"]
BASICS = ["olive oil", "salt", "black pepper", "garlic", "onion", "butter", "lemon juice", "parsley"]


def tagged_recipes(n=40, seed=5):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        kind = rng.choice(["pasta", "curry", "plain", "plain"])
        ings = rng.sample(BASICS, 3) + rng.sample(OTHER, 2)
        tags = set()
        if kind == "pasta":
            ings.append(rng.choice(PASTAS))
            title = rng.choice(["Weeknight", "Easy", "Creamy", "Baked", "Garlicky"]) + " " + rng.choice(["Supper", "Bowl", "Skillet"])
            tags.add(("Pasta night", "custom"))
        elif kind == "curry":
            ings += rng.sample(CURRY_SPICES, 2)
            title = rng.choice(["Golden", "Spiced", "Fragrant", "Weeknight"]) + " " + rng.choice(["Bowl", "Stew", "Pot"])
            tags.add(("Curry", "custom"))
        else:
            title = rng.choice(["Sheet Pan", "Simple", "Garden", "Roasted"]) + " " + rng.choice(["Vegetables", "Salad", "Hash"])
        steps = "Cook everything together and serve."
        out.append((title, ings, steps, tags))
    return out

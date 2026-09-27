"""Fixes from the September 27 list: screenshot parsing, suggested-tag scan
skipping, and the edit form's Suggest tags button.

app.* is imported inside tests only; see the note in test_backup.py.
"""

# OCR of two screenshots of a grocery-store app recipe (Spicy shrimp
# fusilli), exactly as Tesseract read them. The steps were lost: "Method 2
# steps" and "Ingredients 13 total" weren't taken as headings, "Step 1" not
# as a step marker, and the phone's status bar landed in the ingredients.
SHRIMP_OCR = """Spicy shrimp fusilli

2024-10-14 7:39 PM

7:38 a 7 @®

< Q. Search for a product

Ingredients 13 total

8 oz Fusilli

2 tbsp olive oil, divided

1 pkg Uncooked Extra Large Black Tiger Shrimp, thawed
1/2 tsp hot pepper flakes

1 onion, finely diced

3 cloves garlic, minced

1 can diced tomatoes

1/4 tsp salt

1/4 tsp pepper

1/4 tsp dried oregano

1 cup loosely packed baby arugula
1 tbsp lemon juice

2 tosp finely chopped fresh parsley


Method 2 steps

Step 1

Boil water in large skillet set over high heat. Cook pasta
until al dente (cook slightly less than package directions).
Reserve 1/2 cup (125 mL) pasta cooking water. Drain
pasta; set aside. Heat 1 tbsp (15 mL) oil in same skillet set
over medium heat. Add shrimp and hot pepper flakes;
sauté 3 to 4 min. until shrimp are pink and firm. Transfer
to plate.

Step 2

Heat remaining oil in skillet. Add onion and garlic. Cook
about 2 min. to soften. Stir in tomatoes, 1/2 cup (125 mL)
water (not the pasta water), salt, pepper and oregano;
bring to a boil. Reduce heat to medium-low; simmer about
15 min. until sauce is thickened. Add pasta, reserved
pasta water and shrimp into skillet; stir to coat. Just
before serving, stir in arugula and lemon juice; sprinkle
with parsley."""


def test_grocery_app_screenshot_steps_are_found():
    from app.ingestion.pdf_ingest import segment_raw_text
    s = segment_raw_text(SHRIMP_OCR)
    assert s["title_guess"] == "Spicy shrimp fusilli"
    assert len(s["ingredients"]) == 13
    assert s["ingredients"][0] == "8 oz Fusilli"
    assert "2 tbsp finely chopped fresh parsley" in s["ingredients"]        # OCR "tosp"
    assert not any("7:38" in i for i in s["ingredients"])                   # status bar
    assert len(s["steps"]) == 2
    assert s["steps"][0].startswith("Boil water") and s["steps"][0].endswith("Transfer to plate.")
    assert s["steps"][1].startswith("Heat remaining oil") and s["steps"][1].endswith("sprinkle with parsley.")


def test_step_label_is_not_the_steps_heading():
    from app.ingestion.pdf_ingest import HEADING_STEPS, HEADING_INGREDIENTS
    for h in ["Method", "Method 2 steps", "Directions:", "Instructions (4)", "Steps"]:
        assert HEADING_STEPS.match(h), h
    for h in ["Ingredients", "Ingredients 13 total", "Ingredients 1X 2X", "Ingredients (8)"]:
        assert HEADING_INGREDIENTS.match(h), h
    for not_h in ["Step 1", "Step 2:", "Method of cooking is key"]:
        assert not HEADING_STEPS.match(not_h), not_h


def _make(client, title, ingredients):
    r = client.post("/api/recipes", json={"title": title, "source_type": "manual", "steps": ["Simmer gently."],
                                          "ingredients": [{"raw_line": i} for i in ingredients]})
    return r.json()["id"]


def test_skipped_recipes_are_left_out_of_the_scan(client):
    rid = _make(client, "Skip Me Salmon", ["2 salmon fillets"])
    pre = client.post("/api/tags/auto-apply?dry_run=true").json()
    assert any(c["id"] == rid for c in pre["changes"])
    r = client.patch(f"/api/recipes/{rid}/autotag-ignore", json={"ignored": True})
    assert r.status_code == 200 and r.json()["autotag_ignored"] is True
    after = client.post("/api/tags/auto-apply?dry_run=true").json()
    assert all(c["id"] != rid for c in after["changes"]) and after["recipes_ignored"] >= 1
    client.post("/api/tags/auto-apply")
    assert not client.get(f"/api/recipes/{rid}").json()["tags"]

    # A normal edit (PUT) doesn't reset it; the edit screen's checkbox does.
    rec = client.get(f"/api/recipes/{rid}").json()
    client.put(f"/api/recipes/{rid}", json={"title": "Skip Me Salmon", "source_type": "manual",
                                            "ingredients": [{"raw_line": "2 salmon fillets"}], "steps": ["x"]})
    assert client.get(f"/api/recipes/{rid}").json()["autotag_ignored"] is True
    client.patch(f"/api/recipes/{rid}/autotag-ignore", json={"ignored": False})
    assert any(c["id"] == rid for c in client.post("/api/tags/auto-apply?dry_run=true").json()["changes"])


def test_suggest_for_the_edit_form(client):
    r = client.post("/api/tags/suggest", json={
        "title": "Weeknight Clam Pasta", "ingredients": ["1 lb clams", "linguine"], "steps": ["Simmer the sauce."],
        "current_tags": ["Dinner", "stovetop"]})
    got = {t["name"]: t["category"] for t in r.json()}
    assert got.get("Fish") == "main_ingredient"
    assert "Stovetop" not in got           # already listed (case-insensitive)
    assert "Vegetarian" not in got


def test_suggest_skips_vegetarian_next_to_a_listed_meat_tag(client):
    r = client.post("/api/tags/suggest", json={"title": "Sunday Dinner", "ingredients": ["potatoes", "soup mix"],
                                               "current_tags": ["Beef"]})
    assert "Vegetarian" not in {t["name"] for t in r.json()}


def test_screenshot_without_headings_gets_its_steps():
    """An app screenshot with a checklist of ingredients and circled step
    numbers, no "Ingredients"/"Method" headings. OCR turns the checkboxes
    into "LJ" and the circled numbers into "@", "9", "(c)"; the steps used
    to be lost and the title came out as "LJ"."""
    from app.ingestion.pdf_ingest import segment_raw_text
    raw = "\n".join([
        "Test Beef Stew", "2024-02-20 8:57 PM", "7:34 94", "",
        "2 tbsp olive oil", "2 lb beef chuck", "3 garlic cloves", "2 onions",
        "LJ", "L", "LJ", "1 sprig thyme", "AA & a example.com", "",
        "Cut the beef into chunks. Pat dry", "then season well.", "",
        "@ Brown the beef in batches over high", "heat. Remove to a plate.", "",
        "9 Cook garlic and onion for 3 minutes.", "",
        "© Return the beef and simmer for 2 hours.", "",
        "faa) Serve with mashed potatoes!!",
    ])
    s = segment_raw_text(raw)
    assert s["title_guess"] == "Test Beef Stew"
    assert s["ingredients"] == ["2 tbsp olive oil", "2 lb beef chuck", "3 garlic cloves", "2 onions", "1 sprig thyme"]
    assert s["steps"] == [
        "Cut the beef into chunks. Pat dry then season well.",
        "Brown the beef in batches over high heat. Remove to a plate.",
        "Cook garlic and onion for 3 minutes.",
        "Return the beef and simmer for 2 hours.",
        "Serve with mashed potatoes!!",
    ]


def test_steps_stop_at_ratings_and_reviews():
    from app.ingestion.pdf_ingest import segment_raw_text
    raw = "\n".join(["Test Pork Chops", "Ingredients", "4 pork chops", "2 apples", "Directions",
                     "Step 1", "Preheat the oven.", "Step 2", "Roast the chops.",
                     "Reviews (9)", "Great recipe, loved it.", "17 Ratings", "5 star 15"])
    assert segment_raw_text(raw)["steps"] == ["Preheat the oven.", "Roast the chops."]

import os
import sys
import time

from app.ingestion.ingredient_parser import parse_ingredient_block, parse_ingredient_line  # noqa: E402


def test_basic_quantity_unit_name():
    r = parse_ingredient_line("2 cups flour")
    assert r["quantity"] == "2"
    assert r["unit"] == "cup"
    assert r["name"] == "flour"


def test_unit_normalization_variants():
    """tablespoon/tablespoons/tbsp should all collapse to the same token."""
    for line in ["2 tablespoons olive oil", "2 tablespoon olive oil", "2 tbsp olive oil"]:
        r = parse_ingredient_line(line)
        assert r["unit"] == "tbsp"
        assert r["name"] == "olive oil"


def test_pound_variants_normalize():
    assert parse_ingredient_line("3 lbs chicken")["unit"] == "lb"
    assert parse_ingredient_line("1 pound butter")["unit"] == "lb"


def test_fraction_quantity():
    r = parse_ingredient_line("1 1/2 cups sugar")
    assert r["quantity"] == "1 1/2"
    assert r["unit"] == "cup"


def test_parenthetical_between_quantity_and_unit_relocated_to_name():
    """'1 (15 oz) can diced tomatoes' should not let the parenthetical block
    unit detection -- it gets relocated into the name instead of dropped."""
    r = parse_ingredient_line("1 (15 oz) can diced tomatoes")
    assert r["quantity"] == "1"
    assert r["unit"] == "can"
    assert r["name"] == "diced tomatoes (15 oz)"


def test_parenthetical_after_name_unaffected():
    """A parenthetical that isn't between quantity and unit (e.g. a plain
    'to taste' aside with no leading quantity at all) shouldn't be treated
    specially -- confirms the new optional group doesn't over-match."""
    r = parse_ingredient_line("salt (to taste)")
    assert r["quantity"] is None
    assert r["name"] == "salt (to taste)"


def test_no_unit_falls_back_gracefully():
    """No confidently-matched unit -- still searchable/displayable via name,
    just not filterable by amount. Not a crash, not a dropped line."""
    r = parse_ingredient_line("salt to taste")
    assert r["raw_line"] == "salt to taste"
    assert "salt" in r["name"]


def test_blank_line():
    r = parse_ingredient_line("")
    assert r["name"] == ""
    assert r["quantity"] is None


def test_leading_bullet_stripped():
    r = parse_ingredient_line("- 2 cups flour")
    assert r["quantity"] == "2"
    assert r["name"] == "flour"


def test_parse_block_skips_blank_lines():
    lines = ["2 cups flour", "", "  ", "1 tsp salt"]
    results = parse_ingredient_block(lines)
    assert len(results) == 2


def test_a_long_digit_run_parses_at_once():
    from app.ingestion.ingredient_parser import MAX_LINE, parse_ingredient_block, parse_ingredient_line
    started = time.monotonic()
    out = parse_ingredient_block(["1" * 40_000, "2" * 1_000_000])
    assert time.monotonic() - started < 1          # was 3.5 s for the 40,000 alone
    assert [len(i["raw_line"]) for i in out] == [40_000, 1_000_000]    # kept whole: refused on save, not cut
    assert all(len(i["name"]) <= MAX_LINE for i in out)
    # The amounts it reads are unchanged.
    for line, qty in [("1 1/2 cups flour", "1 1/2"), ("3/4 cup sugar", "3/4"), ("1-2 tbsp oil", "1-2"),
                      ("1 to 1 1/2 cups milk", "1 to 1 1/2"), ("2–2½ cups stock", "2–2½"), ("1½ tsp salt", "1½"),
                      ("1,5 tasse de lait", "1,5"), ("2 eggs", "2"), ("1-1/2 cups rice", "1-1/2")]:
        assert parse_ingredient_line(line)["quantity"] == qty, line


def test_an_ingredient_ending_in_a_colon_isnt_a_part_name():
    from app.interchange import _instructions
    from app.text_rules import is_part_name
    assert is_part_name("Sauce:") and is_part_name("Pour la pâte :")
    assert not is_part_name("Salt and pepper to taste:") and not is_part_name("Persil, pour garnir :")
    assert not is_part_name("Just a line") and not is_part_name("one two three four five six seven:")
    assert _instructions(["Salt and pepper to taste:", "Stir."]) == [
        {"@type": "HowToStep", "text": "Salt and pepper to taste:"}, {"@type": "HowToStep", "text": "Stir."}]

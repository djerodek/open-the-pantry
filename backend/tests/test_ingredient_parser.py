import os
import sys

from app.ingestion.ingredient_parser import parse_ingredient_line, parse_ingredient_block  # noqa: E402


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

"""Quebec produce in season, by month, for the recipe list's "In season"
filter (owner, 2026-11-18).

Source: Mangez Québec (Association des producteurs maraîchers du Québec),
"Calendrier des arrivages" 2026, https://mangezquebec.com/info/
calendrier-des-disponibilites-des-fruits-et-legumes-du-quebec/ . Its bars
were read from the two calendar pages (bar colour per month column).

What counts, as the owner asked:
- the calendar's "Haute saison" months; a product with none uses its
  "Disponible" months (haskap, blueberries, blackberries), as do asparagus
  and rhubarb (see their entries); apples use their marked "Saison des
  pommes";
- not greenhouse or indoor produce (greenhouse cucumbers, tomatoes and
  peppers, hydroponic Boston lettuce, mushrooms, endive), nor potatoes,
  whose high season is the whole year;
- not the aromatics in nearly every recipe (garlic, dry onions, shallots,
  herbs), which would make the filter match almost everything from July
  to September. Garlic scapes and green onions count.

Matching follows frontend/js/amounts.js (Weights): only the ingredient's
name is read (up to the first comma, bracket, "or"/"and"); the name found
earliest wins, then the longest ("sweet potato" over "potato", "chou-fleur"
over "chou"). Entries with months None are names that would otherwise match
a shorter entry wrongly ("pomme de terre" isn't "pomme", « échalote
française » isn't the Quebec « échalote », a green onion). A line with a
preserved form (frozen, canned, dried, jam, juice…) doesn't count. Names
are lowercase; accents and plurals are folded in matching.
"""
import re
import unicodedata
from functools import lru_cache

JAN, FEB, MAR, APR, MAY, JUN, JUL, AUG, SEP, OCT, NOV, DEC = range(1, 13)

# (English names, French names, months in high season, or None to block)
PRODUCE = [
    # Spring and early summer
    # Asparagus and rhubarb use the calendar's available months: its high
    # season alone (asparagus June, rhubarb August-September) misses May
    # and June, when Aliments du Québec's harvest guide (2024) and
    # Marchés publics de Montréal place them (review of 0216).
    (["asparagus"], ["asperge"], [MAY, JUN]),
    # Rutabaga (June) and parsnip (July-August) are short next to the usual
    # autumn harvest, but they are the calendar's high-season bars, kept as
    # it shows them (owner, 2026-11-19).
    (["rutabaga"], ["rutabaga"], [JUN]),
    (["strawberry", "strawberries"], ["fraise"], [JUN, JUL, AUG, SEP, OCT]),
    (["romaine", "romaine lettuce"], ["laitue romaine", "romaine"], [JUN, JUL, AUG]),
    (["spinach", "baby spinach"], ["epinard", "bebe epinard", "bebes epinards"], [JUL]),
    (["haskap", "haskap berries", "honeyberries"], ["camerise"], [JUN, JUL]),
    (["garlic scape", "garlic scapes"], ["fleur d'ail", "fleurs d'ail"], [JUL]),
    (["green onion", "spring onion", "scallion"], ["oignon vert", "echalote", "ciboule"], [JUL, AUG]),
    (["bok choy", "pak choi", "pak choy", "baby bok choy"], ["bok choy", "pak choi"], [JUL, AUG]),
    (["parsnip"], ["panais"], [JUL, AUG]),
    (["zucchini", "courgette", "summer squash"], ["courgette", "zucchini"], [JUL, AUG]),
    (["pickling cucumber", "pickling cucumbers"], ["concombre a mariner", "concombres a mariner"], [JUL, AUG, SEP]),
    (["green beans", "yellow beans", "wax beans", "string beans"], ["haricots verts", "haricots jaunes", "haricot vert", "haricot jaune"], [AUG, SEP]),
    (["carrot"], ["carotte"], [JUL, AUG, SEP, OCT, NOV]),
    (["raspberry", "raspberries"], ["framboise"], [JUL, AUG, SEP]),
    (["chicory", "frisee"], ["chicoree", "frisee"], [AUG]),
    (["radicchio"], ["radicchio", "raddichio", "trevise"], [AUG, SEP, OCT]),
    (["blueberry", "blueberries"], ["bleuet", "myrtille"], [JUL, AUG]),
    (["lettuce", "leaf lettuce", "green leaf lettuce", "red leaf lettuce"], ["laitue", "laitue frisee", "laitue en feuilles"], [JUL, AUG]),
    (["radish"], ["radis"], [JUL, AUG, SEP]),
    (["kale"], ["chou kale", "kale", "chou frise"], [JUL, AUG, SEP]),
    (["broccoli"], ["brocoli"], [JUL, AUG, SEP]),
    (["cucumber"], ["concombre"], [JUL, AUG]),
    (["cauliflower"], ["chou-fleur", "chou fleur", "choux-fleurs"], [JUL, AUG, SEP]),
    (["celery"], ["celeri", "branche de celeri", "branches de celeri"], [AUG, SEP]),
    (["kohlrabi"], ["chou-rave", "chou rave"], [AUG, SEP]),
    (["swiss chard", "chard", "rainbow chard"], ["bette a carde", "bettes a carde", "blette", "bette"], [AUG, SEP]),
    (["escarole"], ["scarole"], [AUG, SEP]),
    (["napa cabbage", "nappa cabbage"], ["chou nappa", "chou napa"], [AUG, SEP, OCT]),
    (["sweet corn", "corn on the cob", "ear of corn", "ears of corn", "fresh corn"], ["mais sucre", "epi de mais", "epis de mais", "mais frais"], [AUG]),
    (["cantaloupe", "cantaloup", "muskmelon"], ["cantaloup", "melon cantaloup"], [AUG]),
    (["watermelon"], ["melon d'eau", "pasteque"], [AUG]),
    (["turnip"], ["rabiole", "navet blanc"], [AUG, SEP, OCT]),
    # Late summer and fall
    (["green pepper", "green bell pepper", "bell pepper", "sweet pepper"], ["poivron vert", "poivron"], [AUG, SEP]),
    (["red pepper", "red bell pepper", "yellow pepper", "yellow bell pepper", "orange pepper", "orange bell pepper"],
     ["poivron rouge", "poivron jaune", "poivron orange"], [SEP]),
    (["cabbage", "green cabbage", "white cabbage"], ["chou", "chou vert", "chou blanc", "choux"], [AUG, SEP]),
    (["red cabbage", "purple cabbage"], ["chou rouge"], [AUG, SEP]),
    (["chinese cabbage"], ["chou chinois"], [AUG, SEP]),
    (["ground cherry", "ground cherries", "physalis"], ["cerise de terre", "cerises de terre", "physalis"], [AUG, SEP]),
    (["fennel", "fennel bulb"], ["fenouil", "bulbe de fenouil"], [AUG, SEP]),
    (["eggplant", "aubergine"], ["aubergine"], [AUG, SEP]),
    (["rhubarb"], ["rhubarbe"], [MAY, JUN, JUL, AUG, SEP]),
    (["daikon", "lo bok", "chinese radish"], ["radis chinois", "radis daikon", "daikon"], [SEP]),
    (["leek"], ["poireau"], [SEP]),
    (["beet", "beetroot"], ["betterave"], [SEP]),
    (["tomato", "cherry tomato", "cherry tomatoes", "grape tomatoes", "heirloom tomato"],
     ["tomate", "tomates cerises", "tomate cerise", "tomates raisins"], [SEP]),
    (["blackberry", "blackberries"], ["mure"], [AUG, SEP]),
    (["hot pepper", "chili pepper", "chile pepper", "jalapeno", "serrano pepper", "habanero"],
     ["piment fort", "piment jalapeno", "jalapeno", "piment serrano", "piment"], [SEP]),
    (["celeriac", "celery root"], ["celeri-rave", "celeri rave"], [SEP]),
    (["brussels sprout", "brussels sprouts"], ["chou de bruxelles", "choux de bruxelles"], [SEP, OCT]),
    (["pumpkin", "pie pumpkin"], ["citrouille"], [OCT]),
    (["cranberry", "cranberries"], ["canneberge", "atoca"], [OCT]),
    (["squash", "butternut squash", "butternut", "acorn squash", "spaghetti squash", "winter squash", "kabocha", "delicata"],
     ["courge", "courge musquee", "courge butternut", "courge poivree", "courge spaghetti", "courge d'hiver"], [OCT, NOV]),
    (["apple"], ["pomme"], [SEP, OCT]),
    (["sweet potato", "sweet potatoes", "yam"], ["patate douce", "patates douces", "patate sucree", "patates sucrees"], [NOV, DEC]),
    (["savoy cabbage"], ["chou de savoie", "chou frise de milan"], [NOV, DEC]),
    # Not counted (module docstring): greenhouse, year-round, aromatics.
    (["potato", "potatoes", "baby potatoes"], ["pomme de terre", "pommes de terre", "patate", "grelot"], None),
    (["english cucumber", "mini cucumber", "mini cucumbers", "greenhouse cucumber"],
     ["concombre anglais", "mini concombre", "mini concombres", "concombre de serre"], None),
    (["boston lettuce", "butter lettuce", "bibb lettuce"], ["laitue boston", "laitue beurre"], None),
    (["greenhouse tomato", "hothouse tomato"], ["tomate de serre"], None),
    (["mushroom", "button mushrooms", "cremini", "portobello", "shiitake", "oyster mushrooms"],
     ["champignon", "champignons de paris", "pleurote", "shiitake"], None),
    (["endive", "belgian endive"], ["endive"], None),
    (["shallot"], ["echalote francaise", "echalotes francaises", "echalote seche", "echalotes seches", "echalote grise"], None),
    (["onion", "yellow onion", "red onion", "white onion", "spanish onion", "sweet onion", "pearl onion"],
     ["oignon", "oignon jaune", "oignon rouge", "oignon blanc", "oignon espagnol", "oignon perle"], None),
    (["garlic", "garlic clove", "cloves garlic", "cloves of garlic"], ["ail", "gousse d'ail", "gousses d'ail"], None),
    # Forms of the names above that aren't the produce.
    (["red pepper flakes", "crushed red pepper", "chili powder", "chili flakes", "chili sauce", "hot pepper sauce", "cayenne pepper"],
     ["piment broye", "flocons de piment", "piment de la jamaique", "piment de jamaique", "piment de cayenne", "piment d'espelette", "poudre de chili", "sauce chili", "piments broyes"], None),
    (["tomato paste", "tomato sauce", "tomato puree", "crushed tomatoes", "diced tomatoes", "sun-dried tomatoes", "sundried tomatoes",
      "tomato juice", "canned tomatoes", "whole peeled tomatoes", "passata", "stewed tomatoes"],
     ["pate de tomate", "pate de tomates", "sauce tomate", "coulis de tomate", "tomates broyees", "tomates en des", "tomates sechees",
      "jus de tomate", "tomates en conserve", "tomates pelees", "puree de tomate", "puree de tomates"], None),
    (["pumpkin puree", "pumpkin pie spice", "pumpkin seeds", "pumpkin pie filling"],
     ["puree de citrouille", "graines de citrouille", "epices pour tarte a la citrouille"], None),
    (["applesauce", "apple sauce", "apple juice", "apple cider", "apple cider vinegar", "apple butter"],
     ["compote de pommes", "compote de pomme", "jus de pomme", "cidre", "vinaigre de cidre", "beurre de pomme"], None),
    (["cranberry juice", "cranberry sauce"], ["jus de canneberge", "sauce aux canneberges"], None),
    (["celery seed", "celery salt"], ["graines de celeri", "sel de celeri"], None),
    (["fennel seed", "fennel seeds"], ["graines de fenouil"], None),
    (["corn starch", "cornstarch", "corn syrup", "cornmeal", "corn flour"],
     ["fecule de mais", "sirop de mais", "semoule de mais", "farine de mais", "mais en creme", "mais en grains"], None),
    (["carrot cake"], ["gateau aux carottes"], None),
    (["choux pastry"], ["pate a choux", "choux a la creme"], None),
    # « mûre » is also "ripe": ripe bananas and pears aren't blackberries.
    ([], ["banane mure", "bananes mures", "poire mure", "poires mures", "mangue mure", "mangues mures", "peche mure", "peches mures",
          "bien mure", "bien mures"], None),
]

# A line with any of these isn't fresh produce (anywhere in the line:
# "1 cup blueberries, frozen").
_PRESERVED = re.compile(
    r"(?:^|[^a-z0-9])(?:frozen|canned|tinned|cans?|tins?|jars?|boites?|pots?|dried|dehydrated|jarred|pickled|jam|jelly|preserves|juice|powder|concentrate|"
    r"surgelee?s?|congelee?s?|en conserve|en boite|sechee?s?|deshydratee?s?|en pot|marinee?s?|confiture|gelee|jus|poudre|concentre)"
    r"(?![a-z0-9])")
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_AMOUNT = re.compile(r"^[\s\d.,/\u00bc-\u00be\u2150-\u215e-]+")
_NAME_END = re.compile(r"[,(;/]|\s(?:or|ou|and|et|plus)\s", re.I)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower().replace("’", "'").replace("`", "'")


def _index():
    out = []
    for en, fr, months in PRODUCE:
        mask = 0 if months is None else sum(1 << (m - 1) for m in months)
        for name in en + fr:
            folded = _fold(name)
            words = [re.escape(w) for w in folded.split()]
            # The last word may be plural ("carrot" / "carrots", "choux").
            pattern = re.compile(r"(?:^|[^a-z0-9])(" + r"\s+".join(words) + r")(?:e?s|x)?(?![a-z0-9])")
            out.append((pattern, len(folded), mask))
    return out


_INDEX = _index()


@lru_cache(maxsize=50000)
def line_months(line: str) -> int:
    """The months (bit m-1 for month m) in which this ingredient line's
    produce is in season; 0 when it names none, or one not counted."""
    if not line:
        return 0
    # A preserved form leaves the line out only when every alternative
    # (split at "or") is preserved: "6 tomatoes (or 1 can)", "fresh or
    # frozen" and "frozen or fresh" are fresh produce; "4 cups strawberries
    # (frozen)", "1 (796 ml) can tomatoes" and "frozen or canned corn"
    # aren't (Claude reviews of 0225 and 0228: 0226 dropped brackets, so
    # "(frozen)" counted as fresh, and took only the first alternative).
    alternatives = re.split(r"[\s(\[](?:or|ou)\s", _fold(line))
    if all(_PRESERVED.search(alt) for alt in alternatives):
        return 0
    # Amounts in brackets ("2 cups (500 ml) strawberries") and a leading
    # amount ("1/2 chou") first, so their "(" and "/" don't end the name.
    line = _AMOUNT.sub(" ", _BRACKETS.sub(" ", line))
    end = _NAME_END.search(line)
    folded = _fold(line[:end.start()] if end else line)
    best = None
    for pattern, length, mask in _INDEX:
        m = pattern.search(folded)
        if not m:
            continue
        at = m.start(1)
        if best is None or at < best[0] or (at == best[0] and length > best[1]):
            best = (at, length, mask)
    return best[2] if best else 0


def in_season(lines, month: int) -> bool:
    bit = 1 << (month - 1)
    return any(line_months(line) & bit for line in lines)

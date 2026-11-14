// Amounts in an ingredient list: scaling (½ / 1× / 2×), Weights (volumes
// as grams) and Volumes (weights as cups and spoons), and the two rows of
// buttons for them. Shared by the main app's recipe view (app.js) and What
// to eat?'s reader (picker.js); load weights.js and i18n.js first. Nothing
// here is saved to the recipe: it's the view only.
(function () {
  "use strict";
  const { tx, txn } = self.OTP_I18N || { tx: (s) => s, txn: (n, one, many) => (n === 1 ? one : many) };

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "text") node.textContent = v;
      else if (k === "class") node.className = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children) if (c !== null && c !== undefined) node.append(c);
    return node;
  }
  // Blocked storage (private browsing) throws: the choice then lasts this page.
  function storageGet(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  }
  function storageSet(key, value) {
    try { localStorage.setItem(key, value); } catch { /* lasts this page only */ }
  }

  // "Sauce tartare:", "For the dough:" -- a part of the recipe, shown as a
  // small heading in the lists (same test as the export template).
  function isPartName(text) {
    const t = (text || "").trim();
    // Not an ingredient that ends in ":" ("Salt and pepper to taste:").
    // Same pattern as text_rules.INGREDIENT_TAIL (test_ui_consistency).
    const notAPart = /\b(to taste|for serving|to serve|for garnish|to garnish|for dusting|optional|as needed|au go[uû]t|pour servir|pour garnir|pour d[ée]corer|facultatif|au besoin|en quantit[ée] suffisante|pour (?:la )?friture|for frying|for deep[- ]frying)\b/i;
    return t.endsWith(":") && t.split(/\s+/).length <= 6 && !notAPart.test(t);
  }

  // -------------------------------------------------------------------
  // Scaling (the recipe view's ½ / 1x / 2x buttons). Only the amount at
  // the start of each ingredient line changes ("1 1/2 cups flour" ->
  // "3 cups flour" at 2x; "250 g beurre" -> "125 g beurre" at ½); a line
  // without one ("Salt to taste"), a part name ("Sauce:") and amounts in
  // the steps stay as written. Nothing is saved: it's the view only.
  // -------------------------------------------------------------------
  const UNICODE_FRACTIONS = { "¼": 1 / 4, "½": 1 / 2, "¾": 3 / 4, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 1 / 8,
    "⅜": 3 / 8, "⅝": 5 / 8, "⅞": 7 / 8, "⅕": 1 / 5, "⅖": 2 / 5, "⅗": 3 / 5, "⅘": 4 / 5, "⅙": 1 / 6, "⅚": 5 / 6 };
  const UF = Object.keys(UNICODE_FRACTIONS).join("");
  const AMOUNT = `(?:\\d+\\s+\\d+\\s*/\\s*\\d+|\\d+\\s*[${UF}]|\\d+\\s*/\\s*\\d+|[${UF}]|\\d+(?:[.,]\\d+)?)`;
  const LEADING_AMOUNT = new RegExp(
    `^(\\s*)(${AMOUNT})(?:(\\s*(?:-|–|—|to|à|a|or|ou)\\s*)(${AMOUNT}))?(?=[\\s(]|[^\\d\\s/.,]|$)`, "i");

  function parseAmount(text) {
    const t = text.trim();
    let m;
    if ((m = /^(\d+)\s+(\d+)\s*\/\s*(\d+)$/.exec(t))) return +m[1] + (+m[3] ? +m[2] / +m[3] : 0);
    if ((m = new RegExp(`^(\\d+)\\s*([${UF}])$`).exec(t))) return +m[1] + UNICODE_FRACTIONS[m[2]];
    if ((m = /^(\d+)\s*\/\s*(\d+)$/.exec(t))) return +m[2] ? +m[1] / +m[2] : NaN;
    if (UNICODE_FRACTIONS[t] !== undefined) return UNICODE_FRACTIONS[t];
    return parseFloat(t.replace(",", "."));
  }

  // Written the way the original was: decimals stay decimals (with its
  // comma or point), counts and fractions become fractions, big whole
  // numbers (grams, ml) stay whole.
  const NICE_FRACTIONS = [[1 / 8, "1/8", "⅛"], [1 / 6, "1/6", "⅙"], [1 / 4, "1/4", "¼"], [1 / 3, "1/3", "⅓"],
    [3 / 8, "3/8", "⅜"], [1 / 2, "1/2", "½"], [5 / 8, "5/8", "⅝"], [2 / 3, "2/3", "⅔"], [3 / 4, "3/4", "¾"],
    [5 / 6, "5/6", "⅚"], [7 / 8, "7/8", "⅞"]];
  function formatAmount(value, original) {
    const src = original.trim();
    const decimal = /^\d+[.,]\d+$/.test(src);
    const trimmed = (v) => String(Math.round(v * 100) / 100);
    if (decimal) return src.includes(",") ? trimmed(value).replace(".", ",") : trimmed(value);
    if (/^\d+$/.test(src) && +src >= 10) return String(Math.round(value));
    let whole = Math.floor(value + 1e-9);
    const rest = value - whole;
    if (rest < 0.04) return String(whole);
    if (rest > 0.96) return String(whole + 1);
    const near = NICE_FRACTIONS.reduce((best, n) => (Math.abs(rest - n[0]) < Math.abs(rest - best[0]) ? n : best));
    if (Math.abs(rest - near[0]) >= 0.03) return trimmed(value);
    const unicode = new RegExp(`[${UF}]`).test(src);
    const frac = unicode ? near[2] : near[1];
    if (!whole) return frac;
    return unicode ? `${whole}${frac}` : `${whole} ${frac}`;
  }

  // Left as written rather than scaled wrong (Claude review round 9):
  // "1,000 g" / "1.000 g" could be a thousand or one, so an amount with
  // exactly three digits after its comma or point is ambiguous; and a
  // second amount after the first one's unit ("1 lb 8 oz", "2 tbsp + 1
  // tsp") would stay unscaled while the first changed.
  const AMBIGUOUS_THOUSANDS = /^\d{1,3}[.,]\d{3}$/;
  // Not "00" ("1 cup 00 flour", a flour's grade; Claude review of 0155).
  const SECOND_AMOUNT = /^\s*[a-zA-ZÀ-ÿ.]+\.?\s*(?:\+|plus\b|and\b|et\b)?\s*(?!0+\s)\d+(?:[.,/]\d+)?\s*(?!%)[a-zA-ZÀ-ÿ]/i;

  // The word after a scaled amount follows the new amount: "1 cup" at 2x
  // is "2 cups", "1 1/2 cups" at ½ is "3/4 cup", "1 gousse" at 2x "2
  // gousses". Only the words below (units, containers, portions, eggs);
  // abbreviations (tbsp, g, oz, c. à soupe) never change, and any other word
  // ("3 apples") stays as written. English counts more than 1 as plural,
  // French 2 or more; a word both languages use ("litre") goes by the
  // amount's decimal comma. Case of the first letter is kept.
  const UNIT_WORDS_EN = [
    ["cup", "cups"], ["tablespoon", "tablespoons"], ["teaspoon", "teaspoons"], ["pound", "pounds"],
    ["ounce", "ounces"], ["pint", "pints"], ["quart", "quarts"], ["gallon", "gallons"],
    ["liter", "liters"], ["litre", "litres"], ["milliliter", "milliliters"], ["millilitre", "millilitres"],
    ["gram", "grams"], ["kilogram", "kilograms"], ["clove", "cloves"], ["can", "cans"], ["jar", "jars"],
    ["bottle", "bottles"], ["package", "packages"], ["packet", "packets"], ["bag", "bags"], ["box", "boxes"],
    ["container", "containers"], ["slice", "slices"], ["stick", "sticks"], ["sprig", "sprigs"],
    ["bunch", "bunches"], ["head", "heads"], ["stalk", "stalks"], ["sheet", "sheets"], ["leaf", "leaves"],
    ["pinch", "pinches"], ["dash", "dashes"], ["handful", "handfuls"], ["piece", "pieces"],
    ["inch", "inches"], ["fillet", "fillets"], ["serving", "servings"], ["portion", "portions"],
    ["egg", "eggs"], ["yolk", "yolks"], ["white", "whites"],
  ];
  const UNIT_WORDS_FR = [
    ["tasse", "tasses"], ["cuillère", "cuillères"], ["cuillerée", "cuillerées"], ["gousse", "gousses"],
    ["pincée", "pincées"], ["boîte", "boîtes"], ["conserve", "conserves"], ["tranche", "tranches"],
    ["branche", "branches"], ["feuille", "feuilles"], ["sachet", "sachets"], ["paquet", "paquets"],
    ["botte", "bottes"], ["bâton", "bâtons"], ["morceau", "morceaux"], ["verre", "verres"], ["bol", "bols"],
    ["poignée", "poignées"], ["filet", "filets"], ["litre", "litres"], ["millilitre", "millilitres"],
    ["gramme", "grammes"], ["kilogramme", "kilogrammes"], ["portion", "portions"], ["personne", "personnes"],
    ["part", "parts"], ["œuf", "œufs"], ["jaune", "jaunes"], ["blanc", "blancs"], ["pot", "pots"],
  ];
  function unitWordTable(pairs) {
    const m = new Map();
    for (const [one, many] of pairs) { m.set(one, [one, many]); m.set(many, [one, many]); }
    return m;
  }
  const UNIT_EN = unitWordTable(UNIT_WORDS_EN);
  const UNIT_FR = unitWordTable(UNIT_WORDS_FR);
  // "1 egg yolk": the yolk is what's counted ("2 egg yolks"), not the egg.
  const COUNTED_AFTER = { egg: ["yolk", "yolks", "white", "whites"], "œuf": [] };

  function agreeWord(word, value, frenchAmount) {
    const lower = word.toLowerCase();
    const en = UNIT_EN.get(lower), fr = UNIT_FR.get(lower);
    if (!en && !fr) return word;
    const french = fr && (!en || frenchAmount);
    const [one, many] = french ? fr : en;
    const plural = french ? value >= 2 : value > 1 + 1e-9;
    const out = plural ? many : one;
    return word[0] !== lower[0] ? out[0].toUpperCase() + out.slice(1) : out;
  }

  // rest: the text after the scaled amount(s); value: the last new amount.
  function agreeUnit(rest, value, frenchAmount) {
    const m = /^(\s*)([a-zA-ZÀ-ÿœŒ]+)(?![.\w])(\s+([a-zA-ZÀ-ÿœŒ]+)(?![.\w]))?/.exec(rest);
    if (!m || !m[1]) return rest;
    const first = m[2].toLowerCase();
    const after = m[4] ? m[4].toLowerCase() : "";
    if (COUNTED_AFTER[first] && COUNTED_AFTER[first].includes(after)) {
      return m[1] + m[2] + rest.slice(m[1].length + m[2].length).replace(m[4], agreeWord(m[4], value, frenchAmount));
    }
    return m[1] + agreeWord(m[2], value, frenchAmount) + rest.slice(m[1].length + m[2].length);
  }

  // { text, kept }: kept is true when the line starts with an amount that
  // was left as written (shown with a note), false when it was scaled or
  // has no amount to scale.
  function scaleLineInfo(text, factor) {
    if (factor === 1 || !text || isPartName(text)) return { text, kept: false };
    const m = LEADING_AMOUNT.exec(text);
    if (!m) return { text, kept: false };
    const rest = text.slice(m[0].length);
    const amounts = [m[2], m[4]].filter(Boolean);
    const firstWord = (/^\s*([a-zA-ZÀ-ÿ×]+)/.exec(rest) || [])[1] || "";
    const multiplier = /^(x|×)$/i.test(firstWord);       // "2 x 400 g": the 2 scales
    if (amounts.some((a) => AMBIGUOUS_THOUSANDS.test(a.trim())) || (!multiplier && SECOND_AMOUNT.test(rest))) {
      return { text, kept: true };
    }
    const one = (a) => { const v = parseAmount(a); return Number.isFinite(v) ? formatAmount(v * factor, a) : a; };
    const lastWritten = one(m[4] || m[2]);           // the amount as shown, rounding included
    const shown = parseAmount(lastWritten);
    const unitRest = multiplier || !Number.isFinite(shown)
      ? rest : agreeUnit(rest, shown, /,/.test(lastWritten));
    return { text: m[1] + one(m[2]) + (m[4] ? m[3] + one(m[4]) : "") + unitRest, kept: false };
  }

  function scaleLine(text, factor) {
    return scaleLineInfo(text, factor).text;
  }

  // Servings: the leading amount as for an ingredient, else the only number
  // in the text ("environ 10 croquettes"); null when it can't be scaled.
  function scaleServings(text, factor) {
    if (factor === 1 || !text) return text;
    const info = scaleLineInfo(text, factor);
    if (LEADING_AMOUNT.test(text) && !info.kept) return info.text;
    const numbers = text.match(/\d+(?:[.,]\d+)?/g) || [];
    if (numbers.length !== 1 || AMBIGUOUS_THOUSANDS.test(numbers[0])) return null;
    return text.replace(numbers[0], formatAmount(parseAmount(numbers[0]) * factor, numbers[0]));
  }

  // -------------------------------------------------------------------
  // Weights (the recipe view's Weights button): a volume amount at the
  // start of an ingredient line shown as grams, from the grams-per-cup
  // table in weights.js ("1 cup flour" -> "120 g flour"). Applied after
  // scaling. Weights, counts ("2 eggs") and lines without an amount stay
  // as they are; a volume of something not in the table is left as written
  // and marked. Nothing is saved.
  // -------------------------------------------------------------------
  // Cups in one of each unit. A French "tasse" is the Canadian 250 ml cup;
  // ml and l are converted through the US cup the table is in.
  const US_CUP_ML = 236.588;
  // French spoons, built like ingredient_parser.py's _FR_SPOON / FR_TBSP /
  // FR_TSP ("c. à soupe", "c.à.s.", "c. s.", "cuillère à thé", "c. à c.",
  // "c.c.", "c. t."): tried before the cup, so "c. s." isn't read as "c."
  // (DeepSeek review of 0151). Change both together: test_weights.py checks
  // the forms listed in its FRENCH_SPOONS agree; add new forms there. This
  // side is a little more lenient: the final dot of "c. à t." / "c. à c."
  // may be missing.
  const FR_SPOON = String.raw`(?:c\.?\s*[àa]\.?\s*|cuil\.\s*[àa]\s*|cuill?(?:[eè]re|er[ée]e)s?\s+[àa]\s+)`;
  const VOLUME_UNITS = [
    [new RegExp(String.raw`^(?:${FR_SPOON}(?:soupe|table|s\.?)|c\.\s*s\.|cs)(?![\w])`, "i"), 15 / US_CUP_ML],
    [new RegExp(String.raw`^(?:${FR_SPOON}(?:th[ée]|the|caf[ée]|t\.?|c\.?)|c\.\s*[tc]\.|ct)(?![\w])`, "i"), 5 / US_CUP_ML],
    [/^(?:tasses?)(?![\w])/i, 250 / US_CUP_ML],
    // Not a bare lowercase "c" (ingredient_parser.py doesn't take it for a
    // unit either); "c." but not "c." then a spoon's letters that the
    // patterns above didn't read (left alone rather than read as a cup).
    [/^(?:cups?|c\.(?!\s*(?:[àa](?![\w])|[stc]\.)))(?![\w])/i, 1],
    [/^C(?=\s)/, 1],
    [/^(?:tablespoons?|tbsps?\.?|tbs\.?|tbl\.?|tblsp\.?)(?![\w])/i, 1 / 16],
    [/^T(?=\s)/, 1 / 16],
    [/^(?:teaspoons?|tsps?\.?)(?![\w])/i, 1 / 48],
    [/^t(?=\s)/, 1 / 48],
    [/^(?:fl\.?\s?oz\.?|fluid\s+ounces?)(?![\w])/i, 1 / 8],
    [/^(?:ml|mls|millilit(?:er|re)s?)(?![\w])/i, 1 / US_CUP_ML],
    [/^(?:l|lit(?:er|re)s?)(?![\w'’])/i, 1000 / US_CUP_ML],
  ];
  const STICK_UNIT = /^(?:sticks?|bâtons?)(?![\w])/i;

  function foldText(s) {
    return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[’`]/g, "'");
  }
  let weightIndex = null;
  function weightEntries() {
    if (weightIndex) return weightIndex;
    const out = [];
    for (const entry of (self.OTP_INGREDIENT_WEIGHTS || [])) {
      for (const name of [...entry.en, ...entry.fr]) {
        const folded = foldText(name);
        const words = folded.split(/\s+/).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
        // The last word may be plural ("almond" / "almonds", "noix").
        const pattern = new RegExp(`(?:^|[^a-z0-9])(${words.join("\\s+")})(?:e?s|x)?(?![a-z0-9])`);
        out.push({ pattern, length: folded.length, entry });
      }
    }
    weightIndex = out;
    return out;
  }
  // The ingredient's name is what comes before the first comma,
  // parenthesis, slash, or "or"/"and" ("2 tsp honey (or agave)", "1 cup
  // water or broth"): a word further on ("... and sea salt") isn't it. Of
  // the names found there, the one starting first wins, then the longest:
  // "brown sugar" over "sugar", "kosher salt" (unknown weight) over "salt",
  // "sun-dried tomatoes in oil" over "oil".
  const NAME_END = /[,(;/]|\s(?:or|ou|and|et|plus)\s/i;
  // Words that change what a cup weighs, outside the name found ("2 cups
  // cooked rice" isn't dry rice, "whipped cream" isn't cream, "sugar
  // substitute" isn't sugar): the line is left as written unless the table
  // has that form ("cooked rice"). Claude review of 0155.
  const DENSITY_WORDS = /(?:^|[^a-z0-9])(?:cooked|cuite?s?|whipped|fouettee?s?|substitutes?|substitut|succedane)(?![a-z0-9])/g;
  const NOT_WEIGHED = { g: null };
  function ingredientWeightEntry(name) {
    // "45 ml (3 tablespoons) of olive oil": the same amount again, first.
    name = name.replace(/^\s*\([^)]*\)\s*(?:of\s+|de\s+|d['’]\s*)?/i, " ");
    // One name, not cut at its "and" ("half & half" too: Claude review of 0165).
    name = name.replace(/\bhalf[\s-]*(?:and|&)[\s-]*half\b/gi, "half-and-half");
    const end = NAME_END.exec(name);
    const folded = foldText(end ? name.slice(0, end.index) : name);
    let best = null;
    for (const e of weightEntries()) {
      const m = e.pattern.exec(folded);
      if (!m) continue;
      const at = m.index + m[0].indexOf(m[1]);
      if (!best || at < best.at || (at === best.at && e.length > best.length)) best = { at, length: e.length, entry: e.entry };
    }
    if (!best) return null;
    for (const w of folded.matchAll(DENSITY_WORDS)) {
      const word = w.index + w[0].search(/[a-z]/);
      // "riz non cuit" is uncooked: dry rice, as "uncooked rice" already
      // was (Claude review of 0165). No lookbehind: older Safari.
      if (/(?:^|[^a-z0-9])(?:non|pas)[\s-]+$/.test(folded.slice(0, word))) continue;
      if (word < best.at || word >= best.at + best.length) return NOT_WEIGHED;
    }
    return best.entry;
  }
  function formatGrams(g, french) {
    let v;
    if (g <= 0) v = 0;                                        // "0 cup" stays 0 (GPT review of 0155)
    else if (g < 1) v = Math.max(0.1, Math.round(g * 10) / 10);   // "0.2 g", never "0 g"
    else if (g < 10) v = Math.round(g * 2) / 2;
    else if (g < 100) v = Math.round(g);
    else v = Math.round(g / 5) * 5;
    const s = String(v);
    return french ? s.replace(".", ",") : s;
  }

  // { text, kept, converted }: kept marks a volume line that couldn't be
  // weighed (shown "as written"); converted, one that was. lang "fr": a
  // decimal comma ("0,8 g"), as an amount written with one also gets.
  function weightLineInfo(text, lang) {
    if (!text || isPartName(text)) return { text, kept: false, converted: false };
    const m = LEADING_AMOUNT.exec(text);
    if (!m) return { text, kept: false, converted: false };
    const afterAmount = text.slice(m[0].length);
    const spaced = afterAmount.replace(/^\s+/, "");
    let cups = null, unitLen = 0, stick = false;
    for (const [re, perUnit] of VOLUME_UNITS) {
      const u = re.exec(spaced);
      if (u) { cups = perUnit; unitLen = u[0].length; break; }
    }
    if (cups === null) {
      const s = STICK_UNIT.exec(spaced);
      if (s) { cups = 0.5; unitLen = s[0].length; stick = true; }
    }
    if (cups === null) return { text, kept: false, converted: false };
    const rest = spaced.slice(unitLen);
    const amounts = [m[2], m[4]].filter(Boolean);
    // "3/4 tsp each salt and pepper": one amount for two things.
    const entry = /^\s*(?:each|chacun|chacune)\b/i.test(rest) ? null : ingredientWeightEntry(rest);
    // A stick is a volume only for butter: "1 stick cinnamon" is a count,
    // not a volume to weigh (Claude review of 0155).
    if (stick && !(entry && entry.stick)) return { text, kept: false, converted: false };
    if (!entry || entry.g === null
        || amounts.some((a) => AMBIGUOUS_THOUSANDS.test(a.trim())) || SECOND_AMOUNT.test(afterAmount)) {
      return { text, kept: true, converted: false };
    }
    const values = amounts.map(parseAmount);
    if (!values.every(Number.isFinite)) return { text, kept: true, converted: false };
    const french = lang === "fr" || amounts.some((a) => a.includes(","));
    const grams = values.map((v) => formatGrams(v * cups * entry.g, french));
    const shown = grams.length === 2 ? `${grams[0]}${m[3]}${grams[1]}` : grams[0];
    const tail = /^\s/.test(rest) ? rest : ` ${rest}`;      // "of" / "de" kept: "120 g de farine"
    return { text: `${m[1]}${shown} g${tail}`, kept: false, converted: true };
  }

  // -------------------------------------------------------------------
  // Volumes (the recipe view's Volumes choice): the reverse of Weights, from
  // the same table. A weight at the start of a line (g, kg, oz, lb) shown as
  // a volume in the recipe's language: US cups, tbsp, tsp in English;
  // tasse (250 ml), c. à soupe, c. à thé in French. Kitchen fractions; an
  // ingredient the table doesn't know is left as written and marked.
  // -------------------------------------------------------------------
  const WEIGHT_UNITS = [
    [/^(?:kg|kilos?|kilogram(?:me)?s?)(?![\w])/i, 1000],
    [/^(?:g|gr\.?|grams?|grammes?)(?![\w])/i, 1],
    [/^(?:oz\.?|ounces?|onces?)(?![\w])/i, 28.3495],
    [/^(?:lbs?\.?|pounds?|livres?)(?![\w])/i, 453.592],
  ];
  // [cups per unit, singular, plural]; the first unit used when the amount
  // is at least a quarter of it, the spoons below that.
  const VOLUME_OUT = {
    en: { big: [1, "cup", "cups"], spoon: [1 / 16, "tbsp", "tbsp"], small: [1 / 48, "tsp", "tsp"] },
    fr: { big: [250 / US_CUP_ML, "tasse", "tasses"], spoon: [15 / US_CUP_ML, "c. à soupe", "c. à soupe"],
          small: [5 / US_CUP_ML, "c. à thé", "c. à thé"] },
  };
  const CUP_FRACTIONS = [[0, ""], [1 / 4, "1/4"], [1 / 3, "1/3"], [1 / 2, "1/2"], [2 / 3, "2/3"], [3 / 4, "3/4"], [1, ""]];
  const SPOON_FRACTIONS = [[0, ""], [1 / 2, "1/2"], [1, ""]];
  const TSP_FRACTIONS = [[0, ""], [1 / 8, "1/8"], [1 / 4, "1/4"], [1 / 2, "1/2"], [3 / 4, "3/4"], [1, ""]];
  // { text, value }: the nearest whole-and-fraction ("1 1/2"), at least the
  // smallest fraction (never "0"): a tiny amount shows as 1/8 tsp, which can
  // be many times what it is (0.01 g of cayenne).
  function kitchenAmount(v, fractions) {
    let whole = Math.floor(v);
    let best = fractions[0];
    for (const f of fractions) if (Math.abs(v - whole - f[0]) < Math.abs(v - whole - best[0])) best = f;
    if (best[0] === 1) { whole += 1; best = fractions[0]; }
    if (!whole && !best[1]) best = fractions[1];
    const text = whole && best[1] ? `${whole} ${best[1]}` : (best[1] || String(whole));
    return { text, value: whole + best[0] };
  }
  function volumeUnitFor(cups, lang) {
    const out = VOLUME_OUT[lang];
    if (cups / out.big[0] >= 0.25 - 1e-9) return [out.big, CUP_FRACTIONS];
    if (cups / out.spoon[0] >= 1 - 1e-9) return [out.spoon, SPOON_FRACTIONS];
    return [out.small, TSP_FRACTIONS];
  }

  // { text, kept, converted }, as weightLineInfo. lang: "en" or "fr".
  function volumeLineInfo(text, lang) {
    if (!text || isPartName(text)) return { text, kept: false, converted: false };
    const m = LEADING_AMOUNT.exec(text);
    if (!m) return { text, kept: false, converted: false };
    const afterAmount = text.slice(m[0].length);
    const spaced = afterAmount.replace(/^\s+/, "");
    let grams = null, unitLen = 0;
    for (const [re, perUnit] of WEIGHT_UNITS) {
      const u = re.exec(spaced);
      if (u) { grams = perUnit; unitLen = u[0].length; break; }
    }
    if (grams === null) return { text, kept: false, converted: false };
    const rest = spaced.slice(unitLen);
    const amounts = [m[2], m[4]].filter(Boolean);
    const entry = /^\s*(?:each|chacun|chacune)\b/i.test(rest) ? null : ingredientWeightEntry(rest);
    if (!entry || entry.g === null
        || amounts.some((a) => AMBIGUOUS_THOUSANDS.test(a.trim())) || SECOND_AMOUNT.test(afterAmount)) {
      return { text, kept: true, converted: false };
    }
    const values = amounts.map(parseAmount);
    if (!values.every(Number.isFinite) || values.some((v) => v <= 0)) return { text, kept: true, converted: false };
    const cups = values.map((v) => (v * grams) / entry.g);
    const out = lang === "fr" ? "fr" : "en";
    const unitName = (unit, value) => ((out === "fr" ? value >= 2 : value > 1) ? unit[2] : unit[1]);
    // One unit for both ends of a range, chosen by the larger; but when
    // that rounds the smaller end more than a quarter off ("1-100 g flour"
    // read "1/4-3/4 cup": 1 g as 30 g), each end gets its own unit
    // ("1/2 tsp-3/4 cup"). GPT review of 0155.
    const [unit, fractions] = volumeUnitFor(Math.max(...cups), out);
    let shown = cups.map((c) => kitchenAmount(c / unit[0], fractions));
    const tail = /^\s/.test(rest) ? rest : ` ${rest}`;
    if (cups.some((c, k) => Math.abs(shown[k].value * unit[0] - c) > 0.25 * c)) {
      const ends = cups.map((c) => {
        const [u, f] = volumeUnitFor(c, out);
        const a = kitchenAmount(c / u[0], f);
        return `${a.text} ${unitName(u, a.value)}`;
      });
      return { text: `${m[1]}${ends.join(m[3] || "")}${tail}`, kept: false, converted: true };
    }
    const amountText = shown.length === 2 ? `${shown[0].text}${m[3]}${shown[1].text}` : shown[0].text;
    return { text: `${m[1]}${amountText} ${unitName(unit, shown[shown.length - 1].value)}${tail}`, kept: false, converted: true };
  }

  // The three buttons; onChange(factor) runs now (at 1x) and on each press.
  // A checkbox and its line, in one label so the whole line can be tapped.
  // The <li> around it is marked while ticked (crossed out).
  // lead (optional): drawn between the box and the text, not crossed out
  // (a step's number).
  function tickRow(content, on, onChange, lead = null) {
    const box = el("input", { type: "checkbox", class: "tick-box" });
    box.checked = on;
    box.addEventListener("change", () => {
      box.closest("li")?.classList.toggle("is-ticked", box.checked);
      onChange(box.checked);
    });
    return el("label", { class: "tick-row" }, [box, lead, el("span", { class: "tick-text" }, content)]);
  }

  function scaleButtons(onChange) {
    const choices = [[0.5, "½", tx("Half the quantities")], [1, "1×", tx("Quantities as written")], [2, "2×", tx("Double the quantities")]];
    const note = el("p", { class: "scale-note", role: "status", hidden: "" });
    const buttons = choices.map(([factor, label, name]) => el("button", {
      type: "button", class: "scale-button", "aria-pressed": String(factor === 1), "aria-label": name, title: name, text: label,
      onclick: () => {
        buttons.forEach((b, i) => b.setAttribute("aria-pressed", String(choices[i][0] === factor)));
        onChange(factor);
      },
    }));
    const control = { element: el("div", { class: "scale-buttons", role: "group", "aria-label": tx("Quantities") }, buttons), note };
    queueMicrotask(() => onChange(1));
    return control;
  }


  // The controls and the list for one recipe. lines: the ingredient lines
  // as written; servings: the recipe's servings text; language: "en"/"fr"
  // (the recipe's, for Volumes). Returns { scale, units, note, list }: the
  // two button rows, the status line under them and the <ul>. Scaling is
  // back to 1× each time; the units choice is kept on this device.
  // ticks (optional, the recipe view's checkboxes): { isTicked(i, line),
  // toggle(i, line, on) }, by the line's place and text as written, so a
  // tick survives scaling and a change of units.
  function ingredientControls({ lines, servings, language, listClass = "ingredients-list", ticks = null }) {
    const ingredientsList = el("ul", { class: listClass });
    let currentFactor = 1;
    const savedUnits = storageGet("recipe-app-units");
    let unitsMode = ["written", "weights", "volumes"].includes(savedUnits) ? savedUnits
      : (storageGet("recipe-app-weights") === "1" ? "weights" : "written");   // the 0150 setting
    const volumeLang = language === "fr" ? "fr" : "en";
    const unitChoices = [["written", tx("As written"), tx("Amounts as written")],
                         ["weights", tx("Weights"), tx("Show volumes as weights")],
                         ["volumes", tx("Volumes"), tx("Show weights as volumes")]];
    const unitButtons = unitChoices.map(([mode, label, name]) => el("button", {
      type: "button", class: `scale-button units-button units-${mode}`, "aria-pressed": String(unitsMode === mode),
      title: name, "aria-label": name, text: label,
      onclick: () => {
        unitsMode = mode;
        storageSet("recipe-app-units", mode);
        unitButtons.forEach((b, k) => b.setAttribute("aria-pressed", String(unitChoices[k][0] === mode)));
        renderIngredients(currentFactor);
      },
    }));
    const unitsControl = el("div", { class: "units-control" }, [
      el("div", { class: "scale-buttons units-buttons", role: "group", "aria-label": tx("Units") }, unitButtons),
      el("p", { class: "units-disclaimer", text: tx("Conversions are approximate; adjust as needed.") }),
    ]);
    const renderIngredients = (factor) => {
      currentFactor = factor;
      let kept = 0, unconverted = 0, converted = 0;
      ingredientsList.replaceChildren(...lines.map((text, index) => {
        const info = scaleLineInfo(text, factor);
        let shown = info.text, mark = info.kept;
        if (info.kept) kept += 1;
        else if (unitsMode !== "written") {
          const w = unitsMode === "weights" ? weightLineInfo(info.text, volumeLang) : volumeLineInfo(info.text, volumeLang);
          shown = w.text;
          if (w.kept) { unconverted += 1; mark = true; }
          if (w.converted) converted += 1;
        }
        const content = [
          document.createTextNode(shown),
          mark ? el("span", { class: "unscaled-mark", text: ` (${tx("as written")})` }) : null,
        ];
        if (isPartName(text)) return el("li", { class: "list-part" }, content);
        const li = el("li", { class: mark ? "unscaled" : null },
          ticks ? [tickRow(content, ticks.isTicked(index, text), (on) => ticks.toggle(index, text, on))] : content);
        if (ticks && ticks.isTicked(index, text)) li.classList.add("is-ticked");
        return li;
      }));
      const parts = [];
      if (unitsMode === "weights" && unconverted) parts.push(txn(unconverted, "{n} volume with no known weight, left as written.", "{n} volumes with no known weight, left as written."));
      if (unitsMode === "volumes" && unconverted) parts.push(txn(unconverted, "{n} weight with no known volume, left as written.", "{n} weights with no known volume, left as written."));
      if (factor !== 1 && servings) {
        const scaled = scaleServings(servings, factor);
        parts.push(scaled === null
          ? tx("Servings: {1}", { 1: `${servings} (${tx("as written")})` })
          : tx("Servings: {1}", { 1: scaled }));
      }
      if (factor !== 1 && kept) parts.push(txn(kept, "{n} line left as written: check it by hand.", "{n} lines left as written: check them by hand."));
      if (factor !== 1 || converted) parts.push(tx("Amounts in the steps stay as written."));
      scaleControl.note.textContent = parts.join(" · ");
      scaleControl.note.hidden = !parts.length;
    };
    const scaleControl = scaleButtons(renderIngredients);
    return { scale: scaleControl.element, units: unitsControl, note: scaleControl.note, list: ingredientsList };
  }

  self.OTP_AMOUNTS = { isPartName, scaleLine, scaleLineInfo, scaleServings, weightLineInfo, volumeLineInfo, ingredientControls, tickRow };
})();

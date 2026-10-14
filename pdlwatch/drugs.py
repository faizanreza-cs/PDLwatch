"""Drug dictionary: therapeutic classes, the products PDLs list in them, and name matching.

A PDL row names a *product*: a brand (``Januvia``) or a generic (``alogliptin``), and the two can
have different statuses. Brand preferred over generic is common in Medicaid because supplemental
rebates can make the brand cheaper. So rows are keyed by product, and each product carries its
ingredient for analysis by molecule.

Matching tolerates what PDFs and OCR do to names: trademark symbols, footnote markers, upper
case, dosage forms, "Brand (generic)" pairs, hyphenated or slashed combinations and the odd
misread letter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from rapidfuzz import fuzz, process


@dataclass(frozen=True)
class Product:
    name: str  # as a PDL prints it: brand name, or generic name in lower case
    ingredient: str
    kind: str  # "brand" | "generic"
    drug_class: str


# class -> [(name, ingredient)]; a name in lower case is a generic (or an unbranded biologic)
_CLASSES: dict[str, list[tuple[str, str]]] = {
    "GLP-1 Receptor Agonists": [
        ("Ozempic", "semaglutide"), ("Rybelsus", "semaglutide"), ("Trulicity", "dulaglutide"),
        ("Victoza", "liraglutide"), ("liraglutide", "liraglutide"), ("Byetta", "exenatide"),
        ("Bydureon BCise", "exenatide"), ("Mounjaro", "tirzepatide"),
    ],
    "Anti-Obesity Agents": [
        ("Wegovy", "semaglutide"), ("Zepbound", "tirzepatide"), ("Saxenda", "liraglutide"),
        ("Qsymia", "phentermine/topiramate"), ("Contrave", "naltrexone/bupropion"),
        ("phentermine", "phentermine"),
    ],
    "SGLT2 Inhibitors": [
        ("Jardiance", "empagliflozin"), ("Farxiga", "dapagliflozin"),
        ("Invokana", "canagliflozin"), ("Steglatro", "ertugliflozin"),
        ("Brenzavvy", "bexagliflozin"), ("Synjardy", "empagliflozin/metformin"),
        ("Xigduo XR", "dapagliflozin/metformin"),
    ],
    "DPP-4 Inhibitors": [
        ("Januvia", "sitagliptin"), ("Zituvio", "sitagliptin"),
        ("Janumet", "sitagliptin/metformin"),
        ("Tradjenta", "linagliptin"), ("Onglyza", "saxagliptin"), ("saxagliptin", "saxagliptin"),
        ("Nesina", "alogliptin"), ("alogliptin", "alogliptin"),
    ],
    "Insulins, Long-Acting": [
        ("Lantus", "insulin glargine"), ("insulin glargine", "insulin glargine"),
        ("Basaglar", "insulin glargine"), ("Semglee", "insulin glargine-yfgn"),
        ("insulin glargine-yfgn", "insulin glargine-yfgn"), ("Rezvoglar", "insulin glargine-aglr"),
        ("Toujeo", "insulin glargine"), ("Tresiba", "insulin degludec"),
        ("insulin degludec", "insulin degludec"),
    ],
    "Insulins, Rapid-Acting": [
        ("Humalog", "insulin lispro"), ("insulin lispro", "insulin lispro"),
        ("Admelog", "insulin lispro"), ("Lyumjev", "insulin lispro-aabc"),
        ("Novolog", "insulin aspart"), ("insulin aspart", "insulin aspart"),
        ("Fiasp", "insulin aspart"), ("Apidra", "insulin glulisine"),
    ],
    "Anticoagulants, Oral": [
        ("Eliquis", "apixaban"), ("Xarelto", "rivaroxaban"), ("Pradaxa", "dabigatran"),
        ("dabigatran", "dabigatran"), ("Savaysa", "edoxaban"), ("warfarin", "warfarin"),
        ("Jantoven", "warfarin"),
    ],
    "Hepatitis C Agents": [
        ("Mavyret", "glecaprevir/pibrentasvir"), ("Epclusa", "sofosbuvir/velpatasvir"),
        ("sofosbuvir/velpatasvir", "sofosbuvir/velpatasvir"),
        ("Harvoni", "ledipasvir/sofosbuvir"), ("ledipasvir/sofosbuvir", "ledipasvir/sofosbuvir"),
        ("Vosevi", "sofosbuvir/velpatasvir/voxilaprevir"), ("Zepatier", "elbasvir/grazoprevir"),
        ("Sovaldi", "sofosbuvir"),
    ],
    "Antimigraine Agents, CGRP": [
        ("Aimovig", "erenumab"), ("Ajovy", "fremanezumab"), ("Emgality", "galcanezumab"),
        ("Nurtec ODT", "rimegepant"), ("Ubrelvy", "ubrogepant"), ("Qulipta", "atogepant"),
        ("Zavzpret", "zavegepant"),
    ],
    "Cytokine and CAM Antagonists": [
        ("Humira", "adalimumab"), ("Hyrimoz", "adalimumab-adaz"),
        ("adalimumab-adaz", "adalimumab-adaz"), ("Hadlima", "adalimumab-bwwd"),
        ("Cyltezo", "adalimumab-adbm"), ("adalimumab-adbm", "adalimumab-adbm"),
        ("Amjevita", "adalimumab-atto"), ("Simlandi", "adalimumab-ryvk"),
        ("Yusimry", "adalimumab-aqvh"), ("Enbrel", "etanercept"), ("Xeljanz", "tofacitinib"),
        ("Rinvoq", "upadacitinib"), ("Otezla", "apremilast"), ("Cosentyx", "secukinumab"),
        ("Taltz", "ixekizumab"), ("Skyrizi", "risankizumab"), ("Tremfya", "guselkumab"),
        ("Stelara", "ustekinumab"), ("Wezlana", "ustekinumab-auub"),
        ("Selarsdi", "ustekinumab-aekn"), ("Kevzara", "sarilumab"), ("Orencia", "abatacept"),
        ("Cimzia", "certolizumab pegol"),
    ],
    "Antipsychotics, Atypical": [
        ("aripiprazole", "aripiprazole"), ("Rexulti", "brexpiprazole"), ("Vraylar", "cariprazine"),
        ("Caplyta", "lumateperone"), ("lurasidone", "lurasidone"), ("Latuda", "lurasidone"),
        ("olanzapine", "olanzapine"), ("quetiapine", "quetiapine"),
        ("risperidone", "risperidone"), ("ziprasidone", "ziprasidone"),
        ("paliperidone", "paliperidone"), ("Invega", "paliperidone"), ("Fanapt", "iloperidone"),
        ("asenapine", "asenapine"), ("Saphris", "asenapine"), ("clozapine", "clozapine"),
        ("Lybalvi", "olanzapine/samidorphan"), ("Cobenfy", "xanomeline/trospium"),
    ],
    "ADHD Stimulants": [
        ("methylphenidate ER", "methylphenidate"), ("Concerta", "methylphenidate"),
        ("Vyvanse", "lisdexamfetamine"), ("lisdexamfetamine", "lisdexamfetamine"),
        ("amphetamine salts ER", "amphetamine/dextroamphetamine"),
        ("Adderall XR", "amphetamine/dextroamphetamine"),
        ("dexmethylphenidate ER", "dexmethylphenidate"), ("Focalin XR", "dexmethylphenidate"),
        ("Azstarys", "serdexmethylphenidate/dexmethylphenidate"),
        ("Mydayis", "amphetamine/dextroamphetamine"), ("Jornay PM", "methylphenidate"),
        ("Quillichew ER", "methylphenidate"), ("Dyanavel XR", "amphetamine"),
    ],
    "Inhaled Corticosteroid/LABA Combinations": [
        ("Advair Diskus", "fluticasone/salmeterol"), ("Wixela Inhub", "fluticasone/salmeterol"),
        ("fluticasone/salmeterol", "fluticasone/salmeterol"),
        ("Symbicort", "budesonide/formoterol"), ("Breyna", "budesonide/formoterol"),
        ("budesonide/formoterol", "budesonide/formoterol"),
        ("Dulera", "mometasone/formoterol"), ("Breo Ellipta", "fluticasone furoate/vilanterol"),
        ("AirDuo RespiClick", "fluticasone/salmeterol"),
    ],
    "Lipotropics, Other": [
        ("Repatha", "evolocumab"), ("Praluent", "alirocumab"), ("Leqvio", "inclisiran"),
        ("Nexletol", "bempedoic acid"), ("Nexlizet", "bempedoic acid/ezetimibe"),
        ("ezetimibe", "ezetimibe"),
    ],
    "HIV Antiretrovirals": [
        ("Biktarvy", "bictegravir/emtricitabine/tenofovir alafenamide"),
        ("Genvoya", "elvitegravir/cobicistat/emtricitabine/tenofovir alafenamide"),
        ("Descovy", "emtricitabine/tenofovir alafenamide"),
        ("Truvada", "emtricitabine/tenofovir disoproxil fumarate"),
        ("emtricitabine/tenofovir DF", "emtricitabine/tenofovir disoproxil fumarate"),
        ("Triumeq", "abacavir/dolutegravir/lamivudine"), ("Dovato", "dolutegravir/lamivudine"),
        ("Juluca", "dolutegravir/rilpivirine"),
        ("Odefsey", "emtricitabine/rilpivirine/tenofovir alafenamide"),
        ("Symtuza", "darunavir/cobicistat/emtricitabine/tenofovir alafenamide"),
        ("Tivicay", "dolutegravir"),
    ],
    "Opioid Use Disorder Agents": [
        ("buprenorphine/naloxone", "buprenorphine/naloxone"),
        ("Suboxone", "buprenorphine/naloxone"), ("Zubsolv", "buprenorphine/naloxone"),
        ("buprenorphine", "buprenorphine"), ("Sublocade", "buprenorphine"),
        ("Brixadi", "buprenorphine"), ("Vivitrol", "naltrexone"), ("naltrexone", "naltrexone"),
    ],
    "Proton Pump Inhibitors": [
        ("omeprazole", "omeprazole"), ("pantoprazole", "pantoprazole"),
        ("esomeprazole", "esomeprazole"), ("lansoprazole", "lansoprazole"),
        ("rabeprazole", "rabeprazole"), ("Nexium", "esomeprazole"), ("Protonix", "pantoprazole"),
        ("Dexilant", "dexlansoprazole"), ("dexlansoprazole", "dexlansoprazole"),
    ],
    "Multiple Sclerosis Agents": [
        ("glatiramer", "glatiramer"), ("Copaxone", "glatiramer"), ("Glatopa", "glatiramer"),
        ("dimethyl fumarate", "dimethyl fumarate"), ("Tecfidera", "dimethyl fumarate"),
        ("Vumerity", "diroximel fumarate"), ("fingolimod", "fingolimod"),
        ("Gilenya", "fingolimod"), ("teriflunomide", "teriflunomide"),
        ("Aubagio", "teriflunomide"), ("Mayzent", "siponimod"), ("Zeposia", "ozanimod"),
        ("Kesimpta", "ofatumumab"), ("Mavenclad", "cladribine"),
        ("Bafiertam", "monomethyl fumarate"), ("Ponvory", "ponesimod"),
    ],
    "Immunomodulators, Asthma": [
        ("Xolair", "omalizumab"), ("Nucala", "mepolizumab"), ("Fasenra", "benralizumab"),
        ("Tezspire", "tezepelumab"), ("Dupixent", "dupilumab"),
    ],
    "Smoking Cessation Agents": [
        ("varenicline", "varenicline"), ("bupropion SR", "bupropion"),
        ("nicotine patch", "nicotine"), ("nicotine gum", "nicotine"),
        ("nicotine lozenge", "nicotine"), ("Nicotrol inhaler", "nicotine"),
        ("Nicotrol NS", "nicotine"),
    ],
}  # fmt: skip

# Other names PDFs use for the same product
_ALIASES: dict[str, list[str]] = {
    "Advair Diskus": ["Advair"],
    "Bydureon BCise": ["Bydureon", "Bydureon BCISE"],
    "Nurtec ODT": ["Nurtec"],
    "Breo Ellipta": ["Breo"],
    "AirDuo RespiClick": ["AirDuo", "Airduo"],
    "Wixela Inhub": ["Wixela"],
    "Xigduo XR": ["Xigduo"],
    "methylphenidate ER": ["methylphenidate extended-release", "methylphenidate ext-rel"],
    "dexmethylphenidate ER": ["dexmethylphenidate extended-release"],
    "amphetamine salts ER": ["amphetamine/dextroamphetamine ER", "dextroamphetamine-amphetamine ER",
                             "mixed amphetamine salts ER"],
    "emtricitabine/tenofovir DF": ["emtricitabine/tenofovir disoproxil fumarate",
                                   "emtricitabine-tenofovir DF"],
    "bupropion SR": ["bupropion sustained-release"],
    "Novolog": ["NovoLog"],
    "Humalog": ["HumaLOG"],
    "insulin glargine": ["insulin glargine (unbranded Lantus)"],
    "Nicotrol NS": ["Nicotrol nasal spray"],
}  # fmt: skip

PRODUCTS: list[Product] = [
    Product(name, ingredient, "generic" if name[0].islower() else "brand", cls)
    for cls, items in _CLASSES.items()
    for name, ingredient in items
]
CLASSES: list[str] = list(_CLASSES)
BY_NAME: dict[str, Product] = {p.name: p for p in PRODUCTS}

# Words that describe the package, not the product
_FORMS = (
    r"pens?|tablets?|tabs?|capsules?|caps?|films?|injection|injectable|inhaler|inhalation|"
    r"powder|solution|vials?|kwikpen|flexpen|flextouch|solostar|cartridges?|syringes?|"
    r"auto-?injector|prefilled|pfs|sensoready|sprinkle|chewable|oral|subcutaneous|"
    r"sublingual|kit|starter|pack|kwik pen|tempo pen"
)
_STRENGTH = (
    r"\d+(\.\d+)?\s*(mg|mcg|g|ml|units?|unit/ml|%|mg/ml|mcg/act)"
    r"(\s*/\s*\d*(\.\d+)?\s*(ml|act|day|dose))?"
)
_MARKERS = r"[*†‡§#^¶®™©]"


def normalise(text: str) -> str:
    """Lower case, markers and package words removed, separators unified."""
    t = re.sub(_MARKERS, " ", text)
    t = t.replace("–", "-").replace("—", "-")
    t = t.lower()
    t = re.sub(_STRENGTH, " ", t)
    t = re.sub(rf"\b({_FORMS})\b", " ", t)
    t = re.sub(r"\s*/\s*", "/", t)
    t = re.sub(r"[^a-z0-9/\- ]", " ", t)
    return " ".join(t.split())


@lru_cache(maxsize=1)
def _index() -> dict[str, Product]:
    idx: dict[str, Product] = {}
    for p in PRODUCTS:
        for name in [p.name, *_ALIASES.get(p.name, [])]:
            idx[normalise(name)] = p
    return idx


@dataclass(frozen=True)
class Match:
    product: Product | None
    score: float  # 100 = exact
    text: str  # the cleaned text that was matched


def _candidates(text: str) -> list[str]:
    """The cell itself, then each part of 'A (B)' and 'A / B' style names."""
    out = [text]
    m = re.match(r"^(.*?)\((.*?)\)(.*)$", text)
    if m:
        out += [m.group(1) + m.group(3), m.group(2)]
    return [c for c in out if c.strip()]


def _contained(n: str, names: list[str]) -> str | None:
    """The longest known name that appears in ``n`` as whole words."""
    for name in names:
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", n):
            return name
    return None


@lru_cache(maxsize=1)
def _names_longest_first() -> list[str]:
    return sorted(_index(), key=len, reverse=True)


def match_product(text: str, drug_class: str | None = None) -> Match:
    """The product a PDL cell names, or ``Match(None, ...)`` when nothing is close enough.

    1. Exact name, or either half of "A (B)"; a brand wins over a generic, so
       "semaglutide (Ozempic)" is Ozempic.
    2. The longest known name inside the cell ("Ozempic pen injector").
    3. Fuzzy, for OCR misreads: within the row's class when it is known (score 85+), otherwise
       across the dictionary (92+).
    """
    idx = _index()
    found: list[Product] = []
    for cand in _candidates(text):
        n = normalise(cand)
        if n in idx:
            found.append(idx[n])
    if found:
        brands = [p for p in found if p.kind == "brand"]
        return Match((brands or found)[0], 100.0, normalise(text))
    n = normalise(text)
    if not n:
        return Match(None, 0.0, n)
    inside = _contained(n, _names_longest_first())
    if inside:
        return Match(idx[inside], 95.0, n)
    for cand in _candidates(text):  # an OCR misread: "Otezia", "Mavenciad", "Jormay PM"
        folded = _folded_index().get(fold(normalise(cand)))
        if folded is not None:
            return Match(folded, 96.0, n)
    best = 0.0
    # within the row's class a looser match is safe; then across the whole dictionary
    for cls, cutoff in ((drug_class, 80.0), (None, 92.0)):
        if cls is None and drug_class is not None or cls is not None:
            pool = {k: p for k, p in idx.items() if cls is None or p.drug_class == cls}
            hit = process.extractOne(n, list(pool), scorer=fuzz.ratio)
            if hit and hit[1] >= cutoff:
                return Match(pool[hit[0]], float(hit[1]), n)
            best = max(best, float(hit[1]) if hit else 0.0)
    if drug_class is None:
        hit = process.extractOne(n, list(idx), scorer=fuzz.ratio)
        if hit and hit[1] >= 92:
            return Match(idx[hit[0]], float(hit[1]), n)
        best = max(best, float(hit[1]) if hit else 0.0)
    return Match(None, best, n)


_FOLD = str.maketrans("l1!|jm0q", "iiiiinog")


def fold(text: str) -> str:
    """Collapse letters OCR confuses (l/i/1/j, m/n/rn, 0/o, q/g, vv/w) so misreads compare equal."""
    return text.translate(_FOLD).replace("rn", "n").replace("vv", "w")


@lru_cache(maxsize=1)
def _folded_index() -> dict[str, Product]:
    out: dict[str, Product] = {}
    for name, p in _index().items():
        out.setdefault(fold(name), p)
    return out


def find_products(text: str) -> list[Product]:
    """Every product named in a piece of prose, in order, without overlaps.

    Parenthesised ingredient names after a brand ("Victoza (liraglutide)") are dropped first, so
    the generic isn't counted as a second product.
    """
    t = re.sub(r"\(([^()A-Z]*)\)", " ", text)  # "(liraglutide)", but not "(Ozempic)"
    n = normalise(t)
    idx = _index()
    out: list[tuple[int, Product]] = []
    taken: list[tuple[int, int]] = []
    for name in _names_longest_first():
        for m in re.finditer(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", n):
            s, e = m.span()
            if any(s < b and a < e for a, b in taken):
                continue
            taken.append((s, e))
            out.append((s, idx[name]))
    seen: set[str] = set()
    result = []
    for _, p in sorted(out, key=lambda x: x[0]):
        if p.name not in seen:
            seen.add(p.name)
            result.append(p)
    return result


@lru_cache(maxsize=1)
def _class_index() -> dict[str, str]:
    idx = {normalise(c): c for c in CLASSES}
    extra = {
        "glp-1 agonists": "GLP-1 Receptor Agonists",
        "glp-1 receptor agonist": "GLP-1 Receptor Agonists",
        "incretin mimetics": "GLP-1 Receptor Agonists",
        "weight loss agents": "Anti-Obesity Agents",
        "sglt2 inhibitors": "SGLT2 Inhibitors",
        "sodium-glucose co-transporter 2 inhibitors": "SGLT2 Inhibitors",
        "dipeptidyl peptidase-4 inhibitors": "DPP-4 Inhibitors",
        "long-acting insulins": "Insulins, Long-Acting",
        "insulins long acting": "Insulins, Long-Acting",
        "rapid-acting insulins": "Insulins, Rapid-Acting",
        "anticoagulants": "Anticoagulants, Oral",
        "oral anticoagulants": "Anticoagulants, Oral",
        "hepatitis c": "Hepatitis C Agents",
        "hepatitis c direct-acting antivirals": "Hepatitis C Agents",
        "cgrp antagonists": "Antimigraine Agents, CGRP",
        "migraine cgrp agents": "Antimigraine Agents, CGRP",
        "cytokine and cam antagonists": "Cytokine and CAM Antagonists",
        "targeted immunomodulators": "Cytokine and CAM Antagonists",
        "atypical antipsychotics": "Antipsychotics, Atypical",
        "antipsychotics": "Antipsychotics, Atypical",
        "stimulants and related agents": "ADHD Stimulants",
        "adhd agents": "ADHD Stimulants",
        "ics/laba combinations": "Inhaled Corticosteroid/LABA Combinations",
        "glucocorticoid/beta agonist combinations": "Inhaled Corticosteroid/LABA Combinations",
        "pcsk9 inhibitors and other lipotropics": "Lipotropics, Other",
        "hiv/aids": "HIV Antiretrovirals",
        "antiretrovirals": "HIV Antiretrovirals",
        "opioid dependence treatments": "Opioid Use Disorder Agents",
        "substance use disorder agents": "Opioid Use Disorder Agents",
        "ppis": "Proton Pump Inhibitors",
        "multiple sclerosis": "Multiple Sclerosis Agents",
        "ms agents": "Multiple Sclerosis Agents",
        "asthma biologics": "Immunomodulators, Asthma",
        "smoking deterrents": "Smoking Cessation Agents",
        "tobacco cessation": "Smoking Cessation Agents",
        # the short names people use in questions
        "glp-1": "GLP-1 Receptor Agonists",
        "glp1": "GLP-1 Receptor Agonists",
        "glp-1s": "GLP-1 Receptor Agonists",
        "sglt2": "SGLT2 Inhibitors",
        "sglt-2": "SGLT2 Inhibitors",
        "sglt2s": "SGLT2 Inhibitors",
        "dpp-4": "DPP-4 Inhibitors",
        "dpp4": "DPP-4 Inhibitors",
        "long-acting insulin": "Insulins, Long-Acting",
        "basal insulin": "Insulins, Long-Acting",
        "rapid-acting insulin": "Insulins, Rapid-Acting",
        "mealtime insulin": "Insulins, Rapid-Acting",
        "doacs": "Anticoagulants, Oral",
        "blood thinners": "Anticoagulants, Oral",
        "hep c": "Hepatitis C Agents",
        "hcv": "Hepatitis C Agents",
        "cgrp": "Antimigraine Agents, CGRP",
        "migraine": "Antimigraine Agents, CGRP",
        "tnf inhibitors": "Cytokine and CAM Antagonists",
        "adalimumab biosimilars": "Cytokine and CAM Antagonists",
        "adhd": "ADHD Stimulants",
        "stimulants": "ADHD Stimulants",
        "ics/laba": "Inhaled Corticosteroid/LABA Combinations",
        "pcsk9": "Lipotropics, Other",
        "pcsk9 inhibitors": "Lipotropics, Other",
        "hiv": "HIV Antiretrovirals",
        "oud": "Opioid Use Disorder Agents",
        "ppi": "Proton Pump Inhibitors",
        "multiple sclerosis agents": "Multiple Sclerosis Agents",
        "anti-obesity": "Anti-Obesity Agents",
        "obesity": "Anti-Obesity Agents",
        "weight loss": "Anti-Obesity Agents",
        "smoking cessation": "Smoking Cessation Agents",
    }
    idx.update({normalise(k): v for k, v in extra.items()})
    return idx


def match_class(text: str, cutoff: float = 85) -> str | None:
    """The canonical therapeutic class a heading names, if any."""
    n = normalise(re.sub(r"\(\s*cont(inued|'d)?\.?\s*\)", "", text, flags=re.I))
    if not n or len(n) < 3:
        return None
    idx = _class_index()
    if n in idx:
        return idx[n]
    hit = process.extractOne(n, list(idx), scorer=fuzz.ratio)
    return idx[hit[0]] if hit and hit[1] >= cutoff else None

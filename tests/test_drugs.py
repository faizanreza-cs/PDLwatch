import pytest

from pdlwatch.drugs import (
    BY_NAME,
    CLASSES,
    PRODUCTS,
    find_products,
    match_class,
    match_product,
)


@pytest.mark.parametrize(
    ("text", "product"),
    [
        ("Ozempic", "Ozempic"),
        ("OZEMPIC® (semaglutide) pen 2 mg/3 mL", "Ozempic"),  # mark, case, strength, form
        ("semaglutide (Ozempic)", "Ozempic"),  # a brand in the cell wins
        ("Victoza (liraglutide)", "Victoza"),
        ("liraglutide", "liraglutide"),  # the generic is its own product
        ("Nurtec ODT†", "Nurtec ODT"),
        ("Advair Diskus 250/50", "Advair Diskus"),
        ("Bydureon BCISE", "Bydureon BCise"),
        ("insulin glargine-yfgn", "insulin glargine-yfgn"),
        ("Ozempic pen injector", "Ozempic"),  # a known name inside the cell
        ("Otezia (apremilast)", "Otezla"),  # OCR: l read as i
        ("Jormay PM (methylphenidate)", "Jornay PM"),  # OCR: n read as m
        ("Rinvog", "Rinvoq"),  # OCR: q read as g
    ],
)
def test_names_resolve_to_products(text, product):
    m = match_product(text)
    assert m.product is not None and m.product.name == product


def test_unknown_names_stay_unknown():
    assert match_product("Approval requires a diagnosis").product is None
    assert match_product("").product is None


@pytest.mark.parametrize(
    ("text", "product"),
    [
        ("omeprazole DR", "omeprazole"),  # a release type
        ("aripiprazole ODT", "aripiprazole"),
        ("pantoprazole sodium DR", "pantoprazole"),  # a salt, then the release type
        ("quetiapine fumarate ER", "quetiapine"),
        ("lurasidone HCl", "lurasidone"),  # a salt written short
        ("naloxone/buprenorphine", "buprenorphine/naloxone"),  # not buprenorphine alone
        ("velpatasvir/sofosbuvir", "sofosbuvir/velpatasvir"),
        ("Nurtec ODT", "Nurtec ODT"),  # a name that carries the word is still itself
        ("bupropion SR", "bupropion SR"),
    ],
)
def test_salts_forms_and_combination_order_around_a_known_name(text, product):
    m = match_product(text)
    assert (m.product.name, m.score) == (product, 100.0)


def test_other_words_beside_a_known_name_still_go_to_review():
    # "MR" is a code in New York's list (the drug is also in another program), not a release type
    for text in ("omeprazole OTC", "Humalog Mix 75/25", "Trulicity Plus", "omeprazole MR"):
        m = match_product(text)
        assert m.product is not None and m.score < 100, text


def test_dictionary_is_consistent():
    assert len(BY_NAME) == len(PRODUCTS)  # names are unique
    assert all(p.ingredient and p.drug_class in CLASSES for p in PRODUCTS)
    assert all(p.kind == ("generic" if p.name[0].islower() else "brand") for p in PRODUCTS)


def test_prose_mentions_skip_the_ingredient_after_a_brand():
    found = [
        p.name
        for p in find_products(
            "Preferred agents: Victoza (liraglutide) and liraglutide; Mounjaro requires PA."
        )
    ]
    assert found == ["Victoza", "liraglutide", "Mounjaro"]


@pytest.mark.parametrize(
    ("text", "cls"),
    [
        ("GLP-1 RECEPTOR AGONISTS", "GLP-1 Receptor Agonists"),
        ("GLP-1s", "GLP-1 Receptor Agonists"),
        ("Cytokine & CAM Antagonists (continued)", "Cytokine and CAM Antagonists"),
        ("DOACs", "Anticoagulants, Oral"),
        ("Hep C", "Hepatitis C Agents"),
    ],
)
def test_class_names_and_aliases(text, cls):
    assert match_class(text) == cls


def test_a_sentence_is_not_a_class():
    assert match_class("Prescribers should submit requests") is None


def test_a_product_named_for_reference_is_not_the_match():
    from pdlwatch.drugs import match_product

    assert match_product("Insulin Degludec (compare to Tresiba)").product.name == "insulin degludec"
    assert match_product("sitagliptin (gen Januvia ® )").product is None  # not Januvia
    m = match_product("Adalimumab-adbm (compare to Cyltezo®) biosimilar to Humira®")
    assert m.product.name == "adalimumab-adbm"
    assert match_product("Tresiba (insulin degludec)").product.name == "Tresiba"

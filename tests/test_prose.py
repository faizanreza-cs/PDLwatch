from pdlwatch.extract import prose
from pdlwatch.pdf import Line, Page, Phrase, Word


def _page(*sentences: str) -> Page:
    lines = []
    for i, s in enumerate(sentences):
        lines.append(Line([Phrase([Word(s, 50, 550, 100 + 15 * i, 109 + 15 * i)])]))
    return Page(1, 612, 792, lines, False)


def _read(*sentences: str) -> dict[str, tuple]:
    return {r.product.name: (r.status, r.pa, r.st, r.ql)
            for r in prose.extract([_page(*sentences)])}  # fmt: skip


def test_plain_statements():
    got = _read(
        "Preferred agents: Ozempic and Trulicity.",
        "Non-preferred agents (Mounjaro, Rybelsus) require prior authorization.",
    )
    assert got["Ozempic"][:2] == ("preferred", False)
    assert got["Mounjaro"][:2] == ("non_preferred", True)  # not "preferred ... requires PA"


def test_preferred_with_prior_authorization():
    got = _read(
        "Preferred agents: Wegovy.",
        "Wegovy is preferred but requires prior authorization; clinical criteria apply.",
    )
    assert got["Wegovy"][:2] == ("preferred", True)


def test_step_therapy_belongs_to_the_drug_being_approved():
    got = _read(
        "Preferred agents: Humira.",
        "Prior authorization is required for Cosentyx.",
        "Before Cosentyx is approved, the member must have tried Humira or another "
        "preferred agent.",
    )
    assert got["Cosentyx"] == ("non_preferred", True, True, None)
    assert got["Humira"][2] is None  # named as the drug to try first, not under step therapy


def test_quantity_limits_and_exclusions():
    got = _read(
        "Quantity limits apply to Aimovig: 1 per 30 days.", "Wegovy and Zepbound are not covered."
    )
    assert got["Aimovig"][3] is True
    assert got["Wegovy"][0] == "not_covered"


def test_a_drug_named_without_a_status_has_none():
    got = _read("Members stable on Eliquis may continue therapy for 90 days.")
    assert got["Eliquis"][0] is None

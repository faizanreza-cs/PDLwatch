from pdlwatch.extract.legend import parse


def test_code_legends():
    leg = parse(
        "P = Preferred; NP = Non-Preferred (prior authorization required); NC = Not covered."
    )
    assert leg.codes == {"p": "preferred", "np": "non_preferred", "nc": "not_covered"}
    assert leg.status("NP") == "non_preferred" and leg.status("Not Covered") == "not_covered"


def test_marker_legends():
    leg = parse(
        "* = Prior authorization required (clinical criteria). † = Quantity limit applies. "
        "‡ = Step therapy applies. BvG = Brand preferred over generic."
    )
    assert leg.markers() == {"bvg": "bvg", "*": "pa", "†": "ql", "‡": "st"}


def test_tier_legends():
    leg = parse(
        "Tier 1 = Preferred generic drugs. Tier 2 = Preferred brand drugs. Tier 3 = "
        "Non-preferred drugs (prior authorization required). NF = Not on formulary."
    )
    assert leg.tiers == {1: "preferred", 2: "preferred", 3: "non_preferred"}
    assert leg.tier("T3") == (3, "non_preferred")
    assert leg.tier("Tier 2") == (2, "preferred")
    assert leg.tier("NF") == (None, "not_covered")


def test_a_code_lost_to_ocr_is_noted():
    leg = parse(
        "* = Prior authorization required. t = Quantity limit applies. = Step therapy applies."
    )
    assert leg.codes["t"] == "ql" and leg.lost == {"st"}


def test_defaults_without_a_legend():
    leg = parse("")
    assert leg.status("Preferred") == "preferred"
    assert leg.status("Non-PDL") == "non_preferred"
    assert leg.tier("2") == (2, "preferred") and leg.tier("3") == (3, "non_preferred")


def test_one_symbol_read_for_two_markers_marks_both_lost():
    leg = parse(
        "* = Prior authorization required. + = Quantity limit applies. + = Step therapy applies."
    )  # a scan read † and ‡ both as "+"
    assert leg.codes == {"*": "pa"} and leg.lost == {"ql", "st"}


def test_footnotes_at_the_foot_of_a_page():
    """Oregon defines its symbols in footnotes on every page, beside a note in another column."""
    from pdlwatch.extract.document import legend_text
    from pdlwatch.pdf import Line, Page, Phrase, Word

    def line(top: float, *cells: tuple[str, float]) -> Line:
        def words(text: str, x: float) -> list[Word]:
            return [Word(w, x + 30 * i, x + 30 * i + 25, top, top + 8)
                    for i, w in enumerate(text.split())]  # fmt: skip

        return Line([Phrase(words(text, x)) for text, x in cells])

    page = Page(3, 612, 792, [
        line(86, ("System", 43), ("Class", 124), ("Preferred", 380)),
        line(705, ("* Drug coverage subject to meeting clinical prior authorization criteria", 43),
             ("Note: New drugs are considered non-preferred until reviewed", 360)),
        line(715, ("** Drug coverage subject to quantity limits", 43)),
    ], False)  # fmt: skip
    leg = parse(legend_text([page]))
    assert leg.markers() == {"**": "ql", "*": "pa"}


def test_a_preferred_drug_list_without_a_status_column_lists_preferred_drugs():
    from pdlwatch.extract.document import _default_status
    from pdlwatch.extract.layout import RawRow

    def raw(*roles: str) -> RawRow:
        return RawRow(1, "table", {"drug": "x"}, None, None, False, 100.0, "x", frozenset(roles))

    head = "Florida Medicaid Preferred Drug List Effective October 1, 2026"
    assert _default_status(head, [raw("drug", "class", "pa")]) == "preferred"
    assert _default_status(head, [raw("drug", "pa"), raw("drug", "status")]) is None
    assert _default_status("Prior Authorization Drug List", [raw("drug", "pa")]) is None


def test_a_symbol_explained_as_something_else_is_known():
    from pdlwatch.extract.rows import undefined_symbols

    leg = parse("* = Prior authorization required. ‡ = Age restrictions apply.")
    assert leg.notes == {"‡": "Age restrictions apply"} and leg.markers() == {"*": "pa"}
    assert undefined_symbols("risperidone ‡ TABLET", leg) == set()
    assert undefined_symbols("risperidone † TABLET", leg) == {"†"}

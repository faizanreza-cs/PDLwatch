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

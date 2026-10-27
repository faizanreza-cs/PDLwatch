from pdlwatch.extract.layout import RawRow
from pdlwatch.extract.legend import parse
from pdlwatch.extract.rows import from_raw, score, strip_markers

MARKERS = parse(
    "CC = Prior authorization required (clinical criteria). QL = Quantity limit "
    "applies. * = Prior authorization. † = Quantity limit. BvG = Brand preferred over "
    "generic."
)


def _raw(drug: str, role: str | None = "preferred", ocr: bool = False, **cells: str) -> RawRow:
    roles = frozenset({"preferred", "non_preferred"}) if role else frozenset(cells) | {"drug"}
    return RawRow(
        1, "list" if role else "table", {"drug": drug, **cells}, None, role, ocr, 100.0, drug, roles
    )


def test_markers_come_off_the_name_and_become_flags():
    assert strip_markers("Ozempic CC QL", MARKERS) == ("Ozempic", {"pa", "ql"})
    assert strip_markers("Trulicity*†", MARKERS) == ("Trulicity", {"pa", "ql"})
    assert strip_markers("Januvia BvG", MARKERS) == ("Januvia", {"bvg"})


def test_codes_printed_as_superscripts_come_off_the_word_they_touch():
    # Pennsylvania prints PA and QL as superscripts, which the text layer glues to the word
    assert strip_markers("Duavee TabletQL", MARKERS) == ("Duavee Tablet", {"ql"})
    assert strip_markers("AlphanatePA", MARKERS) == ("Alphanate", {"pa"})
    assert strip_markers("Zavesca CapsulePA QL", MARKERS) == ("Zavesca Capsule", {"pa", "ql"})
    assert strip_markers("Duavee TabletQL", MARKERS, glued=False)[1] == set()  # a scan: OCR checks
    for name in ("NovoLog", "ZyPREXA", "ClonazePAM", "Basaglar KwikPen", "Toprol XL", "PA"):
        assert strip_markers(name, MARKERS) == ((name, set()) if name != "PA" else ("", {"pa"}))


def test_non_preferred_means_prior_authorization():
    r = from_raw(_raw("Mounjaro", "non_preferred"), MARKERS, "d", "OH")
    assert (r.status, r.pa, r.ql) == ("non_preferred", True, False)


def test_a_pa_column_that_says_no_stands_unless_the_row_was_scanned():
    # Washington lists some non-preferred drugs with "N" in its own PA column
    said = from_raw(_raw("Mounjaro", None, status="N", pa="N"), MARKERS, "d", "WA")
    assert (said.status, said.pa) == ("non_preferred", False)
    scanned = from_raw(_raw("Mounjaro", None, ocr=True, status="N", pa="N"), MARKERS, "d", "WA")
    assert scanned.pa is True  # a scan can misread the cell: the Medicaid rule stands
    assert from_raw(_raw("Mounjaro", None, status="NP"), MARKERS, "d", "WA").pa is True


def test_an_unexpressed_field_is_unknown_not_false():
    plain = parse("")  # no markers: the list can't say anything about QL
    r = from_raw(_raw("Ozempic"), plain, "d", "OH")
    assert r.ql is None and r.pa is None


def test_requirements_cells():
    r = from_raw(_raw("Xarelto", None, tier="T3", req="PA; QL (30 per 30 days)"),
                 parse("Tier 3 = Non-preferred"), "d", "TX")  # fmt: skip
    assert (r.status, r.tier, r.pa, r.ql, r.ql_detail) == (
        "non_preferred",
        3,
        True,
        True,
        "30 per 30 days",
    )


def test_quantity_limits_as_utah_writes_them():
    plain = parse("")
    for limit, detail in [
        ("30 Units / 15 Days with Max 1 Fill", "30 Units / 15 Days"),
        ("1 tab per 30 days", "1 tab per 30 days"),
        ("Cumulative: 120 units /30 days", "120 units /30 days"),
        ("2 inhaler/30 days", "2 inhaler/30 days"),
        ("100u/ml: 30ml/30days", "30ml/30days"),
        ("90 MME & 4 tablets /day", "4 tablets /day"),
        ("24 mg per day", "24 mg per day"),
    ]:
        r = from_raw(_raw("Novolog", None, status="Preferred", req=limit), plain, "d", "UT")
        assert (r.ql, r.ql_detail) == (True, detail), limit
    for other in ("Minimum Age: 4 Years Old", "09/01/25", "Female only"):  # an age, a date
        r = from_raw(_raw("Novolog", None, status="Preferred", req=other), plain, "d", "UT")
        assert (r.ql, r.ql_detail) == (False, None), other


def test_ocr_daggers_are_decoded_but_sent_to_review():
    leg = parse("* = Prior authorization. † = Quantity limit applies.")
    r = score([from_raw(_raw("Zituviot", ocr=True), leg, "d", "AL")])[0]
    assert (r.product, r.ql) == ("Zituvio", True)
    assert not r.accepted and "footnote symbol read by OCR" in r.review_reasons


def test_a_tier_outside_the_legend_goes_to_review():
    leg = parse("Tier 1 = Preferred generic. Tier 2 = Preferred brand. Tier 3 = Non-preferred.")
    r = score([from_raw(_raw("sofosbuvir/velpatasvir", None, tier="4"), leg, "d", "MT")])[0]
    assert not r.accepted and "tier 4 is not in the legend" in r.review_reasons


def test_conflicting_rows_go_to_review_and_unknown_drugs_too():
    leg = parse("")
    rows = score([from_raw(_raw("Ozempic", "preferred"), leg, "d", "OH"),
                  from_raw(_raw("Ozempic", "non_preferred"), leg, "d", "OH"),
                  from_raw(_raw("Notadrug Plus"), leg, "d", "OH")])  # fmt: skip
    assert [r.accepted for r in rows] == [False, False, False]
    assert "drug name not in the dictionary" in rows[2].review_reasons


def test_unreadable_scan_cells_go_to_review_not_to_a_guess():
    codes = parse("P = Preferred. NP = Non-preferred.")
    r = score([from_raw(_raw("Ozempic", None, ocr=True, status="P", pa="Ne", ql="_No"),
                        codes, "d", "WI")])[0]  # fmt: skip
    assert r.pa is None and r.ql is False and "PA cell unreadable" in r.review_reasons
    assert not r.accepted
    r = score([from_raw(_raw("Latuda", None, ocr=True, status="NF"), codes, "d", "WI")])[0]
    assert "status code not in the legend" in r.review_reasons and not r.accepted
    raw = _raw("Mydayis", None, ocr=True, status="NP")
    raw.reread = True  # the status cell was empty and re-OCR'd on its own
    r = score([from_raw(raw, codes, "d", "WI")])[0]
    assert r.status == "non_preferred" and not r.accepted


def test_flags_under_an_unrecognised_header_go_to_review():
    r = score([from_raw(_raw("Wegovy", None, tier="Tier 3", other="PA; ST"),
                        parse("Tier 3 = Non-preferred"), "d", "DE")])[0]  # fmt: skip
    assert (r.pa, r.st) == (True, True) and not r.accepted


def test_garbled_text_beside_a_scanned_name_goes_to_review():
    r = score([from_raw(_raw("Nicotrol inhaler (nicotine) aL", ocr=True), MARKERS, "d", "WV")])[0]
    assert r.product == "Nicotrol inhaler" and not r.accepted


def test_a_known_name_inside_a_longer_one_goes_to_review():
    r = score([from_raw(_raw("Humalog Mix 75/25"), MARKERS, "d", "OH")])[0]
    assert r.product == "Humalog" and not r.accepted  # another product, maybe
    assert "words beside the name the dictionary doesn't know" in r.review_reasons


def test_footnote_symbols_anywhere_in_the_name():
    """Oregon: "codeine sulfate * TABLET", with the meanings at the foot of the page."""
    leg = parse("* = Drug coverage subject to meeting clinical prior authorization criteria. "
                "** = Drug coverage subject to quantity limits.")  # fmt: skip
    assert strip_markers("codeine sulfate * TABLET", leg) == ("codeine sulfate TABLET", {"pa"})
    assert strip_markers("sumatriptan ** SPRAY", leg) == ("sumatriptan SPRAY", {"ql"})
    assert strip_markers("Ozempic**", leg) == ("Ozempic", {"ql"})
    assert strip_markers("Ozempic***", leg) == ("Ozempic", set())  # undefined: not read as "*"


def test_a_preferred_with_pa_column_and_a_default_status():
    r = from_raw(_raw("Butrans", "preferred_pa"), parse(""), "d", "KY")
    assert (r.status, r.pa) == ("preferred", True)
    table = RawRow(1, "table", {"drug": "Xarelto", "pa": "No"}, None, None, False, 100.0,
                   "Xarelto | No", frozenset({"drug", "pa"}))  # fmt: skip
    assert from_raw(table, parse(""), "d", "FL").status is None
    r = from_raw(table, parse(""), "d", "FL", default_status="preferred")
    assert (r.status, r.pa) == ("preferred", False) and "only preferred" in r.evidence


def test_a_footnote_symbol_the_legend_does_not_define_goes_to_review():
    leg = parse("* = Prior authorization required.")
    r = score([from_raw(_raw("Xarelto ‡", "preferred"), leg, "d", "OR")])[0]
    assert r.product == "Xarelto" and not r.accepted
    assert "footnote symbol not in the legend" in r.review_reasons
    ok = score([from_raw(_raw("Xarelto *", "preferred"), leg, "d", "OR")])[0]
    assert ok.accepted and ok.pa


def test_preferred_with_pa_written_in_the_status_cell():
    raw = RawRow(1, "table", {"drug": "Strattera", "status": "PREFERRED_WITH_PA"}, None, None,
                 False, 100.0, "x", frozenset({"drug", "status"}))  # fmt: skip
    r = from_raw(raw, parse(""), "d", "IL")
    assert (r.status, r.pa) == ("preferred", True)

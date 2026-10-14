from pdlwatch.extract import layout
from pdlwatch.pdf import Line, Page, Phrase, Word


def _line(top: float, *cells: tuple[str, float], bold: bool = False) -> Line:
    phrases = []
    for text, x in cells:
        words, cx = [], x
        for w in text.split():
            words.append(Word(w, cx, cx + 5 * len(w), top, top + 9, bold))
            cx += 5 * len(w) + 2.5
        phrases.append(Phrase(words))
    return Line(phrases)


def _page(*lines: Line) -> Page:
    return Page(1, 612, 792, list(lines), False)


def test_record_table_with_wrapped_cells():
    page = _page(
        _line(100, ("Drug Name", 50), ("Status", 250), ("PA", 330), ("QL", 380)),
        _line(115, ("Synjardy", 50), ("NP", 250), ("Y", 330)),
        _line(125, ("(empagliflozin/metformin)", 50)),  # the drug cell wraps
        _line(140, ("Jardiance", 50), ("P", 250), ("4 per 28", 380)),
        _line(150, ("days", 380)),  # the QL cell wraps
    )
    rows, pages = layout.parse([page])
    assert pages == {1}
    assert [r.cells["drug"] for r in rows] == ["Synjardy (empagliflozin/metformin)", "Jardiance"]
    assert rows[1].cells["ql"] == "4 per 28 days"


def test_two_line_header_and_mid_word_split():
    page = _page(
        _line(100, ("Drug", 50), ("Status", 250), ("PA", 330), ("Step T", 400)),
        _line(108, ("Required", 330), ("herapy", 400)),
        _line(125, ("Ozempic", 50), ("NP", 250), ("Y", 330), ("Y", 400)),
    )
    rows, _ = layout.parse([page])
    assert rows[0].cells == {"drug": "Ozempic", "status": "NP", "pa": "Y", "st": "Y"}


def test_unknown_header_columns_keep_their_text_out_of_others():
    page = _page(
        _line(100, ("Drug Name", 50), ("Brand/Generic", 200), ("Status", 300)),
        _line(115, ("Januvia", 50), ("B", 200), ("P", 300)),
    )
    rows, _ = layout.parse([page])
    assert rows[0].cells["status"] == "P" and rows[0].cells["other"] == "B"


def test_a_data_row_is_not_a_header():
    assert layout.header(_line(100, ("Ozempic", 50), ("Preferred", 250), ("PA", 330)), 612) is None


def test_lists_with_wrapped_names_markers_and_class_bars():
    page = _page(
        _line(90, ("HIV ANTIRETROVIRALS", 50), bold=True),
        _line(100, ("PREFERRED AGENTS", 50), ("NON-PREFERRED AGENTS", 240)),
        _line(115, ("Biktarvy", 50), ("Genvoya (elvitegravir/cobicistat/", 240)),
        _line(125, ("emtricitabine/tenofovir alafenamide)", 240)),
        _line(140, ("Dovato", 50), ("Triumeq", 240)),
        _line(150, ("QL", 240)),  # a marker that slipped onto its own line
    )
    rows, _ = layout.parse([page])
    got = [(r.list_role, r.cells["drug"]) for r in rows]
    assert got == [
        ("preferred", "Biktarvy"),
        ("non_preferred", "Genvoya (elvitegravir/cobicistat/emtricitabine/tenofovir alafenamide)"),
        ("preferred", "Dovato"),
        ("non_preferred", "Triumeq QL"),
    ]
    assert rows[0].drug_class == "HIV ANTIRETROVIRALS"


def test_a_header_that_lost_a_phrase_borrows_it_from_the_last_one():
    page = _page(
        _line(100, ("PREFERRED", 50), ("NON-PREFERRED", 240), ("PA CRITERIA", 430)),
        _line(115, ("Biktarvy", 50), ("Genvoya", 240)),
        _line(200, ("NON-PREFERRED", 240), ("PA CRITERIA", 430)),  # OCR lost "PREFERRED"
        _line(215, ("Dovato", 50), ("Triumeq", 240)),
    )
    rows, _ = layout.parse([page])
    assert [(r.list_role, r.cells["drug"]) for r in rows][2:] == [
        ("preferred", "Dovato"),
        ("non_preferred", "Triumeq"),
    ]


def test_a_drug_above_the_next_header_is_not_a_class_heading():
    # upper-case names, and OCR lost the class bar between two classes
    page = _page(
        _line(100, ("PREFERRED", 50), ("NON-PREFERRED", 240)),
        _line(115, ("CLOZAPINE", 50), ("LATUDA", 240)),
        _line(130, ("COBENFY", 50)),
        _line(150, ("PREFERRED", 50), ("NON-PREFERRED", 240)),
        _line(165, ("XARELTO", 50), ("ELIQUIS", 240)),
    )
    rows, _ = layout.parse([page])
    assert "COBENFY" in [r.cells["drug"] for r in rows]


def test_a_data_row_with_an_unknown_drug_is_not_a_header():
    # real PDLs list thousands of drugs the dictionary doesn't know
    row = _line(100, ("metformin", 50), ("Preferred", 250), ("QL", 330))
    assert layout.header(row, 612) is None
    head = _line(100, ("Drug Name", 50), ("Brand/Generic", 200), ("Status", 300))
    assert layout.header(head, 612) is not None


def test_generics_on_their_own_lines_are_separate_items():
    page = _page(
        _line(100, ("PREFERRED AGENTS", 50), ("NON-PREFERRED AGENTS", 240)),
        _line(115, ("Ozempic", 50), ("Mounjaro", 240)),
        _line(130, ("metformin ER", 50), ("insulin glargine-", 240)),
        _line(140, ("pioglitazone", 50), ("yfgn", 240)),
        _line(155, ("Nicotrol", 50)),
        _line(165, ("inhaler", 50)),
    )
    rows, _ = layout.parse([page])
    got = [(r.list_role, r.cells["drug"]) for r in rows]
    assert got == [("preferred", "Ozempic"), ("non_preferred", "Mounjaro"),
                   ("preferred", "metformin ER"), ("non_preferred", "insulin glargine-yfgn"),
                   ("preferred", "pioglitazone"), ("preferred", "Nicotrol inhaler")]  # fmt: skip

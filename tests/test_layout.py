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


def test_centred_list_headers_take_the_columns_their_items_start_in():
    """Indiana: headers centred over wide columns, bullets and names ~80 pt left of them."""
    lines = [_line(100, ("DRUG CLASS", 92), ("PREFERRED", 283), ("NON-PREFERRED", 499),
                   ("PA CRITERIA", 677))]  # fmt: skip
    pairs = [("Eliquis", "Savaysa"), ("warfarin", "Bevyxxa"), ("Xarelto", "dabigatran"),
             ("Pradaxa", None)]  # fmt: skip
    for k, (pref, nonpref) in enumerate(pairs):
        cells = [("ANTICOAGULANTS", 30)] if k == 0 else []
        cells += [("•", 201), (pref, 219)]
        if nonpref:
            cells += [("•", 427), (nonpref, 445)]
        lines.append(_line(115 + 12 * k, *cells))
    rows, _ = layout.parse([_page(*lines)])
    assert {(r.cells["drug"], r.list_role) for r in rows} == {
        ("Eliquis", "preferred"), ("warfarin", "preferred"), ("Xarelto", "preferred"),
        ("Pradaxa", "preferred"), ("Savaysa", "non_preferred"), ("Bevyxxa", "non_preferred"),
        ("dabigatran", "non_preferred"),
    }  # fmt: skip


def test_markers_between_columns_do_not_move_them():
    lines = [_line(100, ("PREFERRED", 50), ("NON-PREFERRED", 300))]
    pairs = [("Byetta", "Victoza"), ("Ozempic", "Trulicity"), ("Mounjaro", "Rybelsus"),
             ("liraglutide", "Bydureon BCise")]  # fmt: skip
    for k, (a, b) in enumerate(pairs):
        lines.append(_line(115 + 12 * k, (a, 50), ("QL", 200), (b, 300)))
    rows, _ = layout.parse([_page(*lines)])
    assert [r.list_role for r in rows if r.cells["drug"] in {b for _, b in pairs}] == [
        "non_preferred"] * 4  # fmt: skip
    assert rows[0].cells["drug"] == "Byetta QL"


def test_a_bullet_starts_a_new_item_and_is_not_part_of_the_name():
    page = _page(
        _line(100, ("PREFERRED AGENTS", 50), ("NON-PREFERRED AGENTS", 300)),
        _line(115, ("•", 50), ("Odefsey (emtricitabine/", 60)),
        _line(127, ("rilpivirine/tenofovir AF)", 60)),
        _line(139, ("•", 50), ("Genvoya (elvitegravir/", 60)),
        _line(151, ("•", 50), ("Biktarvy", 60)),
    )
    rows, _ = layout.parse([page])
    assert [r.cells["drug"] for r in rows] == [
        "Odefsey (emtricitabine/rilpivirine/tenofovir AF)", "Genvoya (elvitegravir/", "Biktarvy"]


def test_criteria_and_notes_in_list_columns_are_not_drugs():
    page = _page(
        _line(100, ("PREFERRED AGENTS", 50), ("NON-PREFERRED AGENTS", 300)),
        _line(115, ("program.", 50), ("Xarelto or Eliquis cannot be used.", 300)),
        _line(127, ("(No PA required unless otherwise noted", 50)),
        _line(139, ("Nicotine Gum", 50), ("or provide medical justification for use", 300)),
        _line(151, ("Varenicline (Limited to 18 years and older)", 50)),
        _line(163, ("QTY LIMIT: 2 tablets/day", 50)),
        _line(175, ("Nicotine Lozenge", 50)),
    )
    rows, _ = layout.parse([page])
    assert [r.cells["drug"] for r in rows] == [
        "Nicotine Gum", "Varenicline (Limited to 18 years and older)", "Nicotine Lozenge"]
    assert rows[1].cells["ql"] == "QTY LIMIT: 2 tablets/day"


def test_a_quantity_limit_note_that_wraps_stays_with_its_drug():
    page = _page(
        _line(100, ("Preferred Agents", 78), ("Non-Preferred Agents", 352)),
        _line(115, ("Aimovig", 78), ("Qulipta (atogepant)", 352)),
        _line(127, ("QTY Limit: 240 mg (2 injections) for the first 30", 78)),
        _line(139, ("days, then 120 mg monthly", 78)),
        _line(151, ("Emgality", 78)),
    )
    rows, _ = layout.parse([page])
    assert [r.cells["drug"] for r in rows] == ["Aimovig", "Qulipta (atogepant)", "Emgality"]
    assert rows[0].cells["ql"].endswith("days, then 120 mg monthly")


def test_an_unclosed_parenthesis_does_not_swallow_the_page():
    lines = [_line(100, ("PREFERRED AGENTS", 50), ("NON-PREFERRED AGENTS", 300)),
             _line(115, ("Nicotrol (nicotine", 50))]  # fmt: skip
    names = ["Nicotine Gum", "Nicotine Lozenge", "Nicotine Patch", "Chantix", "Zyban",
             "Nicotrol Inhaler", "Nicorette", "Habitrol"]  # fmt: skip
    for k, name in enumerate(names):
        lines.append(_line(127 + 12 * k, (name, 50)))
    rows, _ = layout.parse([_page(*lines)])
    assert all(len(r.cells["drug"].split()) <= 16 for r in rows) and len(rows) > 1


def test_bulleted_lists_with_notes_and_columns_that_move_from_page_to_page():
    """Indiana: one centred header for the whole list, columns that sit elsewhere on the next
    page, "o ST –" / "o QL –" notes under each drug, and "o" sub-items under a product family."""
    header = _line(100, ("DRUG CLASS", 92), ("PREFERRED", 283), ("NON-PREFERRED", 499),
                   ("PA CRITERIA", 677))  # fmt: skip
    p1 = _page(
        header,
        _line(115, ("Anticoagulants", 45), ("•", 201), ("Eliquis", 219), ("•", 427),
              ("Savaysa", 445)),  # fmt: skip
        _line(127, ("o", 219), ("QL – 2 tabs/day of 2.5mg; 4 tabs/day for 7 days,", 237),
              ("o", 445), ("ST – must have trialed Eliquis and Xarelto OR", 463)),  # fmt: skip
        _line(139, ("then 2 tabs/day for 5mg", 237), ("medical justification for use", 463)),
        _line(151, ("•", 201), ("warfarin", 219), ("•", 427), ("Bevyxxa", 445)),
        _line(163, ("•", 201), ("Xarelto 10mg tablets", 219), ("•", 427), ("dabigatran", 445)),
    )
    p2 = Page(2, 612, 792, [  # no header; the columns sit further left on this page
        _line(100, ("Targeted Immunomodulators", 45), ("•", 180), ("adalimumab products", 198),
              ("•", 395), ("Leqselvi", 413)),  # fmt: skip
        _line(112, ("o", 198), ("Hadlima", 216), ("•", 395), ("Omvoh", 413)),
        _line(124, ("o", 198), ("Simlandi", 216), ("•", 395), ("Sotyktu", 413)),
        _line(136, ("•", 180), ("Enbrel", 198), ("•", 395), ("Spevigo", 413)),
    ], False)  # fmt: skip
    rows, _ = layout.parse([p1, p2])
    got = {(r.cells["drug"], r.list_role) for r in rows}
    assert got == {
        ("Eliquis", "preferred"), ("warfarin", "preferred"), ("Xarelto 10mg tablets", "preferred"),
        ("Savaysa", "non_preferred"), ("Bevyxxa", "non_preferred"), ("dabigatran", "non_preferred"),
        ("adalimumab products", "preferred"), ("Hadlima", "preferred"), ("Simlandi", "preferred"),
        ("Enbrel", "preferred"), ("Leqselvi", "non_preferred"), ("Omvoh", "non_preferred"),
        ("Sotyktu", "non_preferred"), ("Spevigo", "non_preferred"),
    }  # fmt: skip
    eliquis = next(r for r in rows if r.cells["drug"] == "Eliquis")
    assert eliquis.cells["ql"].endswith("then 2 tabs/day for 5mg")
    savaysa = next(r for r in rows if r.cells["drug"] == "Savaysa")
    assert savaysa.cells["st"].startswith("ST – must have trialed")


def test_superscripts_titles_across_columns_and_other_tables():
    """New York: ® set apart with a footnote number, class titles centred across the columns,
    and a dose optimization table that isn't a list of statuses."""
    page = _page(
        _line(100, ("Preferred Drugs", 78), ("Non-Preferred Drugs", 245),
              ("Coverage Parameters", 515)),  # fmt: skip
        _line(115, ("Antipsychotics – Second Generation CC, ST", 277)),
        _line(130, ("Taltz", 37), ("Nucala", 219), ("CLINICAL CRITERIA (CC)", 400)),
        _line(136, ("®", 60), ("1", 66), ("®", 250), ("2", 256)),
        _line(145, ("Tradjenta", 37), ("Actemra", 219), ("DOSE OPTIMIZATION (DO)", 400)),
        _line(160, ("Januvia", 37), ("Cosentyx", 219), ("STEP THERAPY (ST)", 400)),
        _line(175, ("Brand Name", 122), ("Dose Optimization Limitations", 431)),
        _line(190, ("Nexium 20 mg", 38), ("1 daily", 281), ("Capsule", 354)),
    )
    rows, _ = layout.parse([page])
    assert [(r.cells["drug"], r.list_role) for r in rows] == [
        ("Taltz", "preferred"), ("Nucala", "non_preferred"), ("Tradjenta", "preferred"),
        ("Actemra", "non_preferred"), ("Januvia", "preferred"), ("Cosentyx", "non_preferred"),
    ]  # fmt: skip
    assert rows[0].drug_class == "Antipsychotics – Second Generation CC, ST"


def test_a_non_preferred_column_named_with_its_rule():
    """Maryland: "Non-Preferred / Requires Prior Authorization"."""
    lay = layout.header(_line(100, ("Drug Class", 35), ("Preferred", 169),
                              ("Non-Preferred / Requires Prior Authorization", 367)), 612)  # fmt: skip
    assert lay is not None and lay.mode == "list"
    assert [c.role for c in lay.columns] == ["class", "preferred", "non_preferred"]


def test_product_tables_with_label_and_generic_name_columns():
    """Florida: HIC3 | HIC3 Desc | Label Name | Generic Name | ages | PA Required."""
    lay = layout.header(_line(56, ("HIC3", 20), ("HIC3 Desc", 58), ("Label Name", 272),
                              ("Generic Name", 403), ("Medicaid Min Age", 554),
                              ("Medicaid Max Age", 633), ("PA Required", 717)), 792)  # fmt: skip
    assert lay is not None and lay.mode == "record"
    roles = [c.role for c in lay.columns]
    assert roles.count("drug") == 1 and "generic" in roles and "pa" in roles

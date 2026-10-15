import csv

from pdlwatch import review
from pdlwatch.cli import main
from pdlwatch.extract.document import DocResult
from pdlwatch.extract.rows import Row
from pdlwatch.pipeline import tables

PAGE = "Preferred Agents Non-Preferred Agents Ozempic Trulicity QTY LIMIT: 2/day note Mounjaro"


def _row(text, product, status, accepted, reasons, y):
    r = Row("OH-1", "OH", 3, "list", text, "GLP-1", product, None, "brand", 100.0, status,
            accepted=accepted, review_reasons=reasons, y=y)  # fmt: skip
    r.row_key = review.row_key(PAGE, y, text)
    return r


def _doc():
    rows = [
        _row("Ozempic", "Ozempic", None, False, ["status not readable"], 100.0),
        _row("QTY LIMIT: 2/day note", None, "preferred", False,
             ["drug name not in the dictionary"], 112.0),
        _row("Mounjaro", "Mounjaro", "preferred", True, [], 124.0),
        _row("Trulicity pen", "Trulicity", "non_preferred", False,
             ["words beside the name the dictionary doesn't know"], 136.0),
    ]  # fmt: skip
    return DocResult("OH-1", "/x/OH-1.pdf", "OH", "2026-07-01", "Ohio PDL", "pdl", 9, 0, rows)


def test_row_keys_follow_what_was_read():
    assert review.row_key(PAGE, 100.3, "Ozempic  Pen") == review.row_key(PAGE, 100.0, "ozempic pen")
    assert review.row_key(PAGE, 100.0, "Ozempic") != review.row_key(PAGE, 112.0, "Ozempic")
    assert review.row_key(PAGE, 100.0, "Ozempic") != review.row_key(PAGE + " v2", 100.0, "Ozempic")


def test_decisions_made_on_a_review_csv_apply_to_every_table(tmp_path):
    queue = tables([_doc()], "test")["review_queue"]
    assert len(queue) == 3
    path = tmp_path / "review.csv"
    review.to_csv(queue, path)
    with path.open(newline="") as f:
        lines = list(csv.DictReader(f))
    by_text = {line["drug_text"]: line for line in lines}
    by_text["Ozempic"].update(decision="correct", corrected_status="Preferred", corrected_pa="no")
    by_text["QTY LIMIT: 2/day note"].update(decision="reject", note="a note, not a drug")
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(lines[0]))
        w.writeheader()
        w.writerows(lines)
    decisions, errors = review.read_csv(path)
    assert errors == [] and len(decisions) == 2  # the line left blank is skipped
    log = tmp_path / "review_decisions.jsonl"
    review.record(log, decisions, "QA")
    audit = review.row_key(PAGE, 124.0, "Mounjaro")  # an accepted row checked in an audit
    review.record(log, [{"row_key": audit, "decision": "accept"}], "QA")
    review.record(log, [{"row_key": "gone", "decision": "accept"}], "QA")  # its page changed

    tabs = tables([_doc()], "test", review.load(log))
    access = tabs["drug_access"].set_index("product")
    assert access.loc["Ozempic", ["status", "pa", "reviewed"]].tolist() == [
        "preferred", False, "correct"]  # fmt: skip
    assert access.loc["Mounjaro", "reviewed"] == "accept"
    assert tabs["review_queue"]["drug_text"].tolist() == ["Trulicity pen"]
    rows = tabs["formulary_rows"].set_index("drug_text")
    assert rows.loc["QTY LIMIT: 2/day note", "reviewed"] == "reject"
    stats = tabs["formulary_rows"].attrs["review"]
    assert (stats["decisions"], stats["applied"], stats["orphaned"]) == (4, 3, 1)
    assert stats["by_reason"]["status not readable"] == {"reviewed": 1, "wrong": 1,
                                                         "error_rate": 1.0}  # fmt: skip
    assert stats["by_reason"][review.AUTO] == {"reviewed": 1, "wrong": 0, "error_rate": 0.0}


def test_the_latest_decision_wins(tmp_path):
    log = tmp_path / "review_decisions.jsonl"
    key = review.row_key(PAGE, 100.0, "Ozempic")
    review.record(log, [{"row_key": key, "decision": "reject"}], "A")
    review.record(log, [{"row_key": key, "decision": "accept"}], "B")
    assert review.load(log)[key]["decision"] == "accept"
    assert len(log.read_text().splitlines()) == 2  # appended, never rewritten


def test_bad_lines_are_reported_and_nothing_is_recorded(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PDLWATCH_DB", str(tmp_path / "pdlwatch.duckdb"))
    path = tmp_path / "review.csv"
    path.write_text(
        "row_key,drug_text,decision,corrected_status,corrected_pa\n"
        "k1,Ozempic,approve,,\n"
        "k2,Mounjaro,correct,covered,\n"
        "k3,Trulicity,correct,,\n"
        "k4,Byetta,correct,preferred,maybe\n"
        "k5,Victoza,accept,,\n"
    )
    assert main(["review", "--apply", str(path)]) == 2  # no reviewer named
    assert main(["review", "--apply", str(path), "--reviewer", "QA"]) == 1
    err = capsys.readouterr().err
    for n in (2, 3, 4, 5):
        assert f"line {n}:" in err
    assert not review.decisions_path(tmp_path / "pdlwatch.duckdb").exists()

import httpx
import pandas as pd
import pytest

from pdlwatch import alerts

CHANGES = pd.DataFrame(
    [("OH", "Mounjaro", "GLP-1 Receptor Agonists", "status", "preferred", "non_preferred", False),
     ("OH", "Ozempic", "GLP-1 Receptor Agonists", "ql", None, "4 per 28 days", False),
     ("TX", "Wegovy", "Anti-Obesity Agents", "added", None, "preferred", False),
     ("TX", "Januvia", "DPP-4 Inhibitors", "removed", "preferred", None, True),
     ("TX", "Jardiance", "SGLT2 Inhibitors", "status", "non_preferred", "preferred", False)],
    columns=["state", "product", "drug_class", "change", "before", "after", "needs_review"],
).assign(from_doc=lambda d: d["state"] + "-1", to_doc=lambda d: d["state"] + "-2",
         effective="2026-10-01", confidence=0.9)  # fmt: skip
PRODUCTS = pd.DataFrame({"product": ["Mounjaro", "Ozempic", "Wegovy", "Januvia", "Jardiance"],
                         "ingredient": ["tirzepatide", "semaglutide", "semaglutide",
                                        "sitagliptin", "empagliflozin"]})  # fmt: skip


@pytest.fixture
def watchlist(tmp_path):
    p = tmp_path / "wl.yaml"
    p.write_text("name: team\nproducts: [Mounjaro]\ningredients: [semaglutide]\n"
                 "classes: [DPP-4 Inhibitors]\n")  # fmt: skip
    return alerts.Watchlist.load(p)


def test_a_watchlist_picks_its_changes_and_rates_them(watchlist):
    got = alerts.match(CHANGES, PRODUCTS, watchlist)
    assert list(zip(got["product"], got["severity"], got["confirmed"], strict=True)) == [
        ("Mounjaro", "high", True),
        ("Januvia", "high", False),
        ("Ozempic", "medium", True),
        ("Wegovy", "info", True),
    ]
    assert "Jardiance" not in set(got["product"])


def test_states_narrow_a_watchlist(tmp_path):
    p = tmp_path / "wl.yaml"
    p.write_text("name: tx\ningredients: [semaglutide]\nstates: [tx]\n")
    got = alerts.match(CHANGES, PRODUCTS, alerts.Watchlist.load(p))
    assert list(got["product"]) == ["Wegovy"]


def test_an_empty_watchlist_is_an_error(tmp_path):
    p = tmp_path / "wl.yaml"
    p.write_text("name: nothing\nstates: [OH]\n")
    with pytest.raises(ValueError, match="at least one"):
        alerts.Watchlist.load(p)


def test_the_digest_keeps_unconfirmed_changes_apart(watchlist):
    text = alerts.digest(alerts.match(CHANGES, PRODUCTS, watchlist), watchlist)
    assert "1 high, 1 medium, 1 info; 1 more waiting for review." in text
    assert "**OH · Mounjaro**: preferred → non-preferred, effective 2026-10-01" in text
    review = text.split("## Waiting for review")[1]
    assert "Januvia" in review and "Mounjaro" not in review


def test_sent_alerts_are_not_sent_again(watchlist, tmp_path):
    got = alerts.match(CHANGES, PRODUCTS, watchlist)
    ledger = tmp_path / "alerts_sent.jsonl"
    alerts.record(ledger, "team", got[got["confirmed"]])
    sent = alerts.sent_keys(ledger, "team")
    assert len(sent) == 3 and alerts.sent_keys(ledger, "another team") == set()
    assert list(got[~got["key"].isin(sent)]["product"]) == ["Januvia"]  # still unconfirmed


def test_the_webhook_gets_the_digest_only_when_configured(monkeypatch):
    monkeypatch.delenv("PDLWATCH_ALERT_WEBHOOK", raising=False)
    assert alerts.send("hello") is False
    posted = []

    def handler(req: httpx.Request) -> httpx.Response:
        posted.append(req.read())
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert alerts.send("hello", "https://hooks.example.test/x", client) is True
    assert posted == [b'{"text":"hello"}']

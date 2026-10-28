"""The RxNorm lexicon, built from a tiny release in RxNorm's RRF layout (sample rows, not real
RxCUIs: the real files are downloaded by the real-data workflow, not committed)."""

import zipfile
from datetime import date

import httpx
import pytest

from pdlwatch import rxnorm
from pdlwatch.drugs import match_product

# RXCUI|LAT|TS|LUI|STT|SUI|ISPREF|RXAUI|SAUI|SCUI|SDUI|SAB|TTY|CODE|STR|SRL|SUPPRESS|CVF|
CONSO = [
    ("901", "IN", "ticagrelor", "N"), ("902", "BN", "Brilinta", "N"),
    ("903", "IN", "fluticasone", "N"), ("904", "IN", "salmeterol", "N"),
    ("905", "BN", "Advair HFA", "N"), ("906", "MIN", "fluticasone / salmeterol", "N"),
    ("907", "BN", "Oldbrand", "O"),  # obsolete: left out
    ("908", "SCD", "ticagrelor 90 MG Oral Tablet", "N"),  # a clinical drug: left out
    ("909", "BN", "Gel", "N"),  # too short to be found inside a longer cell
    ("910", "IN", "labetalol", "N"), ("911", "PIN", "labetalol hydrochloride", "N"),
    ("912", "IN", "lisinopril", "N"), ("913", "IN", "hydrochlorothiazide", "N"),
    ("914", "MIN", "hydrochlorothiazide / lisinopril", "N"),
    ("915", "BN", "Vigamox", "N"), ("916", "IN", "moxifloxacin", "N"),
    ("917", "IN", "metoprolol", "N"), ("918", "PIN", "metoprolol succinate", "N"),
    ("919", "PIN", "metoprolol tartrate", "N"),
]  # fmt: skip
# RXCUI1|RXAUI1|STYPE1|REL|RXCUI2|RXAUI2|STYPE2|RELA|RUI|SRUI|SAB|SL|DIR|RG|SUPPRESS|CVF|
REL = [("901", "902", "has_tradename"), ("905", "903", "tradename_of"),
       ("905", "904", "tradename_of"), ("915", "916", "tradename_of")]  # fmt: skip


@pytest.fixture
def release(tmp_path):
    z = tmp_path / "RxNorm_full_prescribe_10052026.zip"
    conso = "".join(f"{c}|ENG|P|L|PF|S|Y|A{c}||||RXNORM|{t}|{c}|{s}|0|{sup}|4096|\n"
                    for c, t, s, sup in CONSO)  # fmt: skip
    rel = "".join(f"{a}||CUI|RO|{b}||CUI|{r}|R1||RXNORM||||N||\n" for a, b, r in REL)
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("rrf/RXNCONSO.RRF", conso)
        f.writestr("rrf/RXNREL.RRF", rel)
    return z


@pytest.fixture
def lexicon(release, tmp_path, monkeypatch):
    path = rxnorm.save(rxnorm.build(release), tmp_path / "rxnorm.json")
    monkeypatch.setenv("PDLWATCH_RXNORM", str(path))
    return path


def test_the_lexicon_has_ingredients_and_brands_with_their_ingredients(release):
    names = rxnorm.build(release)["names"]
    assert names["brilinta"] == {"name": "Brilinta", "kind": "brand", "ingredient": "ticagrelor",
                                 "rxcui": "902", "tty": "BN"}  # fmt: skip
    assert names["advair hfa"]["ingredient"] == "fluticasone / salmeterol"
    assert names["fluticasone/salmeterol"]["kind"] == "generic"
    assert "oldbrand" not in names and not any("tablet" in k for k in names)


def test_names_outside_the_dictionary_are_found(lexicon):
    m = match_product("Brilinta 90 mg tablet")
    assert (m.product.name, m.product.ingredient, m.product.rxcui, m.score) == (
        "Brilinta", "ticagrelor", "902", 100.0)  # fmt: skip
    # the exact brand beats the dictionary's "Advair" alias for Advair Diskus
    assert match_product("Advair HFA").product.name == "Advair HFA"
    inside = match_product("Brilinta Starter Kit Plus")
    assert inside.product.name == "Brilinta" and inside.score < 100
    assert match_product("Hair Gel Plus").product is None  # one short word is not a match
    assert match_product("Ozempic").product.name == "Ozempic"  # the dictionary still comes first


def test_salts_forms_and_combinations_are_read_as_rxnorm_names_them(lexicon):
    for text, name in [
        ("labetalol HCl", "labetalol hydrochloride"),  # the salt RxNorm keeps
        ("LISINOPRIL/HYDROCHLOROTHIAZIDE", "hydrochlorothiazide / lisinopril"),  # RxNorm's order
        ("Vigamox 0.5% Eye Drops", "Vigamox"),
        ("metoprolol succ ER", "metoprolol succinate"),  # not metoprolol tartrate
        ("Metoprolol Tartrate", "metoprolol tartrate"),
        ("moxifloxacin hydrochloride drops", "moxifloxacin"),  # a salt RxNorm doesn't keep
    ]:
        m = match_product(text)
        assert (m.product.name, m.score) == (name, 100.0), text
    assert match_product("Vigamox Plus").score < 100  # a word it doesn't know: review
    assert match_product("Advair HFA").product.name == "Advair HFA"  # a brand with the word


def test_without_a_lexicon_nothing_changes(monkeypatch):
    monkeypatch.delenv("PDLWATCH_RXNORM", raising=False)
    assert match_product("Brilinta").product is None
    assert rxnorm.fingerprint() == ""


def test_release_dates_are_first_mondays():
    assert rxnorm.release_dates(date(2026, 10, 14)) == [
        date(2026, 10, 5), date(2026, 9, 7), date(2026, 8, 3)]  # fmt: skip
    assert rxnorm.release_dates(date(2026, 10, 2))[0] == date(2026, 9, 7)  # not out yet


def test_download_falls_back_to_last_months_release(release, tmp_path):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        seen.append(name)
        if name == "rxnormfiles.html":
            return httpx.Response(503)
        if name == "RxNorm_full_prescribe_09082026.zip":  # Labor Day moved the release
            return httpx.Response(200, content=release.read_bytes())
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        got = rxnorm.download(tmp_path / "dl", client, today=date(2026, 10, 14))
    assert got.name == "RxNorm_full_prescribe_09082026.zip" and zipfile.is_zipfile(got)
    assert seen == ["rxnormfiles.html", "RxNorm_full_prescribe_10052026.zip",
                    "RxNorm_full_prescribe_10062026.zip", "RxNorm_full_prescribe_09072026.zip",
                    "RxNorm_full_prescribe_09082026.zip"]  # fmt: skip


def test_the_files_page_links_come_first(release, tmp_path):
    page = (
        '<a href="https://download.nlm.nih.gov/rxnorm/RxNorm_full_prescribe_09082026.zip">'
        '<a href="https://download.nlm.nih.gov/rxnorm/RxNorm_full_prescribe_10052026.zip">'
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        urls = rxnorm.candidate_urls(client, today=date(2026, 10, 14))
    assert urls[:2] == [
        "https://download.nlm.nih.gov/rxnorm/RxNorm_full_prescribe_10052026.zip",
        "https://download.nlm.nih.gov/rxnorm/RxNorm_full_prescribe_09082026.zip",
    ]

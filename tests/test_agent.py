import json
from types import SimpleNamespace

import pytest

from pdlwatch.agent import provider
from pdlwatch.agent.core import Agent, system_prompt
from pdlwatch.agent.provider import MockProvider, ToolCall, Turn, mock_route
from pdlwatch.agent.sql_guard import UnsafeSQLError, scrub_error, validate
from pdlwatch.agent.tools import execute, resolve_drugs, resolve_state, tool_definitions
from pdlwatch.evals import run_evals
from pdlwatch.extract.llm import LLMExtractor, LLMRow, PageRows
from pdlwatch.pdf import Page


@pytest.mark.parametrize(
    ("question", "tool", "args"),
    [
        ("Where is Ozempic preferred?", "drug_access", {"drug": "Ozempic"}),
        ("Which states cover semaglutide?", "drug_access", {"drug": "semaglutide"}),
        ("What does Ohio cover for GLP-1s?", "state_formulary",
         {"state": "OH", "drug_class": "GLP-1 Receptor Agonists"}),
        ("Show the SGLT2 landscape", "class_landscape", {"drug_class": "SGLT2 Inhibitors"}),
        ("Compare Eliquis vs Xarelto", "compare_drugs", {"drugs": ["Eliquis", "Xarelto"]}),
        ("What changed in Texas since the last PDL?", "pdl_changes", {"state": "TX"}),
        ("Which rows need a look in Iowa?", "review_queue", {"state": "IA"}),
        ("How accurate is the extraction?", "extraction_quality", {}),
        ("Which documents do we have for West Virginia?", "documents", {"state": "WV"}),
        ("Which states put a brand ahead of its generic?", "run_saved_query",
         {"name": "brand_over_generic"}),
        ("Using SQL, count rows by status", "run_sql", None),
    ],
)  # fmt: skip
def test_mock_router_picks_tool_and_arguments(question, tool, args):
    got_tool, got_args = mock_route(question)
    assert got_tool == tool
    if args is not None:
        assert got_args == args


def test_pa_means_prior_authorization_not_pennsylvania():
    tool, args = mock_route("Which GLP-1s need PA in Ohio?")
    assert args.get("state") == "OH"
    assert mock_route("What does Pennsylvania cover for GLP-1s?")[1]["state"] == "PA"


def test_resolvers():
    assert set(resolve_drugs("semaglutide")) == {"Ozempic", "Rybelsus", "Wegovy"}
    assert resolve_drugs("ozempic") == ["Ozempic"]
    assert resolve_state("west virginia") == "WV" and resolve_state("tx") == "TX"
    with pytest.raises(ValueError):
        resolve_drugs("not-a-drug-at-all")
    with pytest.raises(ValueError):
        resolve_state("Atlantis")


def test_mock_agent_answers_with_sources(built):
    r = Agent(MockProvider()).ask("Where is Ozempic preferred?")
    assert r.tools_used == ["drug_access"] and not r.steps[0].is_error
    assert "p." in r.answer  # every status cites document and page


def test_tool_errors_go_back_to_the_model_not_up(built):
    out, err = execute("state_formulary", {"state": "Atlantis"})
    assert err and "unknown state" in json.loads(out)["error"]
    out, err = execute("no_such_tool", {})
    assert err
    out, err = execute("run_sql", {"sql": "DROP TABLE drug_access"})
    assert err and "SELECT" in json.loads(out)["error"]
    out, err = execute("drug_access", {"drug": "Ozempic'; DROP TABLE drug_access; --"})
    # names are resolved against the dictionary; only the dictionary's name reaches SQL
    assert not err and {r["product"] for r in json.loads(out)["rows"]} <= {"Ozempic"}


def test_sql_guard():
    assert validate("SELECT * FROM drug_access").endswith("LIMIT 200")
    assert "LIMIT 5" in validate("SELECT state FROM drug_access LIMIT 5")
    assert "LIMIT 200" in validate("SELECT state FROM drug_access LIMIT 100000")
    assert validate("WITH x AS (SELECT * FROM changes) SELECT COUNT(*) FROM x")
    for bad in [
        "DELETE FROM drug_access",
        "SELECT 1; SELECT 2",
        "SELECT * FROM secrets",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM main.drug_access",
        "SELECT getenv('HOME')",
        "COPY drug_access TO 'x.csv'",
    ]:
        with pytest.raises(UnsafeSQLError):
            validate(bad)
    assert scrub_error("Conversion Error: 'Ozempic' is not a number") == (
        "Conversion Error: '...' is not a number"
    )


def test_rogue_model_cant_run_unsafe_sql(built):
    class Rogue:
        name, model = "rogue", "rogue"

        def __init__(self):
            self.n = 0

        def complete(self, system, messages, tools):
            self.n += 1
            if self.n == 1:
                call = ToolCall("t1", "run_sql", {"sql": "DROP TABLE drug_access"})
                block = {"type": "tool_use", "id": "t1", "name": "run_sql", "input": call.input}
                return Turn("", [call], "tool_use", [block])
            return Turn("done", [], "end_turn", [{"type": "text", "text": "done"}])

    r = Agent(Rogue()).ask("drop it")
    assert r.steps[0].is_error and "SELECT" in r.steps[0].output["error"]


def test_tool_schemas_are_complete():
    for t in tool_definitions():
        assert t["description"] and t["input_schema"]["type"] == "object"


def test_system_prompt_names_the_loaded_data(built):
    assert f"{built['results']['data']['documents']} documents" in system_prompt()


def test_providers_translate_tool_calls(monkeypatch):
    monkeypatch.setenv("PDLWATCH_MODEL", "deployment-1")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    call = SimpleNamespace(
        id="c0", function=SimpleNamespace(name="drug_access", arguments='{"drug": "Ozempic"}')
    )
    msg = SimpleNamespace(content=None, tool_calls=[call])
    sent = {}

    def create(**kw):
        sent.update(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    for cls in (provider.OpenAIProvider, provider.AzureOpenAIProvider):
        prov = cls()
        prov.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        turn = prov.complete("sys", [{"role": "user", "content": "Ozempic?"}], tool_definitions())
        assert turn.tool_calls[0].input == {"drug": "Ozempic"} and sent["model"] == "deployment-1"
    a = provider.AnthropicProvider()
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="t1", name="drug_access",
                                 input={"drug": "Ozempic"})],
        stop_reason="tool_use", usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )  # fmt: skip
    a.client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: resp))
    turn = a.complete("sys", [{"role": "user", "content": "Ozempic?"}], tool_definitions())
    assert turn.tool_calls[0].name == "drug_access" and turn.usage["input_tokens"] == 10


def test_providers_request_structured_output(monkeypatch):
    monkeypatch.setenv("PDLWATCH_MODEL", "model-under-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    rows = PageRows(rows=[])
    sent = {}

    def parse(**kw):
        sent.update(kw)
        return SimpleNamespace(parsed_output=rows, stop_reason="end_turn")

    a = provider.AnthropicProvider()
    a.client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    assert a.structured("sys", "page", PageRows) is rows
    assert sent["output_format"] is PageRows and sent["model"] == "model-under-test"
    cut = SimpleNamespace(parsed_output=None, stop_reason="max_tokens")
    a.client = SimpleNamespace(messages=SimpleNamespace(parse=lambda **kw: cut))
    with pytest.raises(ValueError):
        a.structured("sys", "page", PageRows)
    o = provider.OpenAIProvider()
    msg = SimpleNamespace(parsed=rows, refusal=None)
    o.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        parse=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=msg)]))))  # fmt: skip
    assert o.structured("sys", "page", PageRows) is rows


class _FakeLLM:
    """Returns fixed rows for any page: one right, one contradicting the rules, one invented."""

    name = "fake"

    def structured(self, system, user, schema):
        return PageRows(rows=[
            LLMRow(drug="Ozempic", status="preferred", prior_authorization=False,
                   evidence="Ozempic may be dispensed without approval"),
            LLMRow(drug="Mounjaro", status="preferred", evidence="Mounjaro is covered"),
            LLMRow(drug="Wegovy", status="non_preferred", evidence="Wegovy needs a request"),
        ])  # fmt: skip


def _prose_page(text: str) -> Page:
    from pdlwatch.pdf import Line, Phrase, Word

    lines = []
    for i, sentence in enumerate(text.split("\n")):
        words, x = [], 50.0
        for w in sentence.split():
            words.append(Word(w, x, x + 5 * len(w), 100 + 12 * i, 109 + 12 * i, False))
            x += 5 * len(w) + 2.5
        lines.append(Line([Phrase(words)]))
    return Page(1, 612, 792, lines, False)


def test_llm_rows_are_grounded_and_checked_against_the_rules():
    page = _prose_page(
        "GLP-1 Receptor Agonists\n"
        "Ozempic is a preferred agent and does not require prior authorization.\n"
        "Mounjaro requires prior authorization; the prescriber must submit a request."
    )
    ex = LLMExtractor(_FakeLLM())
    rows, calls = ex.extract([page], "XX-PAC-2026-07", "OH")
    by = {r.product: r for r in rows}
    assert calls == 1
    assert "Wegovy" not in by and ex.rejected[0]["drug"] == "Wegovy"  # not on the page
    assert by["Ozempic"].method == "llm+rules" and not by["Ozempic"].review_reasons
    assert "LLM and rules disagree" in by["Mounjaro"].review_reasons
    with pytest.raises(ValueError):
        LLMExtractor(MockProvider())  # no structured output


def test_eval_grading():
    assert run_evals.extract_numbers("12 states, 3.5% and 1,204 rows") == [12, 3.5, 1204]
    assert run_evals.fact_present(0.9905, "row recall 99.1%")
    assert run_evals.fact_present("Ohio", "in ohio the PDL ...")
    assert not run_evals.fact_present(12, "13 states")

"""Access Copilot: question -> tool calls -> grounded answer with sources.

A plain tool-use loop (no framework) so every step is visible and testable:

1. send the question with the tool catalogue,
2. execute each requested tool and return *all* results in one user message,
3. repeat until the model stops calling tools or ``max_steps`` is hit.

The trace (tool names, arguments, results, latency, tokens) is returned alongside the
answer so the API can show its work and the eval harness can grade tool selection.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from pdlwatch import pipeline
from pdlwatch.agent.provider import LLMProvider, get_provider
from pdlwatch.agent.tools import execute, tool_definitions
from pdlwatch.config import get_settings

SYSTEM_PROMPT = """\
You are Access Copilot, a market access analyst's assistant for US Medicaid pharmacy coverage.
{data} You answer questions about where drugs are preferred, non-preferred or not covered on
state preferred drug lists (PDLs), the prior authorization (PA), step therapy and quantity limits
that apply, what changed between PDL versions, and how reliable the extraction is.

How to answer:
- Use the tools for every status and number. Never rely on memory of any state's PDL; if no
  tool can answer, say so. Prefer a purpose-built tool or a saved query over run_sql.
- Lead with the answer in one or two sentences, then the supporting rows (a short table is
  fine), then one line on sources or caveats.
- Cite the source (document and page) for statuses you quote.
- In Medicaid, non-preferred means prior authorization is required; say "non-preferred (PA)".
- Rows waiting for review are not in the access tables. Mention them when a tool reports any.
- Be concise. The audience is a market access or pricing team deciding where to act.
"""


def system_prompt() -> str:
    """The prompt, with a line on which documents are loaded."""
    try:
        d = pipeline.load_results()["data"]
    except FileNotFoundError:
        return SYSTEM_PROMPT.format(data="")
    kind = ("the synthetic test corpus (fictional statuses)" if d["source"] == "synthetic"
            else d["source"])  # fmt: skip
    data = (f"Loaded: {kind}, {d['documents']} documents from {d['states']} states, "
            f"{d['rows']:,} rows.")  # fmt: skip
    return SYSTEM_PROMPT.format(data=data)


@dataclass
class Step:
    tool: str
    input: dict[str, Any]
    output: dict[str, Any]
    is_error: bool
    latency_ms: int


@dataclass
class AgentResult:
    question: str
    answer: str
    steps: list[Step] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    stop_reason: str = ""
    latency_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def tools_used(self) -> list[str]:
        return [s.tool for s in self.steps]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tools_used"] = self.tools_used
        return d


class Agent:
    def __init__(self, provider: LLMProvider | None = None, max_steps: int | None = None) -> None:
        self.provider = provider or get_provider()
        self.max_steps = max_steps or get_settings().max_agent_steps
        self.tools = tool_definitions()
        self.system = system_prompt()

    def ask(self, question: str) -> AgentResult:
        t0 = time.perf_counter()
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        result = AgentResult(
            question=question, answer="", provider=self.provider.name, model=self.provider.model
        )
        usage = {"input_tokens": 0, "output_tokens": 0}

        for _ in range(self.max_steps):
            turn = self.provider.complete(self.system, messages, self.tools)
            for k, v in turn.usage.items():
                usage[k] = usage.get(k, 0) + v
            result.stop_reason = turn.stop_reason
            if turn.stop_reason == "refusal":
                result.answer = turn.text
                break
            if turn.stop_reason == "max_tokens":  # a cut-off tool call must not run
                result.answer = turn.text or "The answer was cut off before it finished."
                break
            messages.append({"role": "assistant", "content": turn.content})
            if not turn.tool_calls:
                result.answer = turn.text
                break
            tool_results = []
            for call in turn.tool_calls:
                s0 = time.perf_counter()
                out, is_error = execute(call.name, call.input)
                result.steps.append(
                    Step(
                        tool=call.name,
                        input=call.input,
                        output=json.loads(out),
                        is_error=is_error,
                        latency_ms=int((time.perf_counter() - s0) * 1000),
                    )
                )
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": out,
                }
                if is_error:
                    block["is_error"] = True
                tool_results.append(block)
            # all results for this turn go back in a single user message
            messages.append({"role": "user", "content": tool_results})
        else:
            result.answer = result.answer or (
                "I hit the step limit before finishing. Partial results are in the trace."
            )
            result.stop_reason = "max_steps"

        result.usage = usage
        result.latency_ms = int((time.perf_counter() - t0) * 1000)
        return result


def ask(question: str, provider: str | None = None) -> AgentResult:
    return Agent(get_provider(provider)).ask(question)

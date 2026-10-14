"""Provider-agnostic LLM interface.

The agent speaks one message format (Anthropic-style content blocks) and every provider
translates to and from it. That keeps the agent loop identical whether it runs on the
Anthropic API, OpenAI, Azure OpenAI, or the deterministic ``MockProvider`` used in tests and CI.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel

from pdlwatch.config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Turn:
    """One assistant response, normalised across providers."""

    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # "end_turn" | "tool_use" | "refusal" | "max_tokens" | ...
    # Exactly what to append to history as the assistant message content. For Anthropic this
    # is the raw response content (thinking blocks must be echoed back unchanged).
    content: list[Any]
    usage: dict[str, int] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Turn: ...


# --------------------------------------------------------------------------- Anthropic


def _require_model(s: Settings, provider: str) -> str:
    if not s.model:
        raise ValueError(
            f"PDLWATCH_MODEL is not set; set it to the {provider} model id to use, "
            "or use PDLWATCH_PROVIDER=mock"
        )
    return s.model


class AnthropicProvider:
    """Anthropic Messages API via the official ``anthropic`` SDK."""

    name = "anthropic"

    def __init__(self, settings: Settings | None = None) -> None:
        import anthropic

        s = settings or get_settings()
        self.model = _require_model(s, "Anthropic")
        self.effort = s.effort
        self.fallbacks = s.fallbacks
        self.client = anthropic.Anthropic()

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Turn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            "system": system,
            "messages": messages,
            "tools": tools,
            "output_config": {"effort": self.effort},
        }
        if self.fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            resp = self.client.beta.messages.create(**kwargs)
        else:
            resp = self.client.messages.create(**kwargs)

        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, input=dict(b.input))
            for b in resp.content
            if b.type == "tool_use"
        ]
        if resp.stop_reason == "refusal":
            text = text or "The model declined to answer this request."
        return Turn(
            text=text,
            tool_calls=calls,
            stop_reason=str(resp.stop_reason),
            content=list(resp.content),
            usage={
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            },
        )

    def structured(self, system: str, user: str, schema: type[T]) -> T:
        """One request whose answer must match ``schema`` (structured outputs)."""
        resp = self.client.messages.parse(
            model=self.model,
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config=cast(Any, {"effort": self.effort}),
        )
        if resp.parsed_output is None:
            raise ValueError(f"no structured output (stop reason: {resp.stop_reason})")
        return resp.parsed_output


# --------------------------------------------------------------------------- OpenAI


def _block(b: Any, key: str, default: Any = None) -> Any:
    return b.get(key, default) if isinstance(b, dict) else getattr(b, key, default)


class OpenAIProvider:
    """Optional OpenAI backend (``pip install "pdlwatch[llm]"``)."""

    name = "openai"

    def __init__(self, settings: Settings | None = None) -> None:
        from openai import OpenAI

        s = settings or get_settings()
        self.model = os.environ.get("PDLWATCH_OPENAI_MODEL") or _require_model(s, "OpenAI")
        self.client = OpenAI()

    @staticmethod
    def _convert(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            content = m["content"]
            if isinstance(content, str):
                out.append({"role": m["role"], "content": content})
                continue
            if m["role"] == "assistant":
                text = "".join(
                    _block(b, "text", "") for b in content if _block(b, "type") == "text"
                )
                calls = [
                    {
                        "id": _block(b, "id"),
                        "type": "function",
                        "function": {
                            "name": _block(b, "name"),
                            "arguments": json.dumps(_block(b, "input")),
                        },
                    }
                    for b in content
                    if _block(b, "type") == "tool_use"
                ]
                msg: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            else:
                for b in content:
                    if _block(b, "type") == "tool_result":
                        out.append(
                            {
                                "role": "tool",
                                "tool_call_id": b["tool_use_id"],
                                "content": b["content"],
                            }
                        )
                    elif _block(b, "type") == "text":
                        out.append({"role": "user", "content": b["text"]})
        return out

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Turn:
        oa_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
        # plain dicts in the documented shape; the SDK's TypedDicts add nothing at runtime
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=cast(Any, self._convert(system, messages)),
            tools=cast(Any, oa_tools),
        )
        msg = resp.choices[0].message
        calls = []
        for c in msg.tool_calls or []:
            fn = getattr(c, "function", None)  # only function tools are defined
            if fn is not None:
                try:
                    args = json.loads(fn.arguments or "{}")
                except json.JSONDecodeError:  # e.g. cut off at max_tokens; the tool says so
                    args = {"_invalid_arguments": True}
                calls.append(ToolCall(id=c.id, name=fn.name, input=args))
        content: list[Any] = []
        if msg.content:
            content.append({"type": "text", "text": msg.content})
        content += [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
        ]
        return Turn(
            text=msg.content or "",
            tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn",
            content=content,
        )

    def structured(self, system: str, user: str, schema: type[T]) -> T:
        """One request whose answer must match ``schema`` (structured outputs)."""
        resp = self.client.chat.completions.parse(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
        )
        msg = resp.choices[0].message
        if msg.parsed is None:
            reason = msg.refusal or resp.choices[0].finish_reason
            raise ValueError(f"no structured output ({reason})")
        return msg.parsed


# --------------------------------------------------------------------------- Azure OpenAI


class AzureOpenAIProvider(OpenAIProvider):
    """Azure OpenAI. Set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, and PDLWATCH_MODEL to your
    deployment name (AZURE_OPENAI_API_VERSION is optional)."""

    name = "azure"

    def __init__(self, settings: Settings | None = None) -> None:
        from openai import AzureOpenAI

        s = settings or get_settings()
        self.model = _require_model(s, "Azure OpenAI deployment")
        self.client = AzureOpenAI(
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
        )


# --------------------------------------------------------------------------- Mock


@dataclass
class _Route:
    pattern: str
    tool: str
    args: dict[str, Any]


# Keyword router: maps a question to the tool an analyst would reach for. Used for offline demos,
# tests and CI evals of the tooling (not of model quality). Order matters: first match wins.
_ROUTES: list[_Route] = [
    _Route(r"saved quer|which quer|what quer|list (the )?quer", "list_saved_queries", {}),
    _Route(r"brand.{0,25}(over|instead of|ahead of).{0,10}generic", "run_saved_query",
           {"name": "brand_over_generic"}),
    _Route(r"scorecard|most restrictive|least restrictive|preferred share by state",
           "run_saved_query", {"name": "state_scorecard"}),
    _Route(r"review|queue|backlog|not sure|unsure|low confidence|needs? a look", "review_queue", {}),
    _Route(r"accura|precision|recall|error rate|how (good|reliable|well)|extraction quality",
           "extraction_quality", {}),
    _Route(r"chang|\blost\b|moved|newly|added|removed|quarter|since the last", "pdl_changes", {}),
    _Route(r"documents?\b|effective date|which files|how many pages|scanned", "documents", {}),
    _Route(r"compare|\bvs\.?\b|versus", "compare_drugs", {}),
]  # fmt: skip

# Canned SQL so the mock exercises run_sql end to end when a question asks for SQL.
_SQL_TEMPLATES: list[tuple[str, str]] = [
    (r"status", "SELECT status, COUNT(*) AS rows FROM drug_access GROUP BY status ORDER BY rows DESC"),
    (r"class", "SELECT drug_class, COUNT(DISTINCT product) AS products FROM drug_access "
               "GROUP BY drug_class ORDER BY products DESC"),
    (r"state", "SELECT state, COUNT(*) AS products FROM drug_access GROUP BY state ORDER BY state"),
]  # fmt: skip

# two-letter codes that are also ordinary words or PDL shorthand ("PA" = prior authorization)
_AMBIGUOUS_CODES = {"PA", "OR", "IN", "ME", "OK", "HI", "DE", "MS", "ID", "AL", "LA", "MA", "CO"}


def _states(question: str) -> list[str]:
    from pdlwatch.synth.states import BY_NAME, STATES

    q = question.lower()
    found = [(q.find(n), c) for n, c in sorted(BY_NAME.items(), key=lambda kv: -len(kv[0]))
             if re.search(rf"\b{re.escape(n)}\b", q)]  # fmt: skip
    found += [(question.find(m.group(0)), m.group(0)) for m in re.finditer(r"\b[A-Z]{2}\b", question)
              if m.group(0) in STATES and m.group(0) not in _AMBIGUOUS_CODES]  # fmt: skip
    out: list[str] = []
    for _, code in sorted(found):
        if code not in out:
            out.append(code)
    # "West Virginia" also contains "Virginia"
    if "WV" in out and "VA" in out and "virginia" not in q.replace("west virginia", ""):
        out.remove("VA")
    return out


def _drugs(question: str) -> list[str]:
    from pdlwatch.drugs import PRODUCTS, find_products

    names = [p.name for p in find_products(question)]
    q = question.lower()
    for ing in sorted({p.ingredient for p in PRODUCTS}, key=len, reverse=True):
        if re.search(rf"\b{re.escape(ing)}\b", q) and not any(
            p.ingredient == ing for p in PRODUCTS if p.name in names
        ):
            names.append(ing)
    return names


def _drug_class(question: str) -> str | None:
    from pdlwatch.drugs import _class_index, normalise

    q = " " + normalise(question) + " "
    for key in sorted(_class_index(), key=len, reverse=True):
        if f" {key} " in q:
            return _class_index()[key]
    return None


def mock_route(question: str) -> tuple[str, dict[str, Any]]:
    """Deterministically choose a tool + arguments for a question."""
    q = question.lower()
    if "sql" in q:
        for pattern, sql in _SQL_TEMPLATES:
            if re.search(pattern, q):
                return "run_sql", {"sql": sql}
    states, drugs, cls = _states(question), _drugs(question), _drug_class(question)
    for r in _ROUTES:
        if not re.search(r.pattern, q):
            continue
        args: dict[str, Any] = dict(r.args)
        if r.tool == "compare_drugs":
            if len(drugs) < 2:
                continue
            args["drugs"] = drugs[:4]
        if r.tool in {"review_queue", "documents", "pdl_changes"} and states:
            args["state"] = states[0]
        if r.tool == "pdl_changes" and drugs:
            args["drug"] = drugs[0]
        return r.tool, args
    if states:
        args = {"state": states[0]}
        if cls:
            args["drug_class"] = cls
        return "state_formulary", args
    if drugs:
        return "drug_access", {"drug": drugs[0]}
    if cls:
        return "class_landscape", {"drug_class": cls}
    return "list_saved_queries", {}


class MockProvider:
    """Deterministic stand-in for an LLM: routes by keywords, then summarises tool output."""

    name = "mock"
    model = "mock-router-v1"

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Turn:
        last = messages[-1]
        if last["role"] == "user" and isinstance(last["content"], str):
            tool, args = mock_route(last["content"])
            call = ToolCall(id=f"mock_{len(messages)}", name=tool, input=args)
            content = [{"type": "tool_use", "id": call.id, "name": tool, "input": args}]
            return Turn(text="", tool_calls=[call], stop_reason="tool_use", content=content)
        # after tool results: produce a grounded summary of the first result
        result = next(
            b["content"]
            for b in last["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        )
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            payload = {"result": result}
        text = "Here is what the data shows (mock mode, no LLM call):\n\n" + _summarise(payload)
        return Turn(
            text=text,
            tool_calls=[],
            stop_reason="end_turn",
            content=[{"type": "text", "text": text}],
        )


def _summarise(payload: dict[str, Any]) -> str:
    if "error" in payload:
        return f"The tool returned an error: {payload['error']}"
    rows = payload.get("rows")
    if isinstance(rows, list) and rows:
        keys = list(rows[0].keys())
        head = "| " + " | ".join(keys) + " |\n|" + "---|" * len(keys) + "\n"
        body = "\n".join("| " + " | ".join(str(r.get(k)) for k in keys) + " |" for r in rows[:10])
        note = f"\n\n{payload['note']}" if payload.get("note") else ""
        return head + body + note
    return "```json\n" + json.dumps(payload, indent=2, default=str)[:2000] + "\n```"


# --------------------------------------------------------------------------- factory


def _has_anthropic_credentials() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_PROFILE")
    )


def get_provider(name: str | None = None, settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    name = (name or s.provider).lower()
    if name == "auto":
        name = "anthropic" if _has_anthropic_credentials() and s.model else "mock"
    if name == "anthropic":
        return AnthropicProvider(s)
    if name == "openai":
        return OpenAIProvider(s)
    if name == "azure":
        return AzureOpenAIProvider(s)
    if name == "mock":
        return MockProvider()
    raise ValueError(f"unknown provider {name!r} (anthropic | openai | azure | mock | auto)")

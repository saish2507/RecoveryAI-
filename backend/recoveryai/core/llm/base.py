"""Provider-agnostic LLM contract.

Everything above this layer — the agent, the governance wrapper, the tests —
talks in terms of `ToolSpec` in and `ToolCall` out. Nothing else. Adding an
enterprise provider is a new file implementing `LLMProvider`, not a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class LLMUnavailable(RuntimeError):
    """The model could not be consulted, for any reason at all.

    Deliberately one error type rather than a hierarchy: every caller does the
    same thing with it — fall back to the deterministic policy engine. The
    distinction that *does* matter (was budget consumed?) is carried by
    `budget_consumed`, not by the class.
    """

    def __init__(self, reason: str, *, budget_consumed: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.budget_consumed = budget_consumed


#: Exception type names that mean "the request never got a considered answer".
#:
#: Matched on the class name rather than the class itself because provider SDKs
#: own these types and importing them here would put a hard dependency on every
#: SDK in the provider-neutral layer. Names are checked across the whole MRO, so
#: an SDK subclass of a listed error still counts.
TRANSIENT_ERROR_NAMES = frozenset(
    {
        "TimeoutError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ConnectError",
        "ConnectionError",
        "ConnectionResetError",
        "RemoteProtocolError",
        "ReadError",
        "NetworkError",
        "ServiceUnavailable",
        "InternalServerError",
        "DeadlineExceeded",
        "ResourceExhausted",
        "Unavailable",
        "TooManyRequests",
    }
)

#: HTTP statuses worth a second attempt: the server said "not now", not "no".
TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def is_transient_error(exc: BaseException) -> bool:
    """Whether `exc` is worth exactly one more attempt.

    The distinction that matters is *transport failure* versus *considered
    refusal*. A timeout means the question never landed; a malformed response or
    a bad API key means it landed and the answer is not going to improve by
    asking again. Retrying the second kind burns budget and delays the fallback
    that was always going to happen.
    """
    names = {klass.__name__ for klass in type(exc).__mro__}
    if names & TRANSIENT_ERROR_NAMES:
        return True

    # httpx and most SDKs hang the status off a `response` attribute rather than
    # raising a distinctly-named class per status.
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    return status in TRANSIENT_STATUS_CODES


@dataclass(frozen=True)
class ToolSpec:
    """One callable action offered to the model, in provider-neutral form.

    `parameters` is a JSON-Schema object. Adapters translate this into whatever
    shape their SDK wants (Gemini `FunctionDeclaration`, OpenAI `tools`, ...).
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    """The model's answer: which tool, with what arguments.

    `reasoning` and `confidence` are pulled out of the arguments because every
    tool schema is required to carry them — that is how structured rationale
    comes back in the same call instead of costing a second turn.
    """

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    confidence: float = 0.5

    @classmethod
    def from_arguments(cls, name: str, arguments: dict[str, Any]) -> ToolCall:
        args = dict(arguments or {})
        reasoning = str(args.pop("reasoning", "") or "")
        raw_confidence = args.pop("confidence", 0.5)
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = min(1.0, max(0.0, confidence))
        return cls(name=name, arguments=args, reasoning=reasoning, confidence=confidence)


@runtime_checkable
class LLMProvider(Protocol):
    """What the agent needs from a model. Two methods, no SDK types."""

    name: str

    def is_configured(self) -> bool:
        """False → the agent skips the network entirely and runs deterministically."""
        ...

    def choose_tool(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpec],
        timeout_seconds: float,
    ) -> ToolCall:
        """Force a function call from `tools` and return it.

        Implementations MUST raise `LLMUnavailable` — never return `None`, never
        leak a provider SDK exception — so that callers have exactly one failure
        path to handle.
        """
        ...

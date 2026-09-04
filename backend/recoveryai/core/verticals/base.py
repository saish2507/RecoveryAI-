"""What a "specialist" is, in one dataclass.

The previous build had three "agents" that were really `if/elif` branches over a
shared function. A vertical is not code — it is *configuration*: a system prompt,
an action palette, a guardrail, a fallback chain and an urgency heuristic. One
agent loop reads that config and behaves like the specialist it describes.

Adding a fourth vertical (subscription dunning, say) is therefore a new config
file plus a registry line, with no change to the agent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from recoveryai.core.llm.base import ToolSpec
from recoveryai.core.models import RecoveryEvent
from recoveryai.core.policy import GuardrailCapacity, GuardrailResult
from recoveryai.core.tools import build_tools

#: Appended to every specialist's prompt, so the response contract is stated once
#: rather than drifting between three copies.
#:
#: These sentences exist to close the two ways a reply becomes unusable and costs
#: the case its decision: answering in prose instead of calling a tool, and
#: naming a tool that was not offered. Both are recorded as `fallback_schema` and
#: fall through to the policy tables, so a vague contract does not produce a
#: worse decision — it produces one the agent did not make.
RESPONSE_CONTRACT = """\

Answering:
- Reply **only** by calling exactly one of the tools provided. Do not answer in prose, \
do not explain first and call afterwards, and do not call more than one.
- Choose only from the tools you were given in this request. Tools absent from that list \
were withheld because a guardrail would refuse them; naming one anyway does not \
unlock it, and costs this case its decision.
- `reasoning` and `confidence` are required on every call. Put your explanation in \
`reasoning`, not in a message.
- If the signals genuinely do not resolve, that is not a reason to skip the call — \
choose the safest available action and set a low `confidence`. A low-confidence \
decision routes to a human, which is a working outcome; no decision is not."""


@dataclass(frozen=True)
class VerticalConfig:
    name: str
    display_name: str
    system_prompt: str

    #: Actions this specialist may ever propose, in rough preference order.
    tool_palette: tuple[str, ...]

    #: `(action_name, capacity, diagnosis) -> (allowed, reason)`.
    guardrail: Callable[[str, GuardrailCapacity, str], GuardrailResult]

    #: Tried in order when the proposed action is blocked. The last entry must be
    #: unconditionally allowed, or a blocked case could have nowhere to land.
    fallback_chain: tuple[str, ...]

    #: `(diagnosis, confidence, reasoning)` from cheap rules only.
    diagnoser: Callable[[RecoveryEvent], tuple[str, float, str]]

    #: Diagnoses that must never be handled autonomously, whatever the model says.
    forced_escalation_diagnoses: frozenset[str] = field(default_factory=frozenset)

    #: How many decisions this specialist may take before handing the case over.
    #:
    #: Per vertical rather than global because the levers differ in how many
    #: attempts they can justify. A recurring mandate either re-presents
    #: successfully or the instrument is genuinely broken, and a third attempt
    #: mostly buys bank fees; an abandoned cart tolerates a couple of reminders
    #: but turns into harassment well before six. A single global cap set for the
    #: most patient vertical silently licenses that behaviour everywhere.
    max_steps: int = 6

    @property
    def prompt(self) -> str:
        """The specialist's brief plus the shared response contract.

        Composed rather than stored so the contract cannot go stale in one
        vertical's copy while the others move on.
        """
        return self.system_prompt + RESPONSE_CONTRACT

    def tools(self, allowed: list[str] | tuple[str, ...] | None = None) -> list[ToolSpec]:
        names = tuple(allowed) if allowed is not None else self.tool_palette
        return build_tools([n for n in self.tool_palette if n in set(names)])

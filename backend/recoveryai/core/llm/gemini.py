"""Gemini adapter.

The only file in the codebase that knows Gemini exists. Note what it does *not*
do: no rate limiting, no budget, no caching, no fallback logic. Those live in
`governance.py` and apply to every provider equally. To add an enterprise model,
copy this file's shape — `is_configured` + `choose_tool` — and nothing else
changes.

The SDK import is deliberately lazy so the package imports (and the whole test
suite runs) without `google-generativeai` installed.
"""

from __future__ import annotations

import logging
from typing import Any

from recoveryai.core.llm.base import LLMUnavailable, ToolCall, ToolSpec

logger = logging.getLogger(__name__)


class GeminiProvider:
    """Native function-calling against Gemini, forced into tool mode.

    `mode="ANY"` means the model must emit a function call — it is never allowed
    to reply with free text we would then have to parse heuristically.
    """

    name = "gemini"

    def __init__(self, api_key: str = "", model: str = "gemini-2.5-pro") -> None:
        self.api_key = (api_key or "").strip()
        self.model = model

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _build_model(self, tools: list[ToolSpec], system_prompt: str) -> Any:
        import google.generativeai as genai  # noqa: PLC0415 — lazy on purpose

        genai.configure(api_key=self.api_key)
        declarations = [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in tools
        ]
        return genai.GenerativeModel(
            model_name=self.model,
            system_instruction=system_prompt,
            tools=[{"function_declarations": declarations}],
            tool_config={"function_calling_config": {"mode": "ANY"}},
        )

    def choose_tool(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpec],
        timeout_seconds: float = 30.0,
    ) -> ToolCall:
        if not self.is_configured():
            raise LLMUnavailable("gemini_api_key_missing")
        if not tools:
            raise LLMUnavailable("no_tools_offered")

        try:
            model = self._build_model(tools, system_prompt)
            response = model.generate_content(
                user_prompt,
                request_options={"timeout": timeout_seconds},
            )
        except ImportError as exc:
            raise LLMUnavailable("google-generativeai_not_installed") from exc
        except Exception as exc:
            # The provider's own message, not just the exception class. Recording
            # only `NotFound` cost real debugging time on a 404 whose body said
            # exactly what was wrong and which model to use instead — and because
            # the fallback path is silent by design, the trace read "LLM
            # unavailable" for days without anyone learning why.
            detail = " ".join(str(exc).split())[:200]
            logger.warning(
                "gemini call failed",
                extra={"model": self.model, "error": type(exc).__name__, "detail": detail},
            )
            raise LLMUnavailable(f"gemini_call_failed: {type(exc).__name__}: {detail}") from exc

        call = self._extract_function_call(response)
        if call is None:
            # Forced tool mode should make this unreachable; if the provider
            # returns prose anyway, that is a provider failure, not something to
            # salvage by parsing text.
            raise LLMUnavailable("gemini_returned_no_function_call")

        name, args = call
        offered = {t.name for t in tools}
        if name not in offered:
            # The model invented a tool, or picked one we pruned for guardrail
            # reasons. Either way we do not honour it.
            raise LLMUnavailable(f"gemini_chose_unoffered_tool: {name}")

        return ToolCall.from_arguments(name, args)

    @staticmethod
    def _extract_function_call(response: Any) -> tuple[str, dict[str, Any]] | None:
        """Dig the function call out of Gemini's candidate/part structure."""
        try:
            candidates = getattr(response, "candidates", None) or []
            for candidate in candidates:
                content = getattr(candidate, "content", None)
                for part in getattr(content, "parts", None) or []:
                    fc = getattr(part, "function_call", None)
                    if fc is None or not getattr(fc, "name", None):
                        continue
                    raw_args = getattr(fc, "args", None) or {}
                    return str(fc.name), {str(k): v for k, v in dict(raw_args).items()}
        except Exception:  # malformed response shape → treat as no call
            return None
        return None

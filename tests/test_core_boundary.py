"""The library-usability guarantee, enforced rather than documented.

`recoveryai.core` must be importable by a host fintech's backend that has never
heard of FastAPI — including one running Django, Flask, or no web framework at
all. A stray `from fastapi import ...` in a core module would make the whole
package undroppable, and it is the kind of import that gets added by accident
during a refactor and noticed six months later by an integrator.

So: import every core module in a subprocess and assert the framework never
shows up in `sys.modules`.
"""

from __future__ import annotations

import pkgutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
CORE = BACKEND / "recoveryai" / "core"

FORBIDDEN = ("fastapi", "starlette", "uvicorn")


def _core_modules() -> list[str]:
    names = ["recoveryai.core"]
    for info in pkgutil.walk_packages([str(CORE)], prefix="recoveryai.core."):
        names.append(info.name)
    return sorted(names)


def test_core_modules_discovered() -> None:
    """Guards the guard: if the walk finds nothing, the real test proves nothing."""
    modules = _core_modules()
    assert "recoveryai.core.agent" in modules
    assert "recoveryai.core.llm.gemini" in modules
    assert len(modules) >= 10


def test_core_has_no_web_framework_in_import_graph() -> None:
    modules = _core_modules()
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(BACKEND)!r})
        for name in {modules!r}:
            __import__(name)
        leaked = sorted(
            m for m in sys.modules
            if any(m == f or m.startswith(f + ".") for f in {FORBIDDEN!r})
        )
        print("LEAKED:" + ",".join(leaked))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, f"core failed to import standalone:\n{proc.stderr}"

    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("LEAKED:")][0]
    leaked = [m for m in line.removeprefix("LEAKED:").split(",") if m]
    assert not leaked, (
        f"recoveryai.core pulled in a web framework: {leaked}. "
        "Core must stay embeddable — move the offending import into recoveryai.api."
    )


@pytest.mark.parametrize("forbidden", FORBIDDEN)
def test_no_core_source_file_mentions_web_framework(forbidden: str) -> None:
    """Catches an import hidden inside a function, which the import-graph test misses."""
    offenders = []
    for path in CORE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            if forbidden in stripped:
                offenders.append(f"{path.relative_to(BACKEND)}:{lineno}: {stripped}")
    assert not offenders, f"{forbidden} imported inside core:\n" + "\n".join(offenders)


def test_agent_is_constructible_without_web_framework() -> None:
    """The actual promise: a host can build and drive the agent as a plain object."""
    script = textwrap.dedent(
        f"""
        import sys, tempfile, os
        sys.path.insert(0, {str(BACKEND)!r})
        os.environ["RECOVERYAI_ENV_FILE"] = os.path.join(tempfile.gettempdir(), "no.env")
        from recoveryai.core.agent import RecoveryAgent
        from recoveryai.core.settings import Settings
        from recoveryai.core.execution import SimulatedExecutor

        tmp = tempfile.mkdtemp()
        s = Settings(gemini_api_key="", database_url="sqlite:///" + os.path.join(tmp, "x.db"))
        agent = RecoveryAgent(settings=s, executor=SimulatedExecutor())
        assert agent.status_snapshot()["executor"] == "simulated"
        print("OK")
        """
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout

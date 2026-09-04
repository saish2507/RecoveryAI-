"""Agent core — a plain Python package with no web-framework dependency.

A host application can `from recoveryai.core.agent import RecoveryAgent` and drive
the whole system in-process. Nothing in this subpackage may import FastAPI,
Starlette or uvicorn; `tests/test_core_boundary.py` enforces that.
"""

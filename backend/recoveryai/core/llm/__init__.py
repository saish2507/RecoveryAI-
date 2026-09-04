"""LLM provider adapters and governance.

`base` defines the contract, `gemini` implements it, `governance` wraps any
implementation with rate/cost/cache control. Adding a provider means adding one
file alongside `gemini`.
"""

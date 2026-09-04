"""HTTP transport layer.

The only subpackage permitted to import FastAPI. Everything it exposes is a thin
wrapper over `recoveryai.core`; a host that embeds the agent directly never
imports anything from here.
"""

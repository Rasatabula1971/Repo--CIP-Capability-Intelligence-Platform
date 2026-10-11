"""
PDR-to-Build domain logic (Implementation Spec v1.1).

Pure functions only. No DB, no IO, no model calls. The orchestrator in
mcp_server.pdr_queries loads facts, calls these, and persists results —
the same split Step 12 uses between core.project and workers.recommend.
"""

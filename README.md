# fusion-spatial-mcp

An MCP server that gives AI agents **genuine 3D spatial understanding of
Autodesk Fusion 360** — exact measurements, collision/clearance/containment,
volumetric occupancy, section cuts, engineering multiviews, and free-space
placement search — plus parametric creation tools.

This is the Fusion implementation of the spatial architecture proven in the
sibling project [`rhino-gh-mcp`](../rhino-gh-mcp) (same machine): a
platform-neutral `spatial-core` engine where each CAD platform only has to
provide two adapter functions (`bodies()` + `tessellate()`), validated by a
checked-in JSON Schema conformance suite.

**Status: planning.** The build plan is in
[docs/BUILD_PLAN.md](docs/BUILD_PLAN.md). Architecture in one line:

```
Claude ──stdio── Node MCP server ──TCP 8767── Fusion add-in (Python, CustomEvent → main thread)
                     └── spatial-core (shared with rhino-gh-mcp)
```

## Layout

- `docs/BUILD_PLAN.md` — the full phased build plan (self-contained)
- `contracts/` — JSON Schema wire contracts for the adapter commands
- `tools/validate-protocol.mjs` — adapter conformance suite (the F2 gate)
- `src/` — Node MCP server (TypeScript) *(to be built)*
- `addin/` — Fusion 360 add-in (Python) *(to be built)*

MIT licensed.

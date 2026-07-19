# fusion-spatial-mcp

An MCP server that gives AI agents **genuine 3D spatial understanding of
Autodesk Fusion 360** — exact measurements, collision/clearance/containment,
volumetric occupancy, section cuts, engineering multiviews, and free-space
placement search — plus **Fusion-native parametric authoring**: constrained
sketches, timeline features, and named user parameters, aimed at producing
clean, human-editable parametric models.

The design is Fusion-first: tools are built around Fusion's own concepts
(feature timeline, sketch constraints, user parameters, components), not
adapted from any other CAD paradigm. Under the hood, the geometry-reading
layer reuses the platform-neutral `spatial-core` engine (developed in
[`rhino-gh-mcp`](../rhino-gh-mcp) on this machine): a CAD platform only has
to provide two adapter functions (`bodies()` + `tessellate()`), validated by
a checked-in JSON Schema conformance suite — spatial understanding of B-Rep
solids is the same no matter how the geometry was authored.

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

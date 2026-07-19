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

```
Claude ──stdio── Node MCP server ──TCP 127.0.0.1:8767── Fusion add-in (Python)
                     └── spatial-core                        └── CustomEvent → main thread
```

**Status:** working end to end. Adapter conformance (F2) passed; six-question
spatial benchmark 6/6 (F3); parametric-bracket authoring acceptance passed
(F4): constrained sketch → extrude → hole → 2×2 pattern → fillet, six named
user parameters, resized purely via parameter driving, dimensions verified
numerically by the spatial tools.

## Setup

1. **Build the server** (Node 22+; the sibling `rhino-gh-mcp` repo must be
   present for the `spatial-core` file: dependency):

   ```
   npm install
   npm run build
   ```

2. **Install the add-in** — link (or copy) `addin/FusionSpatialMCP` into
   Fusion's dev add-ins folder:

   ```powershell
   New-Item -ItemType Junction `
     -Path "$env:APPDATA\Autodesk\Autodesk Fusion 360\API\AddIns\FusionSpatialMCP" `
     -Target "<repo>\addin\FusionSpatialMCP"
   ```

3. **Run the add-in**: in Fusion press **Shift+S** → *Add-Ins* tab →
   **FusionSpatialMCP** → **Run**. The Text Commands palette logs
   `listener running on 127.0.0.1:8767`. Re-run after editing the add-in
   file. (Check *Run on Startup* to make it permanent.)

4. **Register the MCP server** with your client, e.g. for Claude Code:

   ```
   claude mcp add fusion -- node <repo>/dist/index.js
   ```

## Using it (agent workflow)

- **Orient first:** `fusion_document` — document, units, up-axis, and the
  feature timeline (in Fusion, the timeline IS the scene graph).
- **Any metric question** (size, position, distance, clearance, hollowness,
  wall thickness): use the `space_*` tools, never screenshots — they return
  exact numbers. `space_digest` inventories every body with kernel-exact
  volume/area/centroid; bodies get short handles (`b1`, `b2`, …).
- **Authoring is parametric by default.** Every dimension input accepts a
  number (doc units), an expression (`"40 mm"`, `"width/2"`), or
  `{param: "wall_height", value: "40 mm"}` which creates a named user
  parameter on the fly. `fusion_create_sketch` takes entities + dimensions +
  constraints in one call and reports `fullyConstrained`;
  `fusion_add_feature` appends ONE timeline feature per call and returns a
  health report. **Editing = driving parameters**: `fusion_set_parameters`
  (batch, one recompute) or `fusion_edit_feature` (a feature's dimensions by
  role). `fusion_timeline` / `fusion_rollback` for history introspection and
  mid-history edits. `fusion_execute_api_script` is the escape hatch
  (`adsk`/`app`/`ui` preloaded, assign to `result`).
- Quality bar: *would a mechanical designer be happy to inherit this
  timeline?* Named features, named parameters, fully-constrained sketches.

## Tool surface

| Tool | Purpose |
|---|---|
| `fusion_document` | Orientation: doc, units, up-axis, timeline summary |
| `fusion_get_selection` | What the user has selected ("this face/body") |
| `fusion_capture_viewport` | Viewport PNG (appearance; not for metrics) |
| `fusion_list_parameters` / `fusion_set_parameters` | Read / drive user + model parameters (batch, health report) |
| `fusion_create_sketch` | Sketch with entities + dimensions + constraints, returns profile ids |
| `fusion_add_feature` | One timeline feature: extrude, revolve, hole, fillet, chamfer, shell, patterns, mirror, combine |
| `fusion_edit_feature` | Edit a feature in place (parameters by role, suppress, rename) |
| `fusion_timeline` / `fusion_rollback` | Full history introspection / marker moves |
| `fusion_execute_api_script` | Python escape hatch on the main thread |
| `space_digest` | Metric inventory of all bodies |
| `space_measure` | distance / bbox / dims / point-probe |
| `space_relations` | Pairwise clearance, intersection, containment |
| `space_voxels` | ASCII occupancy layers (hollowness, mass distribution) |
| `space_section` | Planar cut: loops, areas, wall thickness |
| `space_views` | Labeled 4-tile orthographic PNG + legend |
| `space_fit` | Free-space placement search |
| `space_pick` | Identify the body under a `space_views` pixel |

## Development

- `node tools/call.mjs <method> [json | @file.py]` — raw wire calls
  (`ping`, `fusion.document`, `fusion.execute @script.py`, …)
- `npm run conformance` — adapter conformance suite (JSON Schema contracts
  in `contracts/`); must print `CONFORMANT`
- `node tools/f2-checks.mjs` — volumes vs. Fusion physical properties +
  world-space proxy tessellation (needs the `tools/f2-scene.py` scene)
- `node tools/f3-benchmark.mjs` — six-question spatial benchmark (≥5/6)
- `node tools/f4-bracket.mjs` — parametric bracket authoring acceptance
- `docs/BUILD_PLAN.md` — the phased build plan with the Fusion API traps
  (internal units are ALWAYS cm; face sketches flip normals; API rectangles
  have no constraints; …)

MIT licensed.

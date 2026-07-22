# fusion-spatial-mcp — Build Plan

> **Historical document.** This plan was written before milestone F6 and is
> kept as the project's design record. F6 has since happened: `spatial-core`
> was extracted into its own public repo (sibling checkout `../spatial-core`,
> https://github.com/aphollis/spatial-core), which is now the canonical home
> of PROTOCOL.md, `contracts/`, and the conformance suite. Sibling-repo
> references below are written relative to this repo's parent directory.

**Goal:** a Fusion 360 MCP server, designed Fusion-native, with the full
spatial reasoning harness.

**Framing rule for this project:** Fusion is NOT "Rhino with a different
API." Grasshopper authoring builds a live dataflow graph; Fusion authoring
appends to a parametric feature timeline driven by named parameters and
constrained sketches. Different verbs, different artifact, different editing
model. Therefore:

- The **geometry-reading layer is shared** — `spatial-core`, the wire
  protocol, and the conformance suite operate on B-Rep solids and are
  modality-blind. Consume them as infrastructure.
- Everything **above** that line — creation tools, tool vocabulary, agent
  guidance, workflows — is designed from Fusion's own concepts (timeline,
  features, sketches + constraints, user parameters, components/occurrences).
  Do not port Grasshopper concepts, names, or workflow shapes. When the
  sibling repo is referenced below it is as an *infrastructure donor and
  engineering-pattern source* (threading, TCP bridging, token efficiency),
  never as a design template.

This plan is written to be executed by an agent session with no prior
context. Read the referenced files before implementing each phase.

---

## 1. Shared infrastructure that already exists (do not rebuild)

| Asset | Location | Role here |
|---|---|---|
| `spatial-core` package | `../spatial-core/` (own repo since F6) | ALL spatial intelligence: BVH distance/containment/collision, voxels, sections, PNG ortho multiview, free-space fit, pixel pick. Platform-neutral by design; consume it, never fork it. |
| Protocol contract | `../spatial-core/PROTOCOL.md` (canonical) | The wire protocol + TS API this project must satisfy. §1 defines the exact two commands the Fusion add-in must implement. |
| JSON Schema contracts | `../spatial-core/contracts/*.schema.json` (canonical; consumed from the package) | Adapter conformance shapes for `space.bodies` / `space.tessellate`. |
| Conformance suite | `../spatial-core/tools/validate-protocol.mjs` (run via `npm run conformance`) | Run against the Fusion add-in's listener; it must print CONFORMANT. This is the acceptance gate for Phase F2. Needs `npm i -D ajv`. |
| Engineering patterns (MCP server) | `../rhino-gh-mcp/src/` | `bridge.ts` (TCP JSON-lines client), `spatial-adapter.ts` (base64 → typed arrays), `index.ts` spatial-tool section + annotation style. Reuse the *mechanics*; the Fusion tool vocabulary is designed fresh in §4. |
| Engineering patterns (in-app bridge) | `../rhino-gh-mcp/rhino/mcp_listener.py` | JSON-lines TCP server, thread-per-connection, main-thread marshaling, sceneVersion in dispatcher, `space.*` handlers. Same mechanics apply; the command surface beyond `space.*` is Fusion's own. |
| Ecosystem survey | `../rhino-gh-mcp/docs/community-mcp-survey.md` | Prior art. Key: Autodesk's MIT **FusionMCPSample** (CustomEvent threading to copy), ndoo/fusion360-mcp-bridge (best threading docs + Bearer auth), faust-machines (84-tool reference, MIT). Do NOT copy GPL (Joe-Spencer) or unlicensed code. |
| Design rationale | `../rhino-gh-mcp/docs/RFD-001-spatial-reasoning.md` | Why the adapter is only two functions; §8b has Fusion-specific notes. |
| Token-efficiency playbook | `../rhino-gh-mcp/docs/EFFICIENCY_PLAN.md` | Apply the same rules here from day one (alwaysLoad, cached static context, terse returns, handles, idempotency). |

**spatial-core consumption:** npm `file:` dependency on `../spatial-core`
(sibling checkouts). Milestone F6 extracted spatial-core into its own repo,
which is the canonical home of the protocol and contracts. Do not copy the
source.

---

## 2. Target architecture

```
Claude (MCP client, stdio)
   └── Node MCP server (this repo, TypeScript)          [port of rhino src/]
         ├── spatial-core  (file:../rhino-gh-mcp/spatial)  — unchanged
         ├── FusionGeometryAdapter (new, ~60 lines)        — same shape as
         │      RhinoGeometryAdapter but talking to the Fusion add-in
         └── TCP 127.0.0.1:8767, newline-delimited JSON
                └── Fusion 360 Add-in (Python, stdlib only)   [new]
                      background socket thread
                        → adsk.core CustomEvent (fire to main thread)
                        → handler runs Fusion API on main thread
                        → threading.Event releases the socket thread
```

**Port 8767.** (Rhino listener owns 8765, chat agent 8766; the survey shows
9876/7654/3000/1999 taken by other community servers.)

**Threading is THE critical pattern.** Fusion's API must run on its main
thread. Every community server converges on: socket thread parks a request,
`app.fireCustomEvent(EVENT_ID, payload)`, registered CustomEventHandler
executes on the main thread, result stored, `threading.Event.set()` releases
the socket thread to respond. Copy the mechanism from Autodesk's MIT
FusionMCPSample (see survey §1.1). Mirror the Rhino listener's outer behavior:
thread-per-connection, JSON lines, `{"id", "method", "params"}` →
`{"id", "result"| "error":{message, traceback}}`, NO SO_REUSEADDR (zombie
sockets must fail bind loudly — hard-won lesson).

---

## 3. Fusion-specific traps (all verified in survey/RFD; do not rediscover)

1. **Units:** the Fusion API is ALWAYS internal centimeters, regardless of
   document display units. The add-in must convert every length/area/volume
   to the design's default length unit
   (`design.unitsManager.defaultLengthUnits`) before serializing, and report
   that unit string in `space.bodies.units`. Write `units.py` with the
   conversion helpers and unit-test the cm→mm/in paths mentally against known
   bodies before trusting anything downstream.
2. **Up-axis:** Fusion documents are commonly Y-up (configurable). Report the
   ACTUAL up axis in `space.bodies.upAxis` ("y" or "z") — spatial-core
   carries it through; do not silently assume.
3. **Entity ids:** use `entityToken` (persistent string) as the body id, not
   names or indices. Tokens survive timeline recompute; document that they can
   go stale after destructive edits — resolve defensively.
4. **Assemblies:** bodies live under occurrences with transforms. v1 policy:
   enumerate `rootComponent.allOccurrences` + root bodies, and serialize
   world-space geometry (`body.boundingBox` is already world-aligned;
   tessellation via `meshManager` yields occurrence-local coords for proxies —
   verify and apply occurrence transform if needed; write a two-occurrence
   test case early).
5. **Tessellation:** `body.meshManager.createMeshCalculator()`;
   `setQuality(...)`; `calculate()` → `nodeCoordinates` (Point3D list, cm) and
   `nodeIndices`. Pack with Python `array('f')`/`array('i')` + `base64` — no
   .NET here, plain CPython (Fusion bundles its own CPython 3).
6. **Add-in lifecycle:** dev add-ins live at
   `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\<Name>\` with
   `<Name>.py` + `<Name>.manifest`. The user has built add-ins before (their
   `FusionRhinoBar` project and an existing "Sheeter" add-in) — reuse their
   conventions if visible. `run(context)` / `stop(context)` entry points;
   clean up sockets and event handlers in `stop()` or reloads leak.
7. **No UI blocking:** long tessellations of huge assemblies will freeze the
   UI during the main-thread slice. Acceptable (Rhino behaves the same), but
   keep per-request work bounded; never loop the socket thread into the API.

---

## 4. Tool surface

### The Fusion mental model (drives every tool decision)

In Fusion, the design **is** the timeline: an ordered history of features
(sketch → extrude → hole → fillet → pattern ...) whose dimensions are driven
by named, unit-aware **user parameters** with expressions (`width/2 + 3 mm`),
organized into **components** joined in assemblies. A good Fusion agent does
not just produce geometry — it authors a *clean, human-editable parametric
model*: well-named parameters, constrained sketches, sensible feature order.
Editing means driving parameters or revising features in place and letting
the timeline recompute — not rebuilding. The agent's quality bar is "would a
mechanical designer be happy to inherit this timeline?"

### Phase A — spatial + read-only understanding (ship first)
The `space_*` toolset comes from spatial-core through the adapter with zero
new geometry code — spatial understanding of solids is modality-independent:
`space_digest, space_measure, space_relations, space_voxels, space_section,
space_views, space_fit, space_pick`.
Fusion-native read-only tools:
- `fusion_document` — doc name, units, up-axis, components, body counts, and
  a **timeline summary** (feature list with names/types/health/suppression) —
  the agent's primary orientation call; the timeline IS the scene graph here.
- `fusion_get_selection` — entityTokens + kinds + bboxes of
  `ui.activeSelections` (resolves "this face/body/feature").
- `fusion_capture_viewport` — `app.activeViewport.saveAsImageFile` → PNG.

### Phase B — parametric authoring (Fusion-native by design)
- `fusion_list_parameters` / `fusion_set_parameters` — read and drive user +
  model parameters by name, with unit-aware values AND expressions. Driving
  parameters is the primary editing verb in Fusion; setting several at once
  (batch) with a single recompute + health report.
- `fusion_create_sketch` — create a sketch on a plane/face (by entityToken)
  with entities (lines/arcs/circles/rects/points) plus **dimensions and
  constraints** — constraints are what make a sketch design-intent rather
  than dumb curves; the tool should encourage fully-defined sketches and
  report the under-constrained count back.
- `fusion_add_feature` — one timeline feature per call: extrude, revolve,
  hole, fillet, chamfer, shell, rectangular/circular pattern, mirror, combine.
  References by entityToken (profiles, faces, edges); dimension inputs accept
  parameter expressions, and the tool can create named user parameters
  on the fly (`{"distance": {"param": "wall_height", "value": "40 mm"}}`) so
  models come out parametric by default.
- `fusion_edit_feature` — modify an existing timeline feature's inputs in
  place (the Fusion-native "change it" — recompute + downstream health).
- `fusion_timeline` — full introspection: features in order, their consumed
  parameters/references, errors/warnings after recompute, rollback marker;
  plus `fusion_rollback` to move the marker for mid-history edits.
- `fusion_execute_api_script` — the escape hatch (`adsk` preloaded, stdout
  captured, `result` returned; destructiveHint). Present because every gap
  needs a relief valve — not the centerpiece.
- sceneVersion bumps in the dispatcher on every mutating method.

Deliberately NOT in scope v1: canvas-style batch "build a whole design in one
call". Fusion's timeline semantics make stepwise feature authoring with
health checks after each step the more faithful (and debuggable) shape; a
batch layer can be added later if turn-count metrics justify it.

### Phase C — later
Assemblies (occurrences, joints, joint limits), appearance/materials, drawing
export, an in-Fusion chat palette (HTML palette + local agent backend),
`fusion_undo`.

All tools carry MCP annotations (readOnlyHint/destructiveHint/idempotentHint)
from day one, and register with `alwaysLoad` semantics in any agent backend.

---

## 5. Milestones & acceptance criteria

| # | Deliverable | Acceptance |
|---|---|---|
| F0 | Repo scaffold: package.json (file: dep on ../rhino-gh-mcp/spatial, ajv devDep), tsconfig, src/bridge.ts + src/index.ts skeleton compiling; add-in skeleton that loads in Fusion and logs to the TEXT COMMANDS palette | `npm run build` clean; add-in appears in Fusion Scripts & Add-Ins and survives run/stop cycles |
| F1 | Listener online: TCP 8767, ping, `fusion.scene` info, CustomEvent main-thread marshaling proven | `ping` → `pong` from a node script; scene info correct with a doc open |
| F2 | **Adapter conformance:** `space.bodies` + `space.tessellate` with units/up-axis/entityToken/assembly handling | `node tools/validate-protocol.mjs` (RHINO_MCP_PORT=8767) prints CONFORMANT against a test doc incl. one assembly with 2 occurrences; volumes match Fusion's own physical properties to 4 sig figs |
| F3 | Full Phase-A tool surface wired in the MCP server | Spatial benchmark: create the shared ground-truth scene (two spheres, hollow box with 2-unit walls, tall cylinder — via an execute script), run the six spatial questions through a client; target ≥5/6 |
| F4 | Phase-B authoring tools + sceneVersion | Author a parametric bracket as a clean timeline (constrained sketch → extrude → hole pattern → fillet) with named user parameters; then resize it purely via `fusion_set_parameters`; spatial tools verify dimensions numerically; timeline health clean |
| F5 | Docs, README, annotations audit, error-message polish | A cold Claude Code session with only this repo's README + MCP registration can drive Fusion successfully |
| F6 | (Optional) Extract spatial-core to its own repo consumed by both projects | Both repos build against the extracted package |

Order F0→F5 strictly; F2's conformance gate is what keeps this cheap — do not
write any spatial tool code before it passes.

---

## 6. Working agreements (inherited from the Rhino project)

- **Never run generated code against the user's live Fusion session without
  asking** — same standing rule as Rhino. Compile/lint offline; batch live
  tests into agreed steps. Fusion has no headless mode; every live test
  touches the user's real app.
- Validate Python by running `py_compile` with any CPython 3.9+ —
  Fusion's bundled interpreter is 3.x; keep the add-in ≥3.9-compatible.
  (Machine-specific tooling paths live in session memory, not here.)
- Git identity: `aphollis <20403818+aphollis@users.noreply.github.com>`
  (email privacy on). git/gh/Node assumed on PATH via portable installs;
  no system Python. winget hangs on UAC — use portable installs.
- Commit per milestone; benchmark before/after any efficiency claim.
- MIT license (file present). Borrow only from MIT sources named in the
  survey; credit in comments where nontrivial patterns are copied.

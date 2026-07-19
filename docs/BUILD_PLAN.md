# fusion-spatial-mcp — Build Plan

**Goal:** a Fusion 360 MCP server with the full spatial reasoning harness,
porting the proven architecture of `C:\Users\nerfd\rhino-gh-mcp` (the sibling
project). The spatial layer is REUSED, not rewritten: this project implements
a thin platform adapter and Fusion-native creation tools.

This plan is written to be executed by an agent session with no prior context.
Read the referenced files before implementing each phase. The Rhino repo on
this machine is the working reference implementation of everything described.

---

## 1. What already exists (do not rebuild)

| Asset | Location | Role here |
|---|---|---|
| `spatial-core` package | `C:\Users\nerfd\rhino-gh-mcp\spatial\` | ALL spatial intelligence: BVH distance/containment/collision, voxels, sections, PNG ortho multiview, free-space fit, pixel pick. Platform-neutral by design; consume it, never fork it. |
| Protocol contract | `C:\Users\nerfd\rhino-gh-mcp\spatial\PROTOCOL.md` | The wire protocol + TS API this project must satisfy. §1 defines the exact two commands the Fusion add-in must implement. |
| JSON Schema contracts | `contracts/*.schema.json` (copied into THIS repo, canonical copy in rhino-gh-mcp) | Adapter conformance shapes for `space.bodies` / `space.tessellate`. |
| Conformance suite | `tools/validate-protocol.mjs` (copied here) | Run against the Fusion add-in's listener; it must print CONFORMANT. This is the acceptance gate for Phase F2. Needs `npm i -D ajv`. |
| Reference MCP server | `C:\Users\nerfd\rhino-gh-mcp\src\` | `index.ts` (tool registration patterns, annotations, spatial section), `bridge.ts` (TCP JSON-lines client), `spatial-adapter.ts` (base64 → typed arrays). Port these shapes. |
| Reference listener | `C:\Users\nerfd\rhino-gh-mcp\rhino\mcp_listener.py` | The in-app bridge pattern: JSON-lines TCP server, thread-per-connection, UI-thread marshaling, sceneVersion in dispatcher, `space.*` handlers. |
| Ecosystem survey | `C:\Users\nerfd\rhino-gh-mcp\docs\community-mcp-survey.md` | Prior art. Key: Autodesk's MIT **FusionMCPSample** (CustomEvent threading to copy), ndoo/fusion360-mcp-bridge (best threading docs + Bearer auth), faust-machines (84-tool reference, MIT). Do NOT copy GPL (Joe-Spencer) or unlicensed code. |
| Design rationale | `C:\Users\nerfd\rhino-gh-mcp\docs\RFD-001-spatial-reasoning.md` | Why the adapter is only two functions; §8b has Fusion-specific notes. |
| Token-efficiency playbook | `C:\Users\nerfd\rhino-gh-mcp\docs\EFFICIENCY_PLAN.md` | Apply the same rules here from day one (alwaysLoad, cached static context, terse returns, handles, idempotency). |

**spatial-core consumption:** npm `file:` dependency on
`../rhino-gh-mcp/spatial` (works on this machine; both repos are siblings
under `C:\Users\nerfd\`). Milestone F6 extracts spatial-core into its own repo
when this dependency becomes annoying. Do not copy the source.

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

### Phase A — spatial + read-only (the differentiator; ship first)
Identical names and schemas to the Rhino server (port from
`rhino-gh-mcp/src/index.ts`, spatial section):
`space_digest, space_measure, space_relations, space_voxels, space_section,
space_views, space_fit, space_pick` — all served by spatial-core through the
FusionGeometryAdapter; ZERO new geometry code.
Plus Fusion-native read-only: `fusion_scene_info` (doc name, units, up-axis,
body/component/timeline counts), `fusion_get_selection` (entityTokens + bboxes
of `ui.activeSelections`), `fusion_capture_viewport` (
`app.activeViewport.saveAsImageFile` → PNG base64).

### Phase B — creation & parametrics
- `fusion_execute_api_script` — escape hatch (every server has one; ours is
  not the centerpiece). `adsk` preloaded, stdout captured, `result` variable
  returned. destructiveHint: true.
- `fusion_set_parameter` / `fusion_list_parameters` — **user parameters are
  Fusion's sliders**; driving them IS parametric design here. Get/set by name
  with unit-aware values. This is the highest-leverage creation tool.
- `fusion_build_features` — the recipe analog: ONE declarative call that
  executes a list of feature ops `[{op:"sketch_rect", plane, w, h, key},
  {op:"extrude", profileOf:key, distance, operation}, {op:"hole_pattern",
  face, dias, grid}, {op:"fillet", edgesOf, radius}]` with keys → entityTokens
  returned (the handle system ported). Idempotency v1: NOT attempted (timeline
  semantics differ from a GH canvas); instead return created tokens and make
  re-runs append — document this honestly.
- sceneVersion bumps in the dispatcher on every mutating method (same list
  discipline as the Rhino listener).

### Phase C — polish / later
Chat palette inside Fusion (HTML palette hosting the same agent backend
pattern as the Rhino panel), templates library, `fusion_undo`, timeline
introspection tools.

All tools carry MCP annotations (readOnlyHint/destructiveHint/idempotentHint)
from day one, and register with `alwaysLoad` semantics in any agent backend.

---

## 5. Milestones & acceptance criteria

| # | Deliverable | Acceptance |
|---|---|---|
| F0 | Repo scaffold: package.json (file: dep on ../rhino-gh-mcp/spatial, ajv devDep), tsconfig, src/bridge.ts + src/index.ts skeleton compiling; add-in skeleton that loads in Fusion and logs to the TEXT COMMANDS palette | `npm run build` clean; add-in appears in Fusion Scripts & Add-Ins and survives run/stop cycles |
| F1 | Listener online: TCP 8767, ping, `fusion.scene` info, CustomEvent main-thread marshaling proven | `ping` → `pong` from a node script; scene info correct with a doc open |
| F2 | **Adapter conformance:** `space.bodies` + `space.tessellate` with units/up-axis/entityToken/assembly handling | `node tools/validate-protocol.mjs` (RHINO_MCP_PORT=8767) prints CONFORMANT against a test doc incl. one assembly with 2 occurrences; volumes match Fusion's own physical properties to 4 sig figs |
| F3 | Full Phase-A tool surface wired in the MCP server | Spatial benchmark port: recreate the rhino bench ground-truth scene in Fusion (two spheres, hollow box, tall cylinder — via an execute script), run the six RFD questions through a client; target ≥5/6 |
| F4 | Phase-B creation tools + sceneVersion | Build a parametric bracket via `fusion_build_features` + drive it via `fusion_set_parameter`; spatial tools verify the result numerically |
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
- Validate Python by running `py_compile` with any CPython 3 (Rhino's works:
  `C:\Users\nerfd\.rhinocode\py39-rh8\python.exe -m py_compile <file>`) —
  Fusion's bundled interpreter is 3.x; keep the add-in ≥3.9-compatible.
- Git identity: `aphollis <20403818+aphollis@users.noreply.github.com>`
  (email privacy on). Portable git: `C:\Users\nerfd\tools\mingit\cmd`;
  gh CLI: `C:\Users\nerfd\tools\ghcli\bin`. Node 22 on PATH. No system
  Python. winget hangs on UAC — use portable installs.
- Commit per milestone; benchmark before/after any efficiency claim.
- MIT license (file present). Borrow only from MIT sources named in the
  survey; credit in comments where nontrivial patterns are copied.

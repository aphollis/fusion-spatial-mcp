# fusion-spatial-mcp — session context

Read `docs/BUILD_PLAN.md` FIRST — it is the authoritative, self-contained plan
with milestones, acceptance gates, Fusion-specific traps, and pointers into
the reference implementation. Follow its phase order (F0→F5); do not write
spatial tool code before the F2 conformance gate passes.

Hard rules:
- **Design Fusion-native.** This project is not a port of the Rhino server:
  tool vocabulary, workflows, and agent guidance come from Fusion's own
  concepts (timeline, features, constrained sketches, user parameters,
  components). The sibling checkout `../rhino-gh-mcp` is an infrastructure
  donor only: a source of engineering patterns (TCP bridge, threading, token
  efficiency). `spatial-core` lives in its own sibling repo `../spatial-core`
  (consumed via `file:` dependency — never fork/copy its source). Do not
  carry Grasshopper concepts or names across.
- Never run generated code against the user's live Fusion session without
  asking first. Compile-check offline; batch live testing into agreed steps.
- Fusion API traps: internal units are ALWAYS centimeters (convert to the
  design's default display unit at the adapter); documents are commonly Y-up
  (report the actual axis); use entityToken as body id; API calls only on the
  main thread via CustomEvent marshaling; listener binds WITHOUT SO_REUSEADDR.
- This repo is public — no machine-specific paths or private details in
  committed files. Machine tooling notes live in session memory, not here.
  Validate add-in Python offline with any CPython 3.9+ `py_compile`.
- Fusion 360 is installed (webdeploy production). Dev add-ins folder:
  %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\ (user has prior add-in
  experience — "Sheeter" add-in and the FusionRhinoBar project).
- Commit per milestone with concise messages; push to origin (public repo).

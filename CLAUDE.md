# fusion-spatial-mcp — session context

Read `docs/BUILD_PLAN.md` FIRST — it is the authoritative, self-contained plan
with milestones, acceptance gates, Fusion-specific traps, and pointers into
the reference implementation. Follow its phase order (F0→F5); do not write
spatial tool code before the F2 conformance gate passes.

Hard rules:
- The sibling project `C:\Users\nerfd\rhino-gh-mcp` is the working reference
  implementation AND the home of `spatial-core` (consumed here via a `file:`
  dependency — never fork/copy its source into this repo).
- Never run generated code against the user's live Fusion session without
  asking first. Compile-check offline; batch live testing into agreed steps.
- Fusion API traps: internal units are ALWAYS centimeters (convert to the
  design's default display unit at the adapter); documents are commonly Y-up
  (report the actual axis); use entityToken as body id; API calls only on the
  main thread via CustomEvent marshaling; listener binds WITHOUT SO_REUSEADDR.
- Tooling on this machine: portable git at C:\Users\nerfd\tools\mingit\cmd,
  gh CLI at C:\Users\nerfd\tools\ghcli\bin (account: aphollis, commits need
  the noreply email — see git config), Node 22, no system Python (py_compile
  via C:\Users\nerfd\.rhinocode\py39-rh8\python.exe), winget hangs on UAC.
- Fusion 360 is installed (webdeploy production). Dev add-ins folder:
  %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\ (user has prior add-in
  experience — "Sheeter" add-in and the FusionRhinoBar project).
- Commit per milestone with concise messages; push to origin (private repo).

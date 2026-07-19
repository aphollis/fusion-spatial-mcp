// F4 acceptance (docs/BUILD_PLAN.md §5): author a parametric bracket as a
// clean timeline (constrained sketch -> extrude -> hole -> 2x2 pattern ->
// fillet) with named user parameters; then resize it purely via parameter
// driving; verify every step numerically with the spatial tools.
// Creates a NEW document; never touches existing ones.
import net from "node:net";
import { SpatialEngine } from "spatial-core";
import { FusionBridge } from "../dist/bridge.js";
import { FusionGeometryAdapter } from "../dist/spatial-adapter.js";

const PORT = Number(process.env.FUSION_MCP_PORT ?? 8767);
const bridge = new FusionBridge("127.0.0.1", PORT);
const engine = new SpatialEngine(new FusionGeometryAdapter(bridge));
const call = (method, params = {}) => bridge.call(method, params, 120_000);

let failures = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? `  (${detail})` : ""}`);
  if (!ok) failures++;
};
const near = (a, b, tol) => Math.abs(a - b) <= tol;
const health = (r) => JSON.stringify(r.timelineHealth);

// -- 1. fresh parametric document -------------------------------------------
await call("fusion.execute", {
  code: "doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)\nresult = doc.name",
});
console.log("step: new parametric document");

// -- 2. parameters up front --------------------------------------------------
await call("fusion.params.set", {
  params: { hole_inset: "10 mm" },
});

// -- 3. constrained base sketch ---------------------------------------------
const sk1 = await call("fusion.sketch.create", {
  plane: "xy", name: "BaseProfile",
  entities: [{ type: "rect", corner1: [0, 0], corner2: [80, 50], key: "r" }],
  constraints: [{ kind: "coincident", a: "r.0.start", b: "origin" }],
  dimensions: [
    { kind: "distance", of: "r.0", orientation: "horizontal",
      value: { param: "base_width", value: "80 mm" } },
    { kind: "distance", of: "r.1", orientation: "vertical",
      value: { param: "base_depth", value: "50 mm" } },
  ],
});
check("base sketch fully constrained", sk1.fullyConstrained === true,
  `profiles=${sk1.profiles.length}, health=${health(sk1)}`);

// -- 4. extrude --------------------------------------------------------------
const ext = await call("fusion.feature.add", {
  type: "extrude", name: "Base", profile: sk1.sketch,
  distance: { param: "base_thick", value: "8 mm" }, operation: "new",
});
const baseBody = ext.bodies[0];
check("extrude Base ok", ext.timelineHealth === "ok" && !!baseBody,
  `body=${baseBody}, health=${health(ext)}`);

let d = await engine.measure({ op: "dims", id: baseBody });
check("base dims 80 x 50 x 8", near(d.dims[0], 80, 0.1) &&
  near(d.dims[1], 50, 0.1) && near(d.dims[2], 8, 0.1), `dims=${JSON.stringify(d.dims)}`);

// -- 5. top face + hole point sketch ----------------------------------------
const face = await call("fusion.execute", {
  code: [
    "des = adsk.fusion.Design.cast(app.activeProduct)",
    "body = des.rootComponent.bRepBodies.item(0)",
    "top = None",
    "for f in body.faces:",
    "    pl = adsk.core.Plane.cast(f.geometry)",
    "    if pl is not None and abs(pl.normal.z) > 0.999:",
    "        if top is None or f.centroid.z > top.centroid.z:",
    "            top = f",
    "result = top.entityToken",
  ].join("\n"),
});
const sk2 = await call("fusion.sketch.create", {
  plane: face.result, name: "HolePoints",
  entities: [{ type: "point", at: [10, 10], key: "h1" }],
  dimensions: [
    { kind: "distance", a: "h1", b: "origin", orientation: "horizontal", value: "hole_inset" },
    { kind: "distance", a: "h1", b: "origin", orientation: "vertical", value: "hole_inset" },
  ],
});
console.log(`step: hole-point sketch ${sk2.sketch} on top face, health=${health(sk2)}`);

// where did the seed point land in model space? (face sketches may flip axes)
const seed = await call("fusion.execute", {
  code: [
    "des = adsk.fusion.Design.cast(app.activeProduct)",
    "sk = None",
    "for s in des.rootComponent.sketches:",
    "    if s.name == 'HolePoints':",
    "        sk = s",
    "p = None",
    "for sp in sk.sketchPoints:",
    "    g = sp.worldGeometry",
    "    if abs(g.z - 0.8) < 1e-6 and (abs(g.x) > 1e-9 or abs(g.y) > 1e-9):",
    "        p = g",
    "result = [p.x * 10, p.y * 10, p.z * 10]",
  ].join("\n"),
});
const [wx, wy] = seed.result;
check("seed hole point at inset from a corner",
  (near(Math.abs(wx), 10, 0.5) || near(Math.abs(wx), 70, 0.5)) &&
  (near(Math.abs(wy), 10, 0.5) || near(Math.abs(wy), 40, 0.5)),
  `world=(${wx.toFixed(1)}, ${wy.toFixed(1)})`);
const sx = wx < 40 ? "" : "-";  // pattern must extend toward the far side
const sy = wy < 25 ? "" : "-";

// -- 6. hole + pattern -------------------------------------------------------
const hole = await call("fusion.feature.add", {
  type: "hole", name: "MountHole", sketch: sk2.sketch, points: ["h1"],
  diameter: { param: "hole_d", value: "6 mm" }, depth: "through",
});
check("hole MountHole ok", hole.timelineHealth === "ok", health(hole));

let probe = await engine.measure({ op: "probe", point: [wx, wy, 4] });
check("hole is open at seed point", probe.insideOf.length === 0,
  `insideOf=${JSON.stringify(probe.insideOf)}`);

const pat = await call("fusion.feature.add", {
  type: "rectangularPattern", name: "HolePattern", features: ["MountHole"],
  axisOne: "x", countOne: 2, spacingOne: `${sx}(base_width - 2*hole_inset)`,
  axisTwo: "y", countTwo: 2, spacingTwo: `${sy}(base_depth - 2*hole_inset)`,
});
check("2x2 hole pattern ok", pat.timelineHealth === "ok", health(pat));

let sec = await engine.section({ origin: [40, 25, 4], normal: [0, 0, 1] });
check("section shows 4 holes (5 loops)", sec.loops.length === 5,
  `loops=${sec.loops.length}`);

// -- 7. fillet the vertical corner edges ------------------------------------
const fil = await call("fusion.feature.add", {
  type: "fillet", name: "CornerFillets",
  edges: { body: baseBody, parallelTo: "z" },
  radius: { param: "fillet_r", value: "3 mm" },
});
check("corner fillets ok", fil.timelineHealth === "ok", health(fil));

// -- 8. resize PURELY via parameters ----------------------------------------
const resize = await call("fusion.params.set", {
  params: { base_width: "100 mm", hole_d: "8 mm" },
});
check("parameter resize recomputed clean", resize.timelineHealth === "ok",
  health(resize));

d = await engine.measure({ op: "dims", id: baseBody });
check("resized dims 100 x 50 x 8", near(d.dims[0], 100, 0.1) &&
  near(d.dims[1], 50, 0.1) && near(d.dims[2], 8, 0.1), `dims=${JSON.stringify(d.dims)}`);

sec = await engine.section({ origin: [50, 25, 4], normal: [0, 0, 1] });
const circles = sec.loops.filter((l) => l.area != null && l.area < 60);
check("holes resized to d=8 (area ~50.3)", circles.length === 4 &&
  circles.every((l) => near(l.area, 50.27, 2)),
  `circleAreas=${circles.map((l) => l.area).join(", ")}`);

// pattern followed base_width: far column = seed +/- new spacing (100-20=80)
const farX = wx + (sx === "" ? 1 : -1) * 80;
probe = await engine.measure({ op: "probe", point: [farX, wy, 4] });
check("far hole column tracked base_width", probe.insideOf.length === 0,
  `probe@x=${farX.toFixed(1)} insideOf=${JSON.stringify(probe.insideOf)}`);

// volume sanity: 100*50*8 - 4 holes pi*16*8 - 4 fillet corners (9-pi*9/4)*8
const dig = await engine.digest();
const vol = dig.bodies[0].volume;
const expected = 100 * 50 * 8 - 4 * Math.PI * 16 * 8 - 4 * (9 - (Math.PI * 9) / 4) * 8;
check("volume matches parametric model", near(vol, expected, expected * 0.01),
  `vol=${vol}, expected~${expected.toFixed(1)}`);

// -- 9. timeline quality -----------------------------------------------------
const tl = await call("fusion.timeline");
const names = tl.timeline.map((t) => t.name);
const unhealthy = tl.timeline.filter((t) => t.health && t.health !== "ok");
check("timeline clean & named", unhealthy.length === 0 &&
  ["BaseProfile", "Base", "HolePoints", "MountHole", "HolePattern", "CornerFillets"]
    .every((n) => names.includes(n)),
  `timeline=[${names.join(", ")}]`);

const pl = await call("fusion.params.list");
console.log("user parameters:", pl.userParameters
  .map((p) => `${p.name}=${p.expression}`).join(", "));

console.log(failures === 0 ? "\nF4 BRACKET ACCEPTANCE PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);

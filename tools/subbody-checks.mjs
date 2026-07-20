// Acceptance for the sub-body geometry tools (fusion_find_geometry +
// fusion_measure_relation) against the ground-truth scene (tools/f2-scene.py,
// mm): analytic classification, radius filters, and relation verdicts
// including the coaxial-vs-concentric distinction. Read-only.
import net from "node:net";
import { FusionBridge } from "../dist/bridge.js";

const bridge = new FusionBridge("127.0.0.1", Number(process.env.FUSION_MCP_PORT ?? 8767));
const call = (m, p = {}) => bridge.call(m, p, 120_000);

let failures = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? `  (${detail})` : ""}`);
  if (!ok) failures++;
};
const near = (a, b, tol) => Math.abs(a - b) <= tol;

// -- find_geometry classification -------------------------------------------
const cyl = await call("fusion.find_geometry", { kind: "cylinder_face" });
check("one cylinder face, r=5, axis z", cyl.matchCount === 1 &&
  near(cyl.matches[0].radius, 5, 0.01) && Math.abs(cyl.matches[0].axis[2]) > 0.999,
  `count=${cyl.matchCount} r=${cyl.matches[0]?.radius} axis=${JSON.stringify(cyl.matches[0]?.axis)}`);

const sph = await call("fusion.find_geometry", { kind: "sphere_face" });
check("two sphere faces", sph.matchCount === 2, `count=${sph.matchCount}`);

const pln = await call("fusion.find_geometry", { kind: "planar_face", maxResults: 100 });
check("many planar faces (box in+out, caps, pegs)", pln.matchCount >= 20,
  `count=${pln.matchCount}`);

const rad = await call("fusion.find_geometry", { kind: "circular_edge", radius: 5, target: "TallCylinder" });
check("cylinder rim edges by radius filter", rad.matchCount === 2,
  `count=${rad.matchCount}`);

// -- coaxial vs concentric (the FE trap, both directions) -------------------
const [eTop, eBot] = rad.matches.sort((a, b) => b.position[2] - a.position[2]);
const co = await call("fusion.relation", { a: eTop.id, b: eBot.id, relation: "coaxial" });
check("rim edges COAXIAL (same axis line)", co.passed === true,
  `angle=${co.measured.angleDeg} offset=${co.measured.axisOffset}`);
const cc = await call("fusion.relation", { a: eTop.id, b: eBot.id, relation: "concentric" });
check("rim edges NOT concentric (centers 100 apart)", cc.passed === false &&
  near(cc.measured.centerDistance, 100, 0.1), `centerDistance=${cc.measured.centerDistance}`);

// -- planar relations on the HollowBox --------------------------------------
const face = (z, nz) => pln.matches.find((m) =>
  m.position[0] > 79 && m.position[0] < 121 &&
  near(m.position[2], z, 0.5) && m.normal && near(m.normal[2], nz, 0.01));
const top = face(40, 1), bot = face(0, -1);
const side = pln.matches.find((m) => m.normal && near(Math.abs(m.normal[0]), 1, 0.01) &&
  near(m.position[0], 120, 0.5));
const par = await call("fusion.relation", { a: top.id, b: bot.id, relation: "parallel" });
check("box top || bottom", par.passed === true, `angle=${par.measured.angleDeg}`);
const fl = await call("fusion.relation", { a: top.id, b: bot.id, relation: "flush" });
check("box top NOT flush with bottom (offset 40)", fl.passed === false &&
  near(fl.measured.planeOffset, 40, 0.1), `planeOffset=${fl.measured.planeOffset}`);
const perp = await call("fusion.relation", { a: top.id, b: side.id, relation: "perpendicular" });
check("box top _|_ side", perp.passed === true, `angle=${perp.measured.angleDeg}`);

// -- body-level distance / clearance ----------------------------------------
const bodies = (await call("space.bodies", {})).bodies;
const byName = {};
for (const b of bodies) byName[`${b.name}|${b.layer}`] = b.id;
const d = await call("fusion.relation", {
  a: byName["SphereA|(Unsaved)"] ?? bodies.find((b) => b.name === "SphereA").id,
  b: bodies.find((b) => b.name === "SphereB").id, relation: "distance",
});
check("sphere distance 30 (kernel-exact)", near(d.measured.distance, 30, 0.01),
  `d=${d.measured.distance}`);
const pegs = bodies.filter((b) => b.name === "PegBody");
const cl = await call("fusion.relation", { a: pegs[0].id, b: pegs[1].id, relation: "clearance", tolerance: 5 });
check("pegs clear by >=5mm", cl.passed === true, `d=${cl.measured.distance}`);

// -- self-healing: poison a handle's token, expect locator re-find ----------
const preTok = JSON.stringify({ code:
  `import sys
mod = None
for k, m in list(sys.modules.items()):
    if hasattr(m, "_ENTITY_INFO") and hasattr(m, "SCENE_VERSION"):
        mod = m
info = mod._ENTITY_INFO.get(${JSON.stringify(eTop.id)})
info["token"] = "poisoned-stale-token"
result = "poisoned"` });
await call("fusion.execute", JSON.parse(preTok));
const healed = await call("fusion.relation", { a: eTop.id, b: eBot.id, relation: "coaxial" });
check("self-healing: poisoned handle re-found by kind+position", healed.passed === true,
  `angle=${healed.measured.angleDeg} offset=${healed.measured.axisOffset}`);

console.log(failures === 0 ? "\nSUB-BODY CHECKS PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);

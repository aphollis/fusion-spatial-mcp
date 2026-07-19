// F3 acceptance: the six-question spatial benchmark against the ground-truth
// scene (tools/f2-scene.py, doc units mm). Exercises SpatialEngine through
// the real FusionGeometryAdapter — the same code path as the MCP tools.
// Target: >=5/6 (docs/BUILD_PLAN.md §5).
import { SpatialEngine } from "spatial-core";
import { FusionBridge } from "../dist/bridge.js";
import { FusionGeometryAdapter } from "../dist/spatial-adapter.js";

const bridge = new FusionBridge("127.0.0.1", Number(process.env.FUSION_MCP_PORT ?? 8767));
const engine = new SpatialEngine(new FusionGeometryAdapter(bridge));

let score = 0;
const q = async (n, question, fn) => {
  try {
    const [ok, detail] = await fn();
    console.log(`${ok ? "PASS" : "FAIL"} Q${n} ${question}\n     ${detail}`);
    if (ok) score++;
  } catch (e) {
    console.log(`FAIL Q${n} ${question}\n     threw: ${e.message.split("\n")[0]}`);
  }
};

const digest = await engine.digest();
const byName = {};
for (const b of digest.bodies) byName[b.name] = b;
const id = (n) => byName[n].id;
const near = (a, b, tol) => Math.abs(a - b) <= tol;

await q(1, "surface-to-surface distance SphereA -> SphereB (expect 30mm)", async () => {
  const r = await engine.measure({ op: "distance", a: id("SphereA"), b: id("SphereB") });
  return [near(r.distance, 30, 0.5), `distance=${r.distance}`];
});

await q(2, "is HollowBox hollow, wall thickness ~2mm (section z=20)", async () => {
  const r = await engine.section({ ids: [id("HollowBox")], origin: [100, 0, 20], normal: [0, 0, 1] });
  const wt = r.wallThickness;
  const ok = r.loops.length >= 2 && wt != null && near(wt.min, 2, 0.5);
  return [ok, `loops=${r.loops.length} wallThickness=${JSON.stringify(wt)}`];
});

await q(3, "TallCylinder dimensions (expect 10 x 10 x 100mm)", async () => {
  const r = await engine.measure({ op: "dims", id: id("TallCylinder") });
  const ok = near(r.dims[0], 10, 0.5) && near(r.dims[1], 10, 0.5) && near(r.dims[2], 100, 0.5);
  return [ok, `dims=${JSON.stringify(r.dims)}`];
});

await q(4, "probe HollowBox cavity center (expect: inside NO solid, nearest=HollowBox @ ~18mm)", async () => {
  const r = await engine.measure({ op: "probe", point: [100, 0, 20] });
  const ok = r.insideOf.length === 0 &&
    r.nearest != null && r.nearest.id === id("HollowBox") && near(r.nearest.distance, 18, 1);
  return [ok, `insideOf=${JSON.stringify(r.insideOf)} nearest=${r.nearest?.id}@${r.nearest?.distance}`];
});

await q(5, "relations: all six bodies mutually clear, SphereA-SphereB clearance ~30mm", async () => {
  const r = await engine.relations({ maxPairs: 30 });
  const bad = r.pairs.filter((p) => p.relation !== "clear");
  const ab = r.pairs.find((p) =>
    [p.a, p.b].includes(id("SphereA")) && [p.a, p.b].includes(id("SphereB")));
  const ok = bad.length === 0 && ab != null && near(ab.clearance, 30, 0.5);
  return [ok, `pairs=${r.pairs.length} nonClear=${bad.length} A-B clearance=${ab?.clearance}`];
});

await q(6, "voxels reveal HollowBox interior void in a mid layer", async () => {
  const r = await engine.voxels({ ids: [id("HollowBox")], res: 12 });
  const mid = r.layers[Math.floor(r.layers.length / 2)];
  const rows = mid.grid.split("\n");
  const inner = rows.slice(1, -1).some((row) => row.slice(1, -1).includes("."));
  const edgeFilled = rows[0].includes("#");
  return [inner && edgeFilled, `midLayer ${mid.index}:\n${mid.grid.split("\n").map((r2) => "       " + r2).join("\n")}`];
});

console.log(`\nSCORE ${score}/6 ${score >= 5 ? "- F3 BENCHMARK PASS" : "- BELOW TARGET"}`);
process.exit(score >= 5 ? 0 : 1);

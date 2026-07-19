// F2 acceptance checks beyond schema conformance (docs/BUILD_PLAN.md §5):
//  - space.bodies volumes match Fusion's own physical properties to 4 sig figs
//    (ground truth = tools/f2-scene.py expected values, doc units mm)
//  - tessellation of occurrence-proxy bodies lands in WORLD space (mesh bbox
//    must match the body's world bbox from space.bodies)
import net from "node:net";

const PORT = Number(process.env.FUSION_MCP_PORT ?? 8767);
function call(method, params) {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection({ host: "127.0.0.1", port: PORT });
    let buf = "";
    sock.once("error", reject);
    sock.on("data", (d) => {
      buf += d.toString();
      const i = buf.indexOf("\n");
      if (i < 0) return;
      const msg = JSON.parse(buf.slice(0, i));
      sock.end();
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    });
    sock.once("connect", () =>
      sock.write(JSON.stringify({ id: 1, method, params }) + "\n"));
    setTimeout(() => reject(new Error("timeout")), 120000);
  });
}

const sig4 = (n) => Number(n).toPrecision(4);
let failures = 0;
function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` ${detail}` : ""}`);
  if (!ok) failures++;
}

const scene = await call("space.bodies", {});
check("units are mm", scene.units === "mm", `(got ${scene.units})`);

// expected volumes in mm^3 (scene is authored in cm, doc units mm)
const expected = {
  SphereA: 4188.7902, SphereB: 4188.7902,
  HollowBox: 17344, TallCylinder: 7853.9816, PegBody: 2000,
};
for (const b of scene.bodies) {
  const exp = expected[b.name];
  if (exp == null) continue;
  check(`volume ${b.name} (${b.layer})`,
    sig4(b.volume) === sig4(exp), `got ${b.volume}, want ~${exp}`);
}
check("6 bodies found", scene.bodies.length === 6, `(got ${scene.bodies.length})`);

// world-space tessellation of both Peg proxies + one root body
const targets = scene.bodies.filter((b) => b.name === "PegBody")
  .concat(scene.bodies.filter((b) => b.name === "HollowBox"));
for (const b of targets) {
  const t = await call("space.tessellate", { id: b.id });
  const v = Buffer.from(t.vertices_b64, "base64");
  const verts = new Float32Array(v.buffer.slice(v.byteOffset, v.byteOffset + v.byteLength));
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < verts.length; i += 3)
    for (let a = 0; a < 3; a++) {
      lo[a] = Math.min(lo[a], verts[i + a]);
      hi[a] = Math.max(hi[a], verts[i + a]);
    }
  const near = (p, q) => Math.abs(p - q) < 0.5; // 0.5 mm mesh-vs-brep slack
  const ok = lo.every((x, a) => near(x, b.bbox.min[a])) &&
             hi.every((x, a) => near(x, b.bbox.max[a]));
  check(`tessellation in world space: ${b.name} (${b.layer})`, ok,
    `mesh [${lo.map(sig4)}]..[${hi.map(sig4)}] vs bbox [${b.bbox.min}]..[${b.bbox.max}]`);
}

console.log(failures === 0 ? "\nF2 CHECKS PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);

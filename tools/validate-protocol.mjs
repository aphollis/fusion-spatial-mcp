// Adapter conformance check: validates a LIVE listener's space.* responses
// against the checked-in JSON Schema contracts (contracts/*.schema.json).
// Works for any platform adapter that speaks the protocol (Rhino today,
// Fusion later). Read-only: makes no mutating calls.
//
//   node tools/validate-protocol.mjs           (listener on 127.0.0.1:8767)
//   RHINO_MCP_PORT=9999 node tools/validate-protocol.mjs

import net from "node:net";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import Ajv from "ajv";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONTRACTS = path.join(__dirname, "..", "contracts");
// Default = the Fusion add-in's port (this repo). Env override still works.
const PORT = Number(process.env.RHINO_MCP_PORT ?? 8767);

const ajv = new Ajv({ allErrors: true, strict: false });
const validateBodies = ajv.compile(
  JSON.parse(fs.readFileSync(path.join(CONTRACTS, "space.bodies.response.schema.json"), "utf8")));
const validateTess = ajv.compile(
  JSON.parse(fs.readFileSync(path.join(CONTRACTS, "space.tessellate.response.schema.json"), "utf8")));

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
    setTimeout(() => reject(new Error("timeout")), 60000);
  });
}

let failures = 0;
function report(name, valid, errors) {
  if (valid) {
    console.log(`PASS ${name}`);
  } else {
    failures++;
    console.log(`FAIL ${name}`);
    for (const e of errors ?? []) console.log(`   ${e.instancePath} ${e.message}`);
  }
}

const bodies = await call("space.bodies", { scope: "all" });
report("space.bodies conforms", validateBodies(bodies), validateBodies.errors);
console.log(`   (${bodies.bodies.length} bodies, sceneVersion ${bodies.sceneVersion})`);

const meshable = bodies.bodies.find((b) => ["solid", "surface", "mesh"].includes(b.kind));
if (meshable) {
  const tess = await call("space.tessellate", { id: meshable.id });
  report("space.tessellate conforms", validateTess(tess), validateTess.errors);
  const vlen = Buffer.from(tess.vertices_b64, "base64").length;
  const ilen = Buffer.from(tess.indices_b64, "base64").length;
  report("tessellate buffer lengths match counts",
    vlen === tess.vertexCount * 12 && ilen === tess.triangleCount * 12);
  console.log(`   (${tess.vertexCount} verts, ${tess.triangleCount} tris from "${meshable.id.slice(0, 12)}")`);
} else {
  console.log("SKIP space.tessellate (no meshable body in scene)");
}

console.log(failures === 0 ? "\nCONFORMANT" : `\n${failures} CONFORMANCE FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);

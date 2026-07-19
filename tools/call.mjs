// Dev CLI: send one JSON-lines request to the Fusion listener and print the
// reply. Usage:
//   node tools/call.mjs ping
//   node tools/call.mjs fusion.document
//   node tools/call.mjs space.tessellate '{"id":"..."}'
//   node tools/call.mjs fusion.execute @tools/f2-scene.py   (@file -> {code})
import net from "node:net";
import fs from "node:fs";

const [method, arg] = process.argv.slice(2);
if (!method) {
  console.error("usage: node tools/call.mjs <method> [jsonParams | @pyfile]");
  process.exit(2);
}
let params = {};
if (arg) {
  params = arg.startsWith("@")
    ? { code: fs.readFileSync(arg.slice(1), "utf8") }
    : JSON.parse(arg);
}
const PORT = Number(process.env.FUSION_MCP_PORT ?? 8767);
const sock = net.createConnection({ host: "127.0.0.1", port: PORT });
let buf = "";
sock.once("error", (e) => { console.error("connect failed:", e.message); process.exit(1); });
sock.on("data", (d) => {
  buf += d.toString();
  const i = buf.indexOf("\n");
  if (i < 0) return;
  sock.end();
  const msg = JSON.parse(buf.slice(0, i));
  console.log(JSON.stringify(msg, null, 2));
  process.exit(msg.error ? 1 : 0);
});
sock.once("connect", () =>
  sock.write(JSON.stringify({ id: 1, method, params }) + "\n"));
setTimeout(() => { console.error("timeout"); process.exit(1); }, 120000);

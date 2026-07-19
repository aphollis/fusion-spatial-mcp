#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { SpatialEngine } from "spatial-core";
import { FusionBridge } from "./bridge.js";
import { FusionGeometryAdapter } from "./spatial-adapter.js";

const PORT = Number(process.env.FUSION_MCP_PORT ?? 8767);
const bridge = new FusionBridge("127.0.0.1", PORT);
const spatial = new SpatialEngine(new FusionGeometryAdapter(bridge));

const INSTRUCTIONS = `
Tools for driving Autodesk Fusion 360 live. The FusionSpatialMCP add-in must be
running inside Fusion (Shift+S > Add-Ins tab > FusionSpatialMCP > Run).

In Fusion the design IS the timeline: an ordered history of features driven by
named user parameters and constrained sketches. Orient with fusion_document
first — it summarizes the document, units, and the feature timeline.

3D spatial understanding (space_* tools): for ANY metric question (size,
position, distance, clearance, containment, hollowness, wall thickness) use
these instead of screenshots — they return exact numbers.
`.trim();

const server = new McpServer(
  { name: "fusion-spatial", version: "0.1.0" },
  { instructions: INSTRUCTIONS },
);

type ToolResult = {
  content: Array<{ type: "text"; text: string } | { type: "image"; data: string; mimeType: string }>;
  isError?: boolean;
};

function text(value: unknown): ToolResult {
  const s = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return { content: [{ type: "text", text: s }] };
}

function errorResult(e: unknown): ToolResult {
  return {
    content: [{ type: "text", text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
    isError: true,
  };
}

async function relay(
  method: string,
  params: Record<string, unknown> = {},
  timeoutMs?: number,
): Promise<ToolResult> {
  try {
    return text(await bridge.call(method, params, timeoutMs));
  } catch (e) {
    return errorResult(e);
  }
}

async function spatialCall(fn: () => Promise<unknown>): Promise<ToolResult> {
  try {
    return text(await fn());
  } catch (e) {
    return errorResult(e);
  }
}

/* ------------------------------ Fusion tools ------------------------------ */
/* F1 smoke-test surface. The full Phase-A tool set (space_* + fusion_*) is
 * wired in milestone F3, after the F2 adapter conformance gate passes.
 * `spatial`, `relay`, `spatialCall`, and `z` are already in place for it. */

server.registerTool(
  "fusion_document",
  {
    annotations: { readOnlyHint: true },
    description:
      "Orientation call: active Fusion document name, design type, units, up-axis, component and " +
      "body counts, plus a timeline summary (features in order with type/health/suppression). " +
      "In Fusion the timeline IS the scene graph — call this first.",
    inputSchema: {},
  },
  async () => relay("fusion.document"),
);

/* --------------------------------- main ----------------------------------- */

void spatial; // consumed by the F3 tool surface
void z;
void spatialCall;

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`fusion-spatial-mcp: MCP server on stdio, expecting Fusion listener on 127.0.0.1:${PORT}`);
}

main().catch((e) => {
  console.error("Fatal:", e);
  process.exit(1);
});

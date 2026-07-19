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
first — it summarizes the document, units, up-axis, and the feature timeline.

Bodies are addressed by short handles ("b1", "b2", ...) returned by
space_digest / fusion_get_selection; body names also work. Handles persist for
the add-in session.

3D spatial understanding (space_* tools): for ANY metric question (size,
position, distance, clearance, containment, hollowness, wall thickness) use
these instead of screenshots — they return exact numbers. space_digest =
scene inventory; space_measure = one targeted measurement; space_relations =
collision/containment; space_voxels = volumetric occupancy layers;
space_section = internal profiles; space_views = labeled orthographic PNG;
space_fit = free-space placement search; space_pick = what's under a pixel
of the last space_views image.

fusion_execute_api_script is the escape hatch: Python inside Fusion with
adsk/app/ui preloaded. Assign to a variable named result to get a value back.
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

server.registerTool(
  "fusion_document",
  {
    annotations: { readOnlyHint: true },
    description:
      "Orientation call: active Fusion document name, design type, units, up-axis, component and " +
      "body counts, user-parameter count, plus a timeline summary (features in order with " +
      "type/health/suppression). In Fusion the timeline IS the scene graph — call this first.",
    inputSchema: {},
  },
  async () => relay("fusion.document"),
);

server.registerTool(
  "fusion_get_selection",
  {
    annotations: { readOnlyHint: true },
    description:
      "List what the user currently has SELECTED in Fusion (kind, id/handle, name, bbox, owning " +
      "body for faces/edges). Call whenever the user says 'this', 'these', 'the selected face' — " +
      "it resolves what they are pointing at.",
    inputSchema: {},
  },
  async () => relay("fusion.selection"),
);

server.registerTool(
  "fusion_capture_viewport",
  {
    annotations: { readOnlyHint: true },
    description:
      "Screenshot of the active Fusion viewport as a PNG. Good for seeing appearance/UI state; " +
      "for metric questions use the space_* tools instead.",
    inputSchema: {
      width: z.number().int().min(64).max(1920).optional().describe("Pixels (default 800)"),
      height: z.number().int().min(64).max(1920).optional().describe("Pixels (default 600)"),
    },
  },
  async ({ width, height }) => {
    try {
      const r = await bridge.call("fusion.capture", { width, height }, 60_000);
      return {
        content: [{ type: "image", data: r.png_b64, mimeType: "image/png" }],
      };
    } catch (e) {
      return errorResult(e);
    }
  },
);

server.registerTool(
  "fusion_execute_api_script",
  {
    annotations: { destructiveHint: true },
    description:
      "Escape hatch: run Python inside Fusion on the main thread. Preloaded globals: adsk, app " +
      "(Application), ui (UserInterface). Assign to a variable named `result` to return a value; " +
      "stdout is captured. Mutations bump sceneVersion. Prefer dedicated tools when they exist.",
    inputSchema: {
      code: z.string().describe("Python source to execute"),
      timeoutMs: z.number().int().min(1000).max(600_000).optional(),
    },
  },
  async ({ code, timeoutMs }) => relay("fusion.execute", { code }, timeoutMs ?? 120_000),
);

/* ------------------------------ spatial tools ----------------------------- */

const idsParam = z
  .array(z.string())
  .optional()
  .describe("Limit to these bodies (handles like 'b1', body names, or entityTokens); default all");

server.registerTool(
  "space_digest",
  {
    annotations: { readOnlyHint: true },
    description:
      "Metric inventory of the 3D scene: every body's handle, kind, bounding box, overall " +
      "dimensions, kernel-exact volume/area/centroid, assembly context, and units. Prefer this " +
      "over screenshots for any size/position/count question — it returns exact numbers.",
    inputSchema: {
      ids: idsParam,
    },
  },
  async ({ ids }) => spatialCall(() => spatial.digest({ ids })),
);

server.registerTool(
  "space_measure",
  {
    annotations: { readOnlyHint: true },
    description:
      "Targeted spatial measurement. op='distance' (a,b): min distance + closest points between two " +
      "bodies. op='bbox' (ids): union bounding box + dims. op='dims' (id): one body's dimensions. " +
      "op='probe' (point): which solids contain the point + nearest body. Pay-per-question — cheapest " +
      "way to answer a specific metric query.",
    inputSchema: {
      op: z.enum(["distance", "bbox", "dims", "probe"]),
      a: z.string().optional().describe("distance: first body handle"),
      b: z.string().optional().describe("distance: second body handle"),
      id: z.string().optional().describe("dims: body handle"),
      ids: z.array(z.string()).optional().describe("bbox: body handles"),
      point: z.array(z.number()).length(3).optional().describe("probe: [x,y,z]"),
    },
  },
  async (args) => spatialCall(() => spatial.measure(args as never)),
);

server.registerTool(
  "space_relations",
  {
    annotations: { readOnlyHint: true },
    description:
      "Pairwise spatial relationships between bodies: clear (with clearance distance), intersects, " +
      "or containment (a_inside_b / b_inside_a). Use to check collisions, clearances, and nesting. " +
      "Pairs are bbox-prefiltered and capped.",
    inputSchema: {
      ids: idsParam,
      maxPairs: z.number().int().min(1).max(100).optional().describe("Pair cap (default 20)"),
    },
  },
  async ({ ids, maxPairs }) => spatialCall(() => spatial.relations({ ids, maxPairs })),
);

server.registerTool(
  "space_voxels",
  {
    annotations: { readOnlyHint: true },
    description:
      "Volumetric occupancy of the scene as stacked ASCII layers ('#'=filled, '.'=empty) along an " +
      "axis — a 3D mental model you can reason over slice by slice. Reveals hollowness, mass " +
      "distribution, and internal structure that no screenshot shows. Default 16-cell resolution.",
    inputSchema: {
      ids: idsParam,
      res: z.number().int().min(4).max(48).optional().describe("Cells along longest axis (default 16)"),
      axis: z.enum(["x", "y", "z"]).optional().describe("Stacking axis (default z)"),
    },
  },
  async ({ ids, res, axis }) => spatialCall(() => spatial.voxels({ ids, res, axis })),
);

server.registerTool(
  "space_section",
  {
    annotations: { readOnlyHint: true },
    description:
      "Cut the scene with a plane and return the profile loops with lengths, areas, and wall " +
      "thickness (when nested loops exist). The way to inspect internal structure: shells, " +
      "cavities, wall thicknesses.",
    inputSchema: {
      origin: z.array(z.number()).length(3).describe("Point on the cutting plane [x,y,z]"),
      normal: z.array(z.number()).length(3).describe("Plane normal [x,y,z]"),
      ids: idsParam,
    },
  },
  async ({ origin, normal, ids }) =>
    spatialCall(() =>
      spatial.section({
        ids,
        origin: origin as [number, number, number],
        normal: normal as [number, number, number],
      }),
    ),
);

server.registerTool(
  "space_fit",
  {
    annotations: { readOnlyHint: true },
    description:
      "Free-space/placement search: find axis-aligned positions where a box of given dimensions fits " +
      "with a clearance on all sides, avoiding existing geometry. Returns candidate placements (bbox + " +
      "center) sorted by distance to a target point, plus the total number of valid positions. Use for " +
      "'where can this part go?' assembly questions. Grid-approximate — verify a chosen spot with " +
      "space_measure.",
    inputSchema: {
      dims: z.array(z.number().positive()).length(3).describe("Part size [dx,dy,dz] in doc units"),
      clearance: z.number().min(0).optional().describe("Required clearance on all sides (default 0)"),
      ids: idsParam,
      region: z
        .object({
          min: z.array(z.number()).length(3),
          max: z.array(z.number()).length(3),
        })
        .optional()
        .describe("Search region bbox; default = scene bbox expanded by the part size"),
      target: z.array(z.number()).length(3).optional().describe("Prefer placements near this point"),
      res: z.number().int().min(8).max(64).optional().describe("Grid cells along longest axis (default 32)"),
      maxResults: z.number().int().min(1).max(20).optional().describe("Candidates to return (default 5)"),
    },
  },
  async ({ dims, clearance, ids, region, target, res, maxResults }) =>
    spatialCall(() =>
      spatial.fit({
        dims: dims as [number, number, number],
        clearance,
        ids,
        region: region as { min: [number, number, number]; max: [number, number, number] } | undefined,
        target: target as [number, number, number] | undefined,
        res,
        maxResults,
      }),
    ),
);

server.registerTool(
  "space_views",
  {
    annotations: { readOnlyHint: true },
    description:
      "Neutral engineering multiview of the geometry: one PNG with four labeled orthographic tiles " +
      "(top / front / right / iso), depth-shaded with a scale grid, plus a text legend. Better than " +
      "a perspective screenshot for understanding form — no camera guesswork.",
    inputSchema: {
      ids: idsParam,
      tile: z.number().int().min(120).max(480).optional().describe("Pixels per tile (default 240)"),
    },
  },
  async ({ ids, tile }) => {
    try {
      const r = await spatial.views({ ids, tile });
      return {
        content: [
          { type: "text", text: r.legend },
          { type: "image", data: r.png.toString("base64"), mimeType: "image/png" },
        ],
      };
    } catch (e) {
      return errorResult(e);
    }
  },
);

server.registerTool(
  "space_pick",
  {
    annotations: { readOnlyHint: true },
    description:
      "Identify what is under a pixel of the MOST RECENT space_views image (call with the SAME ids " +
      "and tile as that space_views call). px/py are full-image coordinates (0..2*tile). Returns the " +
      "quadrant name and the hit body id/name + 3D world point, or null for background. Use when you " +
      "see something in a rendered view and need to know which body it is.",
    inputSchema: {
      px: z.number().int().min(0).describe("Pixel x in the full views image"),
      py: z.number().int().min(0).describe("Pixel y in the full views image"),
      ids: idsParam,
      tile: z.number().int().min(120).max(480).optional().describe("Tile size used in the space_views call (default 240)"),
    },
  },
  async ({ px, py, ids, tile }) => spatialCall(() => spatial.pick({ px, py, ids, tile })),
);

/* --------------------------------- main ----------------------------------- */

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`fusion-spatial-mcp: MCP server on stdio, expecting Fusion listener on 127.0.0.1:${PORT}`);
}

main().catch((e) => {
  console.error("Fatal:", e);
  process.exit(1);
});

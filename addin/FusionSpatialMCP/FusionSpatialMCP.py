# -*- coding: utf-8 -*-
"""
FusionSpatialMCP - Fusion 360 add-in half of fusion-spatial-mcp.

Serves newline-delimited JSON over TCP on 127.0.0.1:8767 for the Node MCP
server:  {"id": 1, "method": "space.bodies", "params": {...}}
      -> {"id": 1, "result": {...}} | {"id": 1, "error": {"message", "traceback"}}

All Fusion API work is marshalled onto the main thread via a CustomEvent
(the standard pattern; cf. Autodesk's MIT FusionMCPSample TaskManager): the
socket thread parks the job, fires the event, the handler executes on the
main thread, and a threading.Event releases the socket thread to respond.

Install: link or copy this folder into
  %APPDATA%\\Autodesk\\Autodesk Fusion 360\\API\\AddIns\\FusionSpatialMCP
then Shift+S > Add-Ins tab > FusionSpatialMCP > Run.
Logs go to the Text Commands palette (View > Show Text Commands).

Kept as a single file on purpose: Fusion caches imported sibling modules
across add-in restarts, which turns multi-module add-ins into stale-code
traps during development.

Fusion API traps honored here (docs/BUILD_PLAN.md §3):
- API lengths are ALWAYS internal centimeters; everything serialized is
  converted to the design's default length unit, which is reported.
- entityToken is the body id (persistent, but can go stale after
  destructive edits - resolved defensively).
- Assembly bodies are reached through occurrence proxies and serialized in
  world space.
- The listener binds WITHOUT SO_REUSEADDR so a zombie socket fails loudly.
"""

import base64
import json
import math
import socket
import threading
import traceback
from array import array

import adsk.core
import adsk.fusion

HOST = "127.0.0.1"
PORT = 8767
MAIN_TIMEOUT = 600  # seconds a single command may wait on the main thread
EVENT_ID = "FusionSpatialMCP_dispatch"

_app = None
_ui = None
_custom_event = None
_dispatch_handler = None
_listener = None

# Monotonic scene version (same contract as the Rhino listener): starts at 1,
# bumped by the dispatcher after every SUCCESSFUL mutating call. spatial-core
# caches meshes/BVH keyed by (id, sceneVersion).
SCENE_VERSION = 1

# Mutating methods bump SCENE_VERSION after each successful call.
MUTATING_METHODS = set([
    "fusion.execute", "fusion.params.set", "fusion.sketch.create",
    "fusion.feature.add", "fusion.feature.edit", "fusion.rollback",
])


# --------------------------------------------------------------------------- #
# main-thread marshaling
# --------------------------------------------------------------------------- #

class _Job(object):
    __slots__ = ("fn", "done", "result", "error")

    def __init__(self, fn):
        self.fn = fn
        self.done = threading.Event()
        self.result = None
        self.error = None


_jobs = {}
_jobs_lock = threading.Lock()
_next_job_id = [1]


class _DispatchHandler(adsk.core.CustomEventHandler):
    """Runs parked jobs on the Fusion main thread."""

    def __init__(self):
        super(_DispatchHandler, self).__init__()

    def notify(self, args):
        job_id = args.additionalInfo
        with _jobs_lock:
            job = _jobs.pop(job_id, None)
        if job is None:
            return
        try:
            job.result = job.fn()
        except Exception:
            job.error = traceback.format_exc()
        finally:
            job.done.set()


def run_on_main(fn, timeout=MAIN_TIMEOUT):
    """Execute fn() on the Fusion main thread and return its result."""
    if _app is None or _custom_event is None:
        raise RuntimeError("FusionSpatialMCP is not fully started.")
    job = _Job(fn)
    with _jobs_lock:
        job_id = str(_next_job_id[0])
        _next_job_id[0] += 1
        _jobs[job_id] = job
    _app.fireCustomEvent(EVENT_ID, job_id)
    if not job.done.wait(timeout):
        with _jobs_lock:
            _jobs.pop(job_id, None)
        raise RuntimeError(
            "Timed out waiting for the Fusion main thread. Fusion may be busy "
            "or a modal dialog may be open on screen.")
    if job.error:
        raise RuntimeError(job.error)
    return job.result


# --------------------------------------------------------------------------- #
# units & design helpers (Fusion API is ALWAYS internal centimeters)
# --------------------------------------------------------------------------- #

def _design():
    des = adsk.fusion.Design.cast(_app.activeProduct)
    if des is None:
        raise RuntimeError(
            "No active Fusion design. Open (or switch to) a design document.")
    return des


def _unit_factor(des):
    """(unit_string, cm_to_unit_factor) for the design's default length unit.

    Lengths scale by f, areas by f**2, volumes by f**3.
    e.g. mm -> f=10, in -> f=1/2.54.
    """
    um = des.unitsManager
    unit = um.defaultLengthUnits  # "mm", "cm", "m", "in", "ft"
    f = um.convert(1.0, "cm", unit)
    return unit, f


def _up_axis():
    """"y" or "z". The API exposes no per-document orientation, so this
    reports the modeling-orientation preference (what new docs use and what
    the ViewCube treats as up). VERIFY-LIVE at F2 against a real doc."""
    try:
        prefs = _app.preferences.generalPreferences
        y_up = adsk.core.DefaultModelingOrientations.YUpModelingOrientation
        if prefs.defaultModelingOrientation == y_up:
            return "y"
    except Exception:
        pass
    return "z"


def _round4(v):
    try:
        return round(float(v), 4)
    except Exception:
        return None


def _pt(x, y, z, f):
    return [_round4(x * f), _round4(y * f), _round4(z * f)]


def _bbox_dict(bb, f):
    return {"min": _pt(bb.minPoint.x, bb.minPoint.y, bb.minPoint.z, f),
            "max": _pt(bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z, f)}


def _iter_body_entries(des):
    """Yield (BRepBody, Occurrence|None) for root bodies and all occurrence
    body proxies. Proxies report world-space properties."""
    root = des.rootComponent
    for i in range(root.bRepBodies.count):
        yield root.bRepBodies.item(i), None
    occs = root.allOccurrences
    for i in range(occs.count):
        occ = occs.item(i)
        for j in range(occ.bRepBodies.count):
            yield occ.bRepBodies.item(j), occ


# Short stable handles: entityTokens are ~250-char strings - hostile as
# agent-facing ids. space.bodies assigns "b1", "b2", ... per token and every
# resolver accepts handle, raw token, or body name. Handles persist for the
# add-in session; sceneVersion invalidation covers mutations as usual.
_BODY_HANDLES = {}   # entityToken -> handle
_HANDLE_TOKENS = {}  # handle -> entityToken
_next_handle = [1]


def _handle_for(token):
    h = _BODY_HANDLES.get(token)
    if h is None:
        h = "b%d" % _next_handle[0]
        _next_handle[0] += 1
        _BODY_HANDLES[token] = h
        _HANDLE_TOKENS[h] = token
    return h


def _find_body(des, id_str):
    """Resolve a body by short handle, entityToken, or name; defensive about
    stale tokens after destructive edits."""
    s = str(id_str)
    s = _HANDLE_TOKENS.get(s, s)
    try:
        ents = des.findEntityByToken(s)
    except Exception:
        ents = None
    for e in (ents or []):
        b = adsk.fusion.BRepBody.cast(e)
        if b is not None:
            return b
    matches = []
    for b, _occ in _iter_body_entries(des):
        if b.name == s:
            matches.append(b)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError(
            "Body name '%s' is ambiguous (%d matches); use its entityToken "
            "from space.bodies." % (s, len(matches)))
    raise RuntimeError(
        "No body with entityToken or name '%s'. Tokens can go stale after "
        "destructive edits - call space.bodies for a fresh list." % s)


# --------------------------------------------------------------------------- #
# command handlers
# --------------------------------------------------------------------------- #

_HEALTH_NAMES = {
    0: "ok",          # HealthyFeatureHealthState
    1: "warning",     # WarningFeatureHealthState
    2: "error",       # ErrorFeatureHealthState
    3: "suppressed",  # SuppressedFeatureHealthState
    4: "rolledBack",  # RolledBackFeatureHealthState
    5: "unknown",     # UnknownFeatureHealthState
}


def cmd_fusion_document(params):
    def work():
        des = _design()
        doc = _app.activeDocument
        unit, f = _unit_factor(des)
        root = des.rootComponent

        body_count = root.bRepBodies.count
        occs = root.allOccurrences
        for i in range(occs.count):
            body_count += occs.item(i).bRepBodies.count

        parametric = des.designType == adsk.fusion.DesignTypes.ParametricDesignType
        timeline = []
        marker = None
        if parametric:
            tl = des.timeline
            marker = tl.markerPosition
            for i in range(tl.count):
                item = tl.item(i)
                entry = {"index": i, "name": item.name}
                try:
                    ent = item.entity
                    if ent is not None:
                        entry["type"] = ent.objectType.split("::")[-1]
                except Exception:
                    pass
                try:
                    if item.isSuppressed:
                        entry["suppressed"] = True
                except Exception:
                    pass
                try:
                    health = _HEALTH_NAMES.get(int(item.healthState), "unknown")
                    if health != "ok":
                        entry["health"] = health
                        msg = item.errorOrWarningMessage
                        if msg:
                            entry["message"] = msg
                except Exception:
                    pass
                timeline.append(entry)

        return {
            "document": doc.name if doc else None,
            "designType": "parametric" if parametric else "direct",
            "units": unit,
            "upAxis": _up_axis(),
            "components": des.allComponents.count,
            "bodies": body_count,
            "userParameters": des.userParameters.count,
            "timelineMarker": marker,
            "timeline": timeline,
            "sceneVersion": SCENE_VERSION,
        }

    return run_on_main(work)


def cmd_space_bodies(params):
    ids = params.get("ids")
    # scope "doc"/"all" are equivalent here; "gh" has no Fusion meaning and
    # yields an empty list rather than an error (protocol compatibility).
    scope = params.get("scope") or "all"

    def work():
        des = _design()
        unit, f = _unit_factor(des)
        bodies = []
        if scope != "gh":
            wanted = None
            if ids:
                wanted = set()
                for i in ids:
                    s = str(i)
                    wanted.add(_HANDLE_TOKENS.get(s, s))
                    wanted.add(s)
            for b, occ in _iter_body_entries(des):
                try:
                    token = b.entityToken
                    name = b.name
                    handle = _handle_for(token)
                    if wanted is not None and token not in wanted \
                            and name not in wanted and handle not in wanted:
                        continue
                    bb = b.boundingBox  # world-aligned (proxies: world space)
                    solid = bool(b.isSolid)
                    volume = _round4(b.volume * f ** 3) if solid else None
                    area = None
                    try:
                        area = _round4(b.area * f ** 2)
                    except Exception:
                        pass
                    centroid = None
                    try:
                        com = b.physicalProperties.centerOfMass
                        centroid = _pt(com.x, com.y, com.z, f)
                    except Exception:
                        pass
                    # "layer" carries the assembly context (occurrence path,
                    # or owning component for root bodies) - Fusion's nearest
                    # organizational analogue.
                    if occ is not None:
                        where = occ.fullPathName
                    else:
                        where = b.parentComponent.name
                    bodies.append({
                        "id": handle,
                        "name": name or None,
                        "source": "doc",
                        "kind": "solid" if solid else "surface",
                        "bbox": _bbox_dict(bb, f),
                        "volume": volume,
                        "area": area,
                        "centroid": centroid,
                        "itemCount": None,
                        "layer": where,
                    })
                except Exception:
                    continue
        return {"units": unit,
                "upAxis": _up_axis(),
                "sceneVersion": SCENE_VERSION,
                "bodies": bodies}

    return run_on_main(work)


def _quality_for(density):
    Q = adsk.fusion.TriangleMeshQualityOptions
    if density < 0.25:
        return Q.LowQualityTriangleMesh
    if density < 0.6:
        return Q.NormalQualityTriangleMesh
    if density < 0.85:
        return Q.HighQualityTriangleMesh
    return Q.VeryHighQualityTriangleMesh


def _apply_matrix(coords, m):
    """Apply a Matrix3D (16 doubles, row-major) to a flat [x,y,z,...] list."""
    out = [0.0] * len(coords)
    m00, m01, m02, m03 = m[0], m[1], m[2], m[3]
    m10, m11, m12, m13 = m[4], m[5], m[6], m[7]
    m20, m21, m22, m23 = m[8], m[9], m[10], m[11]
    for i in range(0, len(coords), 3):
        x, y, z = coords[i], coords[i + 1], coords[i + 2]
        out[i] = m00 * x + m01 * y + m02 * z + m03
        out[i + 1] = m10 * x + m11 * y + m12 * z + m13
        out[i + 2] = m20 * x + m21 * y + m22 * z + m23
    return out


def _mesh_bbox_matches(coords, bb, tol):
    """True if the mesh's bbox center is within tol of the body's world bbox
    center - the invariant that decides whether proxy meshes still need the
    occurrence transform applied."""
    if not coords:
        return True
    n = len(coords) // 3
    cx = sum(coords[0::3]) / n
    cy = sum(coords[1::3]) / n
    cz = sum(coords[2::3]) / n
    bx = (bb.minPoint.x + bb.maxPoint.x) / 2.0
    by = (bb.minPoint.y + bb.maxPoint.y) / 2.0
    bz = (bb.minPoint.z + bb.maxPoint.z) / 2.0
    return math.sqrt((cx - bx) ** 2 + (cy - by) ** 2 + (cz - bz) ** 2) <= tol


def cmd_space_tessellate(params):
    id_str = params.get("id")
    if not id_str:
        raise RuntimeError("An 'id' (entityToken or body name) is required.")
    density = params.get("density")
    density = 0.5 if density is None else float(density)

    def work():
        des = _design()
        unit, f = _unit_factor(des)
        body = _find_body(des, id_str)

        calc = body.meshManager.createMeshCalculator()
        calc.setQuality(_quality_for(density))
        tri = calc.calculate()
        if tri is None:
            raise RuntimeError("Tessellation failed for '%s'." % id_str)
        coords = list(tri.nodeCoordinatesAsFloat)  # flat [x,y,z,...] in cm
        indices = list(tri.nodeIndices)
        if not coords or not indices:
            raise RuntimeError("Body '%s' produced no triangles." % id_str)

        # Occurrence proxies: the TriangleMesh may come back in the body's
        # component-local frame. Detect against the proxy's world bbox and
        # apply the occurrence's world transform only when actually needed.
        bb = body.boundingBox
        diag = math.sqrt(
            (bb.maxPoint.x - bb.minPoint.x) ** 2 +
            (bb.maxPoint.y - bb.minPoint.y) ** 2 +
            (bb.maxPoint.z - bb.minPoint.z) ** 2)
        occ = body.assemblyContext
        if occ is not None and not _mesh_bbox_matches(coords, bb, diag * 0.25 + 1e-6):
            coords = _apply_matrix(coords, occ.transform.asArray())
            if not _mesh_bbox_matches(coords, bb, diag * 0.25 + 1e-6):
                _log("WARNING: tessellation of '%s' does not line up with its "
                     "world bbox even after the occurrence transform." % id_str)

        if f != 1.0:  # cm -> document units
            coords = [c * f for c in coords]

        verts = array("f", coords)      # little-endian float32 on Windows/x64
        idx = array("i", indices)       # little-endian int32
        return {
            "vertices_b64": base64.b64encode(verts.tobytes()).decode("ascii"),
            "indices_b64": base64.b64encode(idx.tobytes()).decode("ascii"),
            "vertexCount": len(coords) // 3,
            "triangleCount": len(indices) // 3,
            "toleranceEstimate": _round4(diag * f * 0.002) or 0,
            "units": unit,
            "sceneVersion": SCENE_VERSION,
        }

    return run_on_main(work)


# --------------------------------------------------------------------------- #
# Phase-B authoring: parameters, sketches, features, timeline
# --------------------------------------------------------------------------- #

_SKETCHES = {}      # sketch handle "s1" -> sketch entityToken
_SKETCH_KEYS = {}   # (sketch_handle, entity_key) -> entityToken
_next_sketch = [1]


def _expr_of(spec, des):
    """Expression string from a dimension/extent spec: number (doc units),
    expression string ("40 mm", "width/2"), or {"param": name, "value": expr}
    which creates the named user parameter on the fly (models come out
    parametric by default)."""
    if isinstance(spec, dict):
        name = spec.get("param")
        val = spec.get("value")
        if not name:
            raise RuntimeError("param spec needs a 'param' name.")
        ups = des.userParameters
        if ups.itemByName(name) is None:
            unit = des.unitsManager.defaultLengthUnits
            ups.add(name, adsk.core.ValueInput.createByString(str(val)),
                    unit, "")
        return str(name)
    return str(spec)


def _vi(spec, des):
    """ValueInput from a spec (see _expr_of). Strings parse in doc units."""
    return adsk.core.ValueInput.createByString(_expr_of(spec, des))


def _plane_of(des, spec):
    root = des.rootComponent
    named = {"xy": root.xYConstructionPlane,
             "xz": root.xZConstructionPlane,
             "yz": root.yZConstructionPlane}
    s = str(spec or "xy").lower()
    if s in named:
        return named[s]
    ents = des.findEntityByToken(str(spec))
    for e in (ents or []):
        face = adsk.fusion.BRepFace.cast(e)
        if face is not None:
            return face
        cp = adsk.fusion.ConstructionPlane.cast(e)
        if cp is not None:
            return cp
    raise RuntimeError(
        "Unknown sketch plane '%s' (use 'xy'/'xz'/'yz' or a planar face "
        "entityToken from fusion_get_selection)." % spec)


def _axis_of(des, spec):
    root = des.rootComponent
    named = {"x": root.xConstructionAxis,
             "y": root.yConstructionAxis,
             "z": root.zConstructionAxis}
    s = str(spec).lower()
    if s in named:
        return named[s]
    ents = des.findEntityByToken(str(spec))
    for e in (ents or []):
        for t in (adsk.fusion.ConstructionAxis, adsk.fusion.SketchLine,
                  adsk.fusion.BRepEdge):
            c = t.cast(e)
            if c is not None:
                return c
    raise RuntimeError("Unknown axis '%s' (use 'x'/'y'/'z' or an entityToken "
                       "of an edge/sketch line/construction axis)." % spec)


def _find_sketch(des, handle):
    token = _SKETCHES.get(str(handle), str(handle))
    ents = des.findEntityByToken(token)
    for e in (ents or []):
        sk = adsk.fusion.Sketch.cast(e)
        if sk is not None:
            return sk
    raise RuntimeError("Unknown sketch '%s'. Known: %s" %
                       (handle, ", ".join(sorted(_SKETCHES.keys())) or "(none)"))


def _profiles_of(des, spec):
    """Resolve 'pN' profile refs: "s1" (all profiles) or "s1:0"."""
    s = str(spec)
    idx = None
    if ":" in s:
        s, i = s.rsplit(":", 1)
        idx = int(i)
    sk = _find_sketch(des, s)
    profs = sk.profiles
    if profs.count == 0:
        raise RuntimeError("Sketch '%s' has no closed profiles." % spec)
    if idx is not None:
        if idx >= profs.count:
            raise RuntimeError("Sketch '%s' has %d profiles; index %d is out "
                               "of range." % (s, profs.count, idx))
        return [profs.item(idx)]
    return [profs.item(i) for i in range(profs.count)]


def _find_feature(des, spec):
    """Resolve a timeline feature by name or index."""
    tl = des.timeline
    s = str(spec)
    if s.lstrip("-").isdigit():
        i = int(s)
        if i < 0:
            i += tl.count
        return tl.item(i)
    for i in range(tl.count):
        if tl.item(i).name == s:
            return tl.item(i)
    raise RuntimeError("No timeline feature named '%s'. Call fusion_timeline "
                       "for the current list." % s)


def _collection(items):
    col = adsk.core.ObjectCollection.create()
    for it in items:
        col.add(it)
    return col


def _health(des):
    """Compact post-mutation health report: unhealthy timeline items only."""
    issues = []
    try:
        if des.designType == adsk.fusion.DesignTypes.ParametricDesignType:
            tl = des.timeline
            for i in range(tl.count):
                item = tl.item(i)
                try:
                    h = _HEALTH_NAMES.get(int(item.healthState), "unknown")
                except Exception:
                    continue
                if h in ("warning", "error"):
                    entry = {"feature": item.name, "health": h}
                    try:
                        msg = item.errorOrWarningMessage
                        if msg:
                            entry["message"] = msg
                    except Exception:
                        pass
                    issues.append(entry)
    except Exception:
        pass
    return {"timelineHealth": issues if issues else "ok",
            "sceneVersion": SCENE_VERSION + 1}  # +1: dispatcher bumps after


def _body_handles(feature):
    out = []
    try:
        for i in range(feature.bodies.count):
            out.append(_handle_for(feature.bodies.item(i).entityToken))
    except Exception:
        pass
    return out


def _param_info(p, um):
    entry = {"name": p.name, "expression": p.expression, "unit": p.unit}
    try:
        entry["value"] = um.formatInternalValue(p.value, p.unit, False) \
            if p.unit else _round4(p.value)
    except Exception:
        pass
    try:
        if p.comment:
            entry["comment"] = p.comment
    except Exception:
        pass
    return entry


def cmd_fusion_params_list(params):
    include_model = bool(params.get("includeModel"))

    def work():
        des = _design()
        um = des.unitsManager
        out = {"userParameters": [_param_info(des.userParameters.item(i), um)
                                  for i in range(des.userParameters.count)]}
        if include_model:
            model = []
            aps = des.allParameters
            for i in range(aps.count):
                p = aps.item(i)
                mp = adsk.fusion.ModelParameter.cast(p)
                if mp is None:
                    continue
                entry = _param_info(p, um)
                try:
                    entry["role"] = mp.role
                    entry["feature"] = mp.createdBy.name
                except Exception:
                    pass
                model.append(entry)
            out["modelParameters"] = model
        return out

    return run_on_main(work)


def cmd_fusion_params_set(params):
    values = params.get("params")
    if not isinstance(values, dict) or not values:
        raise RuntimeError("'params' must be a non-empty {name: expression} "
                           "object.")

    def work():
        des = _design()
        um = des.unitsManager
        updated = []
        for name, expr in values.items():
            p = des.userParameters.itemByName(str(name))
            if p is None:
                p = des.allParameters.itemByName(str(name))
            if p is None:
                # unknown name -> create a user parameter (parametric by
                # default); use an explicit unit-suffixed expression to
                # control the unit.
                p = des.userParameters.add(
                    str(name),
                    adsk.core.ValueInput.createByString(str(expr)),
                    um.defaultLengthUnits, "")
            else:
                p.expression = str(expr)
            updated.append(_param_info(p, um))
        r = {"updated": updated}
        r.update(_health(des))
        return r

    return run_on_main(work)


def _sketch_ref(sk, made, ref):
    """Resolve an entity reference inside a sketch.create call:
    'origin' | key | key.start/.end/.center | rectKey.N[.start/.end]"""
    s = str(ref)
    if s == "origin":
        return sk.originPoint
    parts = s.split(".")
    obj = made.get(parts[0])
    if obj is None:
        raise RuntimeError("Unknown entity key '%s'. Known: %s" %
                           (parts[0], ", ".join(sorted(made.keys()))))
    for part in parts[1:]:
        if part.isdigit():          # rectangle line index
            obj = obj[int(part)]
        elif part == "start":
            obj = obj.startSketchPoint
        elif part == "end":
            obj = obj.endSketchPoint
        elif part == "center":
            obj = obj.centerSketchPoint
        else:
            raise RuntimeError("Unknown entity suffix '.%s' in '%s'." %
                               (part, s))
    return obj


def cmd_fusion_sketch_create(params):
    plane = params.get("plane", "xy")
    name = params.get("name")
    entities = params.get("entities") or []
    dimensions = params.get("dimensions") or []
    constraints = params.get("constraints") or []

    def work():
        des = _design()
        unit, f = _unit_factor(des)
        inv = 1.0 / f  # doc units -> cm
        root = des.rootComponent
        sk = root.sketches.add(_plane_of(des, plane))
        if name:
            sk.name = str(name)

        P = adsk.core.Point3D.create

        def pt(xy):
            return P(float(xy[0]) * inv, float(xy[1]) * inv, 0)

        made = {}
        auto = [0]

        def keyed(spec, obj):
            k = spec.get("key")
            if not k:
                auto[0] += 1
                k = "e%d" % auto[0]
            made[str(k)] = obj
            return str(k)

        curves = sk.sketchCurves
        for e in entities:
            t = str(e.get("type", ""))
            if t == "line":
                obj = curves.sketchLines.addByTwoPoints(
                    pt(e["from"]), pt(e["to"]))
            elif t == "rect":
                lines = curves.sketchLines.addTwoPointRectangle(
                    pt(e["corner1"]), pt(e["corner2"]))
                obj = [lines.item(i) for i in range(lines.count)]
            elif t == "circle":
                obj = curves.sketchCircles.addByCenterRadius(
                    pt(e["center"]), float(e["radius"]) * inv)
            elif t == "arc":
                obj = curves.sketchArcs.addByThreePoints(
                    pt(e["from"]), pt(e["through"]), pt(e["to"]))
            elif t == "point":
                obj = sk.sketchPoints.add(pt(e["at"]))
            else:
                raise RuntimeError(
                    "Unknown entity type '%s' (line|rect|circle|arc|point)."
                    % t)
            keyed(e, obj)

        # constraints first (they position geometry), then dimensions
        gc = sk.geometricConstraints
        for c in constraints:
            k = str(c.get("kind", ""))
            if k == "horizontal":
                gc.addHorizontal(_sketch_ref(sk, made, c["of"]))
            elif k == "vertical":
                gc.addVertical(_sketch_ref(sk, made, c["of"]))
            elif k == "coincident":
                gc.addCoincident(_sketch_ref(sk, made, c["a"]),
                                 _sketch_ref(sk, made, c["b"]))
            elif k == "concentric":
                gc.addConcentric(_sketch_ref(sk, made, c["a"]),
                                 _sketch_ref(sk, made, c["b"]))
            elif k == "equal":
                gc.addEqual(_sketch_ref(sk, made, c["a"]),
                            _sketch_ref(sk, made, c["b"]))
            elif k == "tangent":
                gc.addTangent(_sketch_ref(sk, made, c["a"]),
                              _sketch_ref(sk, made, c["b"]))
            elif k == "parallel":
                gc.addParallel(_sketch_ref(sk, made, c["a"]),
                               _sketch_ref(sk, made, c["b"]))
            elif k == "perpendicular":
                gc.addPerpendicular(_sketch_ref(sk, made, c["a"]),
                                    _sketch_ref(sk, made, c["b"]))
            elif k == "midpoint":
                gc.addMidPoint(_sketch_ref(sk, made, c["point"]),
                               _sketch_ref(sk, made, c["line"]))
            else:
                raise RuntimeError("Unknown constraint kind '%s'." % k)

        dims = sk.sketchDimensions
        DO = adsk.fusion.DimensionOrientations
        orient_map = {"horizontal": DO.HorizontalDimensionOrientation,
                      "vertical": DO.VerticalDimensionOrientation,
                      "aligned": DO.AlignedDimensionOrientation}
        text_off = [0]
        for d in dimensions:
            k = str(d.get("kind", ""))
            text_off[0] += 1.0
            tp = P(text_off[0], -1.0 - text_off[0] * 0.5, 0)
            if k == "distance":
                if "of" in d:  # length of a line
                    line = _sketch_ref(sk, made, d["of"])
                    a, b = line.startSketchPoint, line.endSketchPoint
                else:
                    a = _sketch_ref(sk, made, d["a"])
                    b = _sketch_ref(sk, made, d["b"])
                orient = orient_map.get(str(d.get("orientation", "aligned")),
                                        DO.AlignedDimensionOrientation)
                dim = dims.addDistanceDimension(a, b, orient, tp)
            elif k == "diameter":
                dim = dims.addDiameterDimension(
                    _sketch_ref(sk, made, d["of"]), tp)
            elif k == "radius":
                dim = dims.addRadialDimension(
                    _sketch_ref(sk, made, d["of"]), tp)
            else:
                raise RuntimeError(
                    "Unknown dimension kind '%s' (distance|diameter|radius)."
                    % k)
            if d.get("value") is not None:
                dim.parameter.expression = _expr_of(d["value"], des)

        # register handle + keyed entity tokens for later feature calls
        h = "s%d" % _next_sketch[0]
        _next_sketch[0] += 1
        _SKETCHES[h] = sk.entityToken
        for k, obj in made.items():
            try:
                if isinstance(obj, list):
                    for i, o in enumerate(obj):
                        _SKETCH_KEYS[(h, "%s.%d" % (k, i))] = o.entityToken
                else:
                    _SKETCH_KEYS[(h, k)] = obj.entityToken
            except Exception:
                pass

        profs = sk.profiles
        profiles = []
        for i in range(profs.count):
            entry = {"id": "%s:%d" % (h, i)}
            try:
                entry["area"] = _round4(
                    profs.item(i).areaProperties().area * f ** 2)
            except Exception:
                pass
            profiles.append(entry)
        r = {"sketch": h, "name": sk.name,
             "fullyConstrained": bool(sk.isFullyConstrained),
             "profiles": profiles,
             "keys": sorted(made.keys())}
        r.update(_health(des))
        return r

    return run_on_main(work)


_OPERATIONS = {
    "new": "NewBodyFeatureOperation",
    "join": "JoinFeatureOperation",
    "cut": "CutFeatureOperation",
    "intersect": "IntersectFeatureOperation",
    "newComponent": "NewComponentFeatureOperation",
}


def _operation_of(spec):
    FO = adsk.fusion.FeatureOperations
    key = _OPERATIONS.get(str(spec or "new"))
    if key is None:
        raise RuntimeError("Unknown operation '%s' (%s)." %
                           (spec, "|".join(sorted(_OPERATIONS))))
    return getattr(FO, key)


def _edges_of(des, spec):
    """Edge set for fillet/chamfer: list of entityTokens, or a filter
    {"body": handle, "parallelTo": "x|y|z"} / {"body": handle} (all edges)."""
    if isinstance(spec, list):
        out = []
        for token in spec:
            ents = des.findEntityByToken(str(token))
            for e in (ents or []):
                edge = adsk.fusion.BRepEdge.cast(e)
                if edge is not None:
                    out.append(edge)
                    break
        if not out:
            raise RuntimeError("No edges resolved from the given tokens.")
        return out
    if isinstance(spec, dict) and spec.get("body"):
        body = _find_body(des, spec["body"])
        axis = spec.get("parallelTo")
        out = []
        for i in range(body.edges.count):
            edge = body.edges.item(i)
            if axis:
                line = adsk.core.Line3D.cast(edge.geometry)
                if line is None:
                    continue
                v = line.startPoint.vectorTo(line.endPoint)
                v.normalize()
                want = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[
                    str(axis).lower()]
                dot = abs(v.x * want[0] + v.y * want[1] + v.z * want[2])
                if dot < 0.999:
                    continue
            out.append(edge)
        if not out:
            raise RuntimeError("No edges of body '%s' matched the filter."
                               % spec["body"])
        return out
    raise RuntimeError("'edges' must be a token list or "
                       "{'body': handle, 'parallelTo': 'x|y|z'}.")


def _feature_entities(des, params):
    """Bodies and/or features referenced by a pattern/mirror call."""
    items = []
    for h in (params.get("bodies") or []):
        items.append(_find_body(des, h))
    for nm in (params.get("features") or []):
        items.append(_find_feature(des, nm).entity)
    if not items:
        raise RuntimeError("Provide 'bodies' (handles) and/or 'features' "
                           "(timeline names).")
    return items


def cmd_fusion_feature_add(params):
    ftype = str(params.get("type", ""))

    def work():
        des = _design()
        root = des.rootComponent
        feats = root.features
        op = _operation_of(params.get("operation"))

        if ftype == "extrude":
            profs = _collection(_profiles_of(des, params.get("profile")))
            inp = feats.extrudeFeatures.createInput(profs, op)
            inp.setDistanceExtent(bool(params.get("symmetric")),
                                  _vi(params.get("distance"), des))
            feature = feats.extrudeFeatures.add(inp)
        elif ftype == "revolve":
            profs = _collection(_profiles_of(des, params.get("profile")))
            inp = feats.revolveFeatures.createInput(
                profs, _axis_of(des, params.get("axis", "z")), op)
            inp.setAngleExtent(bool(params.get("symmetric")),
                               _vi(params.get("angle", "360 deg"), des))
            feature = feats.revolveFeatures.add(inp)
        elif ftype == "hole":
            sk_h = params.get("sketch")
            keys = params.get("points") or []
            pts = []
            for k in keys:
                token = _SKETCH_KEYS.get((str(sk_h), str(k)))
                if token is None:
                    raise RuntimeError(
                        "Unknown sketch point '%s' in sketch '%s'." %
                        (k, sk_h))
                for e in (des.findEntityByToken(token) or []):
                    sp = adsk.fusion.SketchPoint.cast(e)
                    if sp is not None:
                        pts.append(sp)
                        break
            if not pts:
                raise RuntimeError("Provide 'sketch' (handle) and 'points' "
                                   "(keys of sketch points).")
            inp = feats.holeFeatures.createSimpleInput(
                _vi(params.get("diameter"), des))
            inp.setPositionBySketchPoints(_collection(pts))
            depth = params.get("depth", "through")
            if str(depth) == "through":
                inp.setAllExtent(
                    adsk.fusion.ExtentDirections.NegativeExtentDirection)
            else:
                inp.setDistanceExtent(_vi(depth, des))
            feature = feats.holeFeatures.add(inp)
        elif ftype == "fillet":
            edges = _collection(_edges_of(des, params.get("edges")))
            inp = feats.filletFeatures.createInput()
            inp.edgeSetInputs.addConstantRadiusEdgeSet(
                edges, _vi(params.get("radius"), des), True)
            feature = feats.filletFeatures.add(inp)
        elif ftype == "chamfer":
            edges = _collection(_edges_of(des, params.get("edges")))
            inp = feats.chamferFeatures.createInput(edges, True)
            inp.setToEqualDistance(_vi(params.get("distance"), des))
            feature = feats.chamferFeatures.add(inp)
        elif ftype == "shell":
            body = _find_body(des, params.get("body"))
            faces = []
            for token in (params.get("removeFaces") or []):
                for e in (des.findEntityByToken(str(token)) or []):
                    fc = adsk.fusion.BRepFace.cast(e)
                    if fc is not None:
                        faces.append(fc)
                        break
            inp = feats.shellFeatures.createInput(
                _collection(faces if faces else [body]), False)
            inp.insideThickness = _vi(params.get("thickness"), des)
            feature = feats.shellFeatures.add(inp)
        elif ftype == "rectangularPattern":
            ents = _collection(_feature_entities(des, params))
            PDT = adsk.fusion.PatternDistanceTypes
            inp = feats.rectangularPatternFeatures.createInput(
                ents, _axis_of(des, params.get("axisOne", "x")),
                _vi(params.get("countOne", 2), des),
                _vi(params.get("spacingOne"), des),
                PDT.SpacingPatternDistanceType)
            if params.get("axisTwo"):
                inp.setDirectionTwo(
                    _axis_of(des, params.get("axisTwo")),
                    _vi(params.get("countTwo", 2), des),
                    _vi(params.get("spacingTwo"), des))
            feature = feats.rectangularPatternFeatures.add(inp)
        elif ftype == "circularPattern":
            ents = _collection(_feature_entities(des, params))
            inp = feats.circularPatternFeatures.createInput(
                ents, _axis_of(des, params.get("axis", "z")))
            inp.quantity = _vi(params.get("count", 4), des)
            inp.totalAngle = _vi(params.get("totalAngle", "360 deg"), des)
            feature = feats.circularPatternFeatures.add(inp)
        elif ftype == "mirror":
            ents = _collection(_feature_entities(des, params))
            inp = feats.mirrorFeatures.createInput(
                ents, _plane_of(des, params.get("plane")))
            feature = feats.mirrorFeatures.add(inp)
        elif ftype == "combine":
            target = _find_body(des, params.get("target"))
            tools = _collection(
                [_find_body(des, h) for h in (params.get("tools") or [])])
            inp = feats.combineFeatures.createInput(target, tools)
            inp.operation = _operation_of(params.get("operation", "join"))
            inp.isKeepToolBodies = bool(params.get("keepTools"))
            feature = feats.combineFeatures.add(inp)
        else:
            raise RuntimeError(
                "Unknown feature type '%s' (extrude|revolve|hole|fillet|"
                "chamfer|shell|rectangularPattern|circularPattern|mirror|"
                "combine)." % ftype)

        if params.get("name"):
            try:
                feature.name = str(params["name"])
            except Exception:
                pass
        r = {"feature": feature.name,
             "type": ftype,
             "bodies": _body_handles(feature)}
        r.update(_health(des))
        return r

    return run_on_main(work)


def cmd_fusion_feature_edit(params):
    spec = params.get("feature")

    def work():
        des = _design()
        um = des.unitsManager
        item = _find_feature(des, spec)
        feature = item.entity
        changed = []
        sets = params.get("set") or {}
        if sets:
            # a feature's dimensions are its model parameters; editing in
            # Fusion = driving parameters (match by role or name)
            mine = []
            aps = des.allParameters
            for i in range(aps.count):
                mp = adsk.fusion.ModelParameter.cast(aps.item(i))
                if mp is not None and mp.createdBy is not None \
                        and mp.createdBy == feature:
                    mine.append(mp)
            for key, expr in sets.items():
                k = str(key).lower()
                match = None
                for mp in mine:
                    if mp.name.lower() == k or (mp.role or "").lower() == k:
                        match = mp
                        break
                if match is None:
                    raise RuntimeError(
                        "Feature '%s' has no parameter '%s'. Available: %s"
                        % (item.name, key,
                           ", ".join("%s (%s)" % (m.role or m.name, m.name)
                                     for m in mine) or "(none)"))
                match.expression = _expr_of(expr, des)
                changed.append(_param_info(match, um))
        if params.get("suppress") is not None:
            item.isSuppressed = bool(params["suppress"])
        if params.get("name") and params["name"] != item.name:
            feature.name = str(params["name"])
        r = {"feature": item.name, "updated": changed}
        r.update(_health(des))
        return r

    return run_on_main(work)


def cmd_fusion_timeline(params):
    def work():
        des = _design()
        um = des.unitsManager
        if des.designType != adsk.fusion.DesignTypes.ParametricDesignType:
            return {"designType": "direct", "timeline": []}
        # group model parameters by owning feature (one pass)
        by_feature = {}
        aps = des.allParameters
        for i in range(aps.count):
            mp = adsk.fusion.ModelParameter.cast(aps.item(i))
            if mp is None or mp.createdBy is None:
                continue
            try:
                entry = {"name": mp.name, "role": mp.role,
                         "expression": mp.expression}
                by_feature.setdefault(mp.createdBy.name, []).append(entry)
            except Exception:
                continue
        tl = des.timeline
        items = []
        for i in range(tl.count):
            item = tl.item(i)
            entry = {"index": i, "name": item.name}
            try:
                ent = item.entity
                if ent is not None:
                    entry["type"] = ent.objectType.split("::")[-1]
            except Exception:
                pass
            try:
                if item.isSuppressed:
                    entry["suppressed"] = True
            except Exception:
                pass
            try:
                h = _HEALTH_NAMES.get(int(item.healthState), "unknown")
                if h != "ok":
                    entry["health"] = h
                    msg = item.errorOrWarningMessage
                    if msg:
                        entry["message"] = msg
            except Exception:
                pass
            if item.name in by_feature:
                entry["parameters"] = by_feature[item.name]
            items.append(entry)
        return {"timelineMarker": tl.markerPosition,
                "count": tl.count,
                "timeline": items,
                "sceneVersion": SCENE_VERSION}

    return run_on_main(work)


def cmd_fusion_rollback(params):
    to = params.get("to", "end")

    def work():
        des = _design()
        tl = des.timeline
        if str(to) == "end":
            tl.moveToEnd()
        elif str(to) == "start":
            tl.markerPosition = 0
        else:
            item = _find_feature(des, to)
            tl.markerPosition = item.index + 1
        r = {"timelineMarker": tl.markerPosition, "count": tl.count}
        r.update(_health(des))
        return r

    return run_on_main(work)


def cmd_fusion_selection(params):
    """entityTokens/handles + kinds + bboxes of the user's active selections -
    resolves 'this face/body/feature'."""
    def work():
        des = _design()
        unit, f = _unit_factor(des)
        items = []
        sels = _ui.activeSelections
        for i in range(sels.count):
            try:
                ent = sels.item(i).entity
                entry = {"kind": ent.objectType.split("::")[-1]}
                try:
                    entry["name"] = ent.name
                except Exception:
                    pass
                try:
                    token = ent.entityToken
                    entry["id"] = _handle_for(token) \
                        if adsk.fusion.BRepBody.cast(ent) else token
                except Exception:
                    pass
                try:
                    bb = ent.boundingBox
                    if bb is not None:
                        entry["bbox"] = _bbox_dict(bb, f)
                except Exception:
                    pass
                # owning body for faces/edges/vertices
                try:
                    body = ent.body
                    if body is not None:
                        entry["body"] = _handle_for(body.entityToken)
                        entry["bodyName"] = body.name
                except Exception:
                    pass
                items.append(entry)
            except Exception:
                continue
        return {"count": len(items), "units": unit, "items": items}

    return run_on_main(work)


def cmd_fusion_capture(params):
    """Viewport screenshot -> PNG (base64)."""
    width = int(params.get("width") or 800)
    height = int(params.get("height") or 600)

    def work():
        import os
        import tempfile
        vp = _app.activeViewport
        if vp is None:
            raise RuntimeError("No active viewport.")
        path = os.path.join(tempfile.gettempdir(),
                            "FusionSpatialMCP_capture.png")
        if not vp.saveAsImageFile(path, width, height):
            raise RuntimeError("Viewport capture failed.")
        with open(path, "rb") as fh:
            data = fh.read()
        try:
            os.remove(path)
        except OSError:
            pass
        return {"png_b64": base64.b64encode(data).decode("ascii"),
                "width": width, "height": height}

    return run_on_main(work)


def cmd_fusion_execute(params):
    """Escape hatch: run Python on the Fusion main thread. Globals: adsk,
    app, ui. Assign to a variable named `result` to return a value; stdout
    is captured."""
    code = params.get("code")
    if not code:
        raise RuntimeError("A 'code' string is required.")

    def work():
        import io
        import contextlib
        env = {"adsk": adsk, "app": _app, "ui": _ui}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(code, env)
        result = env.get("result")
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            result = repr(result)
        return {"result": result, "stdout": buf.getvalue()[-8000:]}

    return run_on_main(work)


HANDLERS = {
    # "ping" is answered on the socket thread (dispatcher), no API needed.
    "fusion.document": cmd_fusion_document,
    "fusion.selection": cmd_fusion_selection,
    "fusion.capture": cmd_fusion_capture,
    "fusion.execute": cmd_fusion_execute,
    "fusion.params.list": cmd_fusion_params_list,
    "fusion.params.set": cmd_fusion_params_set,
    "fusion.sketch.create": cmd_fusion_sketch_create,
    "fusion.feature.add": cmd_fusion_feature_add,
    "fusion.feature.edit": cmd_fusion_feature_edit,
    "fusion.timeline": cmd_fusion_timeline,
    "fusion.rollback": cmd_fusion_rollback,
    "space.bodies": cmd_space_bodies,
    "space.tessellate": cmd_space_tessellate,
}


# --------------------------------------------------------------------------- #
# TCP server (JSON lines, thread-per-connection)
# --------------------------------------------------------------------------- #

def _log(msg):
    try:
        _app.log("FusionSpatialMCP: %s" % msg)
    except Exception:
        pass


class Listener(object):
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.stop_flag = False
        self.sock = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Deliberately NO SO_REUSEADDR: on Windows it lets a new socket bind
        # alongside a zombie listener and connections silently land in the
        # zombie's queue. Without it a zombie makes bind() fail loudly.
        try:
            self.sock.bind((self.host, self.port))
        except Exception:
            raise RuntimeError(
                "Port %d is stuck (a previous listener socket was not "
                "released). Close and reopen Fusion to clear it." % self.port)
        self.sock.listen(4)
        t = threading.Thread(target=self._accept_loop)
        t.daemon = True
        t.start()

    def shutdown(self):
        self.stop_flag = True
        try:
            self.sock.close()
        except Exception:
            pass

    def _accept_loop(self):
        while not self.stop_flag:
            try:
                conn, _ = self.sock.accept()
            except Exception:
                break
            # Each client gets its own thread so the MCP server's persistent
            # connection never blocks others; all Fusion work still serializes
            # at the main-thread boundary via run_on_main.
            t = threading.Thread(target=self._serve_safe, args=(conn,))
            t.daemon = True
            t.start()

    def _serve_safe(self, conn):
        try:
            self._serve(conn)
        except Exception:
            pass

    def _serve(self, conn):
        buf = b""
        try:
            while not self.stop_flag:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    reply = self._handle(line.decode("utf-8", "replace"))
                    if reply is not None:
                        conn.sendall((reply + "\n").encode("utf-8"))
                    if self.stop_flag:
                        return
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _handle(self, payload):
        global SCENE_VERSION
        try:
            req = json.loads(payload)
        except Exception:
            return json.dumps({"id": None,
                               "error": {"message": "invalid JSON request"}})
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if method == "ping":
            return json.dumps({"id": rid, "result": "pong"})
        if method == "sys.shutdown":
            self.shutdown()
            return json.dumps({"id": rid, "result": "shutting down"})

        fn = HANDLERS.get(method)
        if fn is None:
            return json.dumps({"id": rid,
                               "error": {"message": "unknown method '%s'" % method}})
        try:
            result = fn(params)
        except Exception as e:
            return json.dumps({"id": rid,
                               "error": {"message": str(e) or "unknown error",
                                         "traceback": traceback.format_exc()}})
        if method in MUTATING_METHODS:
            SCENE_VERSION += 1
        return json.dumps({"id": rid, "result": result})


def kill_previous_instance():
    """If an older listener owns the port, ask it to shut down."""
    import time
    try:
        s = socket.create_connection((HOST, PORT), 1)
        s.sendall(b'{"id":0,"method":"sys.shutdown"}\n')
        try:
            s.recv(1024)
        except Exception:
            pass
        s.close()
        time.sleep(0.6)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# add-in lifecycle
# --------------------------------------------------------------------------- #

def run(context):
    global _app, _ui, _custom_event, _dispatch_handler, _listener
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        # Clear any event left over from a previous load of this add-in.
        try:
            _app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        _custom_event = _app.registerCustomEvent(EVENT_ID)
        _dispatch_handler = _DispatchHandler()
        _custom_event.add(_dispatch_handler)

        kill_previous_instance()
        _listener = Listener(HOST, PORT)
        _listener.start()
        _log("listener running on %s:%d" % (HOST, PORT))
    except Exception:
        if _ui:
            _ui.messageBox("FusionSpatialMCP failed to start:\n\n"
                           + traceback.format_exc())


def stop(context):
    global _custom_event, _dispatch_handler, _listener
    try:
        if _listener is not None:
            _listener.shutdown()
            _listener = None
        if _custom_event is not None and _dispatch_handler is not None:
            _custom_event.remove(_dispatch_handler)
        _app.unregisterCustomEvent(EVENT_ID)
        _custom_event = None
        _dispatch_handler = None
        _log("listener stopped.")
    except Exception:
        pass

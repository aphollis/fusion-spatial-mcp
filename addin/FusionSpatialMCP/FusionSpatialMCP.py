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

# Mutating methods bump SCENE_VERSION; the rest of the Phase-B authoring
# surface joins this set at F4.
MUTATING_METHODS = set(["fusion.execute"])


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

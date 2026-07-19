# F2 conformance ground-truth scene. Sent via fusion.execute; runs on the
# Fusion main thread with adsk/app/ui preloaded. Creates a NEW direct-design
# document (never touches existing docs):
#   root bodies (cm): SphereA r=1 @ (0,0,1); SphereB r=1 @ (5,0,1);
#     HollowBox outer 4^3 minus inner 3.6^3 (0.2 walls) @ (10,0,2);
#     TallCylinder r=0.5 h=10 base z=0 @ (15,0,*)
#   component "Peg" (1x1x2 box), two occurrences @ (0,6,0) and (3,6,0)
# Expected volumes (cm^3): spheres 4.18879, box 17.344, cyl 7.85398, pegs 2.
# Returns each body's Fusion-reported volume + bbox as the ground truth that
# space.bodies must match to 4 significant figures.
import adsk.core
import adsk.fusion

app = adsk.core.Application.get()
doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
des = adsk.fusion.Design.cast(app.activeProduct)
des.designType = adsk.fusion.DesignTypes.DirectDesignType
root = des.rootComponent
tmp = adsk.fusion.TemporaryBRepManager.get()
P = adsk.core.Point3D.create
V = adsk.core.Vector3D.create


def obb(cx, cy, cz, dx, dy, dz):
    return adsk.core.OrientedBoundingBox3D.create(
        P(cx, cy, cz), V(1, 0, 0), V(0, 1, 0), dx, dy, dz)


sA = tmp.createSphere(P(0, 0, 1), 1.0)
sB = tmp.createSphere(P(5, 0, 1), 1.0)
outer = tmp.createBox(obb(10, 0, 2, 4.0, 4.0, 4.0))
inner = tmp.createBox(obb(10, 0, 2, 3.6, 3.6, 3.6))
tmp.booleanOperation(outer, inner,
                     adsk.fusion.BooleanTypes.DifferenceBooleanType)
cyl = tmp.createCylinderOrCone(P(15, 0, 0), 0.5, P(15, 0, 10), 0.5)

for name, b in [("SphereA", sA), ("SphereB", sB),
                ("HollowBox", outer), ("TallCylinder", cyl)]:
    nb = root.bRepBodies.add(b)
    nb.name = name

m1 = adsk.core.Matrix3D.create()
m1.translation = V(0, 6, 0)
peg_occ = root.occurrences.addNewComponent(m1)
peg_comp = peg_occ.component
peg_comp.name = "Peg"
pb = peg_comp.bRepBodies.add(tmp.createBox(obb(0, 0, 1, 1.0, 1.0, 2.0)))
pb.name = "PegBody"
m2 = adsk.core.Matrix3D.create()
m2.translation = V(3, 6, 0)
root.occurrences.addExistingComponent(peg_comp, m2)

app.activeViewport.fit()

gt = []
entries = [(b, None) for b in root.bRepBodies]
for occ in root.allOccurrences:
    for b in occ.bRepBodies:
        entries.append((b, occ))
for b, occ in entries:
    bb = b.boundingBox
    gt.append({
        "name": b.name,
        "where": occ.fullPathName if occ else "root",
        "token": b.entityToken,
        "volume_cm3": b.volume,
        "bbox_cm": [[bb.minPoint.x, bb.minPoint.y, bb.minPoint.z],
                    [bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z]],
    })
result = {"doc": doc.name,
          "units": des.unitsManager.defaultLengthUnits,
          "groundTruth": gt}

# Navigation generation and review

Navigation proposals are derived from available camera/reconstruction evidence and remain provisional until reviewed. Inferred boundaries are not surveyed architectural measurements.

Review the exact candidate and its evidence:

- coordinate frame and viewer transform;
- boundary algorithm, shape, margin and camera station inclusion;
- floor and ceiling classification and clearance;
- free XYZ and camera clearance policy;
- room/region identity;
- explicit portal locations and directions;
- tour or viewpoint paths and all limitations.

Use navigation validate to inspect geometric constraints and the local navigation review interface to record an approval or rejection bound to the current artifact. Proximity between rooms does not create a doorway. A changed candidate invalidates prior approval. The public viewer must use the approved reviewed navigation and omit Debug Fly.

The Engine does not estimate complete architectural planes automatically, lock visitor camera height, implement furniture collision, or treat synthetic browser fixtures as venue evidence. Route interpolation is piecewise linear; spline navigation is deferred.

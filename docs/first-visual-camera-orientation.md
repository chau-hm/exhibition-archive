# First-visual camera orientation

The first-visual Technical Reviewer embeds the same Visitor page used on its
own, so both previews must consume one initial camera pose and one model
transform. SuperSplat uses the PlayCanvas Y-up viewer frame. A COLMAP
reconstruction has an arbitrary world frame; its coordinates cannot be sent
directly to a Y-up camera unless that convention has been established.

## Pose conventions

COLMAP `images.bin` stores a world-to-camera quaternion in WXYZ order and a
world-to-camera translation. The camera center is `-Rᵀt`. Camera-local +Z is
forward, +X is image-right, and +Y points down the decoded image, so camera-up
in COLMAP world coordinates is the negative second row of `R`. The existing
first-visual start camera is selected deterministically as the registered
camera nearest the component-wise camera-center median. Its target follows
that registered camera's +Z forward vector; its FOV remains 70.75 degrees.

The viewer previously passed that raw COLMAP position and target to a camera
that implicitly used PlayCanvas +Y as up, while leaving the Gaussian entity at
identity rotation. That did not transpose the COLMAP rotation or swap
quaternion fields. It omitted the scene-frame alignment and the look-at up
axis. Reset View repeated the same pose, and vertical movement used viewer Y,
which did not correspond to the reconstruction's up direction.

## Reusable alignment and fallback

For each registered camera, the Engine derives its local-up direction from the
COLMAP world-to-camera rotation. It finds the medoid direction, aggregates
camera-ups within 25 degrees, and only applies an alignment when there are at
least eight valid poses, at least 70% consensus, and the inlier 90th-percentile
deviation is no more than 20 degrees. The shortest proper quaternion rotation
maps the inferred direction to PlayCanvas +Y. The Engine applies that same
viewer-side rotation to the Gaussian model entity and to the initial camera
position and target. The PLY and COLMAP reconstruction stay unchanged.

When evidence misses any threshold, the Engine uses an identity transform and
the raw deterministic camera pose, and marks `ORIENTATION_REVIEW_REQUIRED`.
The estimate is camera-orientation evidence only; it is not a physical-gravity
measurement, a ground-plane finding, or navigation approval. No LiDAR data,
dominant scene geometry, or fixed project-specific axis rotation is used.

The generated `initial_camera.orientation` record carries the evidence,
thresholds, proposal status, and fallback reason. The technical Reviewer
reports that status and warns that an applied estimate remains an unreviewed
proposal. Visitor and Reviewer both use the shared Viewer runtime and the same
settings pose, so Reset View returns to the corrected opening orientation.

## Nam Shan diagnosis

The first-visual output's registered pose nearest the median is
`IMG_1307.jpg`. Its raw COLMAP camera-up is approximately
`[-0.0552, 0.0588, 0.9967]`. Across the 77 registered images, the median
camera-up is within 1.8 degrees of their normalized consensus
`[-0.0214, 0.0084, 0.9997]`; the 90th-percentile deviation is under 4 degrees.
This strongly supports a +Z camera-up convention for this reconstruction and
does not rely on the rejected LiDAR alignment. Camera-center Z spans only
0.475 SfM units while X/Y span 15.6/18.2 units, a corroborating trajectory
signal rather than proof of gravity.

The selected camera center was approximately `[6.312559, -2.160161,
1.464304]`, with its two-unit forward target at `[4.955759, -0.699654,
1.303016]` and FOV 70.75 degrees. Those values came from a valid registered
camera pose; the error was interpreting them in the browser's Y-up frame while
leaving the model in the arbitrary COLMAP frame. The corrected full-model
opening view matches the upright orientation and content of `IMG_1307.jpg`.

The prepared SfM JPEGs are 5712×4284 with EXIF orientation 1. The preparation
manifest records EXIF orientation baked into pixels, output orientation 1,
and passing pixel-content checks. The camera/model pipeline therefore has no
evidence of a 90-degree HEIC conversion mismatch. Camera forward and center
conversion also match COLMAP's WXYZ/world-to-camera convention. The defect is
the unaligned +Z reconstruction frame being viewed with the browser's implicit
+Y up vector.

Spirula Studio's upright presentation is consistent with that application
using a viewer-level up-axis or leveling choice; it does not establish that
the raw COLMAP frame is Y-up. The Studio screenshots do not capture enough
settings to identify which of its viewing options performed that alignment.
The camera-pose consensus above independently establishes the viewer transform
used by this Engine preview. Physical gravity and navigation boundaries remain
unverified.

# Phase 4D.2 — GPU SOG Publication Acceptance

Date: 2026-11-03

Engine baseline: `7ae1bebc8696f1daf1c35f15563a3bd9885d9c78` (accepted Phase 4D.1)
Acceptance package and screenshots: Chuen Lung `derived/phase4d2/`

## Result

The accepted Phase 4D.1 master PLY now has a validated external SOG derivative and a modular, SOG-primary Visitor package. The PLY remains the reference master. The Visitor package contains the SOG and enclosure collision asset, not a duplicate PLY. The existing Review package remains available with all five guided routes. No reconstruction, SfM, or training stage was run, and public release authorization remains `NEEDS_REVIEW`.

## SOG provenance and size

| Item | Recorded value |
|---|---|
| PLY master | 247,738,651 bytes; SHA-256 `bebbec74f033deed6bf4df8811fbd6537947f9e3ba07d684622a79a4f62d9d3e` |
| PLY metadata | 998,940 Gaussians; SH band 3 |
| Converter | SplatTransform v3.10.3 (`4f90012`) |
| Host execution | Apple M4 Pro GPU, selection `-g 0`, 10 SH iterations, no decimation/filter/coordinate transform |
| Confirmed conversion duration | 17.274 seconds |
| Reported peak memory | GPU 122.3 MB; CPU 1.21 GB |
| SOG | 16,137,572 bytes; SHA-256 `462a32b31a2cedae912cdd037baf124cae46aa08b6ae91a7e0c12d337ed6b919` |
| SOG metadata | SOG v2, 998,940 Gaussians, SH band 3, 8 archive members; CRC and referenced texture checks passed |
| Output/input size ratio | 6.514%; 93.486% smaller than the PLY |

The exact host command, tool/device details, supplied duration and memory diagnostics, source/SOG hashes, inspection output, and registration timestamp are in `derived/phase4d2/output/artifacts/visitor-sog/provenance.json`. The visitor package's `config/exhibition.json` carries the public conversion summary. SOG is a lossy derivative; the matching PLY remains unchanged and is not claimed to be byte- or pixel-equivalent.

## GPU diagnosis and execution policy

The old Engine conversion path explicitly selected CPU. In the current Engine execution environment, the pinned SplatTransform v3.10.3 binary reports `No GPU adapters found`; therefore direct Engine conversion cannot use the host GPU from this process. The user's confirmed host command used the same SplatTransform build and selected GPU 0 on Apple M4 Pro. This establishes an execution-mode and adapter-visibility difference, but does not prove sandboxing alone caused the earlier CPU stalls.

Two earlier approved full-master CPU runs (10 and 20 iterations, each capped at 600 seconds) and one approved 1-in-16 sample run (10 iterations, capped at 120 seconds) timed out. Available output did not identify the precise stalled stage. They were not repeated. Engine conversion now enumerates adapters, defaults to a visible GPU, records its choice and logs, stages and validates the output before promotion, and refuses an implicit CPU fallback. CPU mode requires explicit opt-in plus `EXHIBITION_ARCHIVE_SOG_DEVICE=cpu`. For GPU-restricted hosts, `sog-import` validates and registers an already-generated SOG without recompression.

## Publication and reproducibility

The standard `package` workflow completed the visitor, archive, and verification stages while reusing valid caches for earlier stages; SfM and training were skipped. The generated Visitor site has 16 indexed payload files and 18,564,303 total indexed bytes. Its SOG is an external `assets/exhibition.sog`; `assets/exhibition.ply` is absent. Review continues to reference the accepted PLY and its five guided routes.

The package index SHA-256 was `221dd6df85a1cb7ee04417edeaf44e0b7634108a80794411eae5c64e36736906`. A second Engine package run with the same registered SOG and inputs regenerated the exact same package-index bytes. This reproducibility claim covers deterministic package generation from fixed inputs and runtime; separate GPU conversion runs are not promised to produce identical SOG bytes.

## Chrome acceptance

Real Chuen Lung packages were served on loopback at root and `/nested/` and exercised in Google Chrome 156.0.8078.80 with SuperSplat Viewer 1.42.0 / PlayCanvas 2.23.3 on WebGPU. The final SOG browser report records 53/53 checks passing, including English and Traditional Chinese switching while the SOG scene is active. It contains zero browser, console, network, HTTP, package-index, or local-reference failures across 25 requests.

Measured first rendered SOG view: 2,618 ms at root and 2,626 ms under `/nested/`. The browser measured the SOG asset transfer body as 16,137,572 bytes. At the same opening pose the PLY comparison rendered in 3,566 ms in this run; these are local Chrome measurements, not a general device/network benchmark.

Verified behavior includes:

- The opening camera uses canonical `station-046` metadata (FOV 70.75), matches the requested SuperSplat pose within `1.32e-7` viewer units, is inside the accepted enclosure, and has aligned world-up/no roll. It has 0.7652 common-frame horizontal boundary clearance, 2.3093 floor clearance, and 3.3807 ceiling clearance.
- W/A/S/D, Q/E, diagonal movement, mouse look, movement release, and browser focus transitions work in locked Fly Mode. Walk/Orbit/framing/overlay/game-mode shortcuts remain blocked.
- Camera movement toward open space succeeds before the wall-boundary test; motion then stops with 0.001 common-unit clearance margin. Wall-parallel movement slides along the boundary. Free flight clamps at the 0.08 floor margin and 5.77 ceiling limit. Reset from a wall returns to the authored opening pose inside the enclosure.
- Traditional Chinese and English switching updates text without reloading the model or moving the camera. Traditional Chinese has no accidental Simplified Chinese text; Help opens on load and can be hidden/reopened. The 390-pixel viewport check passed.
- Visitor UI exposes only Help and Reset View with no tours, diagnostics, route selectors, or geometry overlays. All five Review guided routes remain present.
- Root and nested URL asset paths work. SOG is the active model; there were no missing-asset or browser errors.

## PLY/SOG visual comparison

The browser harness captured PLY and SOG at the same 1440×900 camera poses: the accepted opening view, a close photographic-wall view with pale handwritten hanging details, the headphone-installation view, and dedicated central floor and inferred-ceiling views. The PLY was the unchanged accepted master hash. Image metrics are screenshot comparisons including expected renderer/pixel variation:

| View | Mean absolute RGB error | 95th percentile channel difference | Pixels >8 difference | Pixels >32 difference |
|---|---:|---:|---:|---:|
| Opening | 2.0105 | 8 | 3.862% | 0.139% |
| Photographic wall / pale handwritten detail | 2.4956 | 9 | 5.312% | 1.227% |
| Headphone installation | 2.6065 | 11 | 8.642% | 0.290% |
| Central floor | 2.4731 | 9 | 6.168% | 0.000% |
| Inferred ceiling | 1.9810 | 7 | 2.145% | 0.001% |

Screenshot inspection found the pale handwritten detail still visible and the artwork/headphone structures visually consistent. Floor and ceiling differences remained small; the streaking/ghost-like ceiling and floor reconstruction features are already present in the accepted PLY. No new objectionable SOG defect was observed. This is artifact-specific visual acceptance, not proof of lossless equivalence. Existing pale hanging text/white-wall ghosting and ceiling/floor artifacts remain limitations of the accepted PLY reconstruction; SOG does not improve those reconstruction limitations.

All bilingual, narrow-screen, boundary, opening, and matched PLY/SOG screenshots plus browser JSON, request log, and Chrome logs are preserved under `derived/phase4d2/evidence/visitor-browser-sog/`.

## Acceptance gates

| Gate | Result | Evidence/limit |
|---|---|---|
| Host GPU conversion | PASS | User-confirmed Apple M4 Pro run; 17.274 s; same SplatTransform 3.10.3 build |
| Engine GPU execution | DOCUMENTED ENVIRONMENT LIMITATION | Current Engine process sees no adapters; conversion code has bounded auto/index GPU selection; no GPU conversion was repeated here |
| External SOG import | PASS | Source hash, PLY/SOG metadata, generator, archive members, CRCs, and copied bytes validated |
| SOG integrity/provenance | PASS | Input and output hashes/sizes, tool/command, GPU, settings, execution diagnostics, Gaussian count and SH bands recorded |
| SOG Visitor publication | PASS | Modular SOG-primary package; unused visitor PLY omitted |
| PLY/SOG fidelity | PASS | Five matched viewpoints inspected; metrics above; no new objectionable SOG defect observed |
| Browser navigation/regression | PASS | Chrome report: 53/53 checks, root and nested paths, all supported axes, boundaries, reset, active-SOG language switching; no errors |
| Modular package integrity | PASS | 16 indexed payload files; hashes/relative references verified; repeated package index byte-identical |
| Public release authorization | NEEDS_REVIEW | No deployment or rights approval performed |

## Commands and evidence

The accepted SOG was not recompressed during this phase. The Engine import used `sog-import` with the existing Phase 4D.1 SOG, pinned inspector, source master hash, user-confirmed host command/device/settings, and supplied execution diagnostics. The package was produced with `bin/exhibition-archive package <isolated Chuen Lung Phase 4D.2 workspace>`; browser-only acceptance instrumentation was used only in a separate temporary test copy and is absent from the production package. No SfM, COLMAP, or Gaussian training command was run.

See [publishing](publishing.md) for GPU policy and import workflow, and [visitor integration](visitor-integration.md) for static hosting and embedding.

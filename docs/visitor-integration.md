# Integrating a Visitor Website Package

A Visitor package is a static website, not a single-file export. Serve it over HTTP or HTTPS so the browser can fetch its JSON, JavaScript, collision mesh, and Gaussian model as separate assets. The same relative paths work at a hosting root and below a nested URL prefix. Direct `file://` opening is not supported because modern browsers restrict fetches from local files.

## Package layout

The package keeps presentation, Viewer settings, language text, model assets, and the upstream runtime in separate files:

```text
index.html
assets/exhibition.sog                 # selected for a SOG-primary package
# or assets/exhibition.ply            # selected when using the PLY publication profile
assets/enclosure-collision.glb
config/exhibition.json
config/settings.json
config/locales.json
config/package-index.json
js/viewer.js
css/viewer.css
vendor/supersplat-viewer.js
vendor/supersplat-viewer.css
```

The accepted PLY master remains unchanged at its Engine artifact location and is identified by SHA-256 in `config/exhibition.json`. A SOG-primary Visitor package contains the SOG as an independent external asset and does not copy the unused 247.7 MB PLY into the website. When the SOG is unavailable or not selected, the Engine publishes a PLY-profile package containing the accepted PLY instead. The SOG may be copied into another application without copying the HTML page.

## Registering an externally generated SOG

On a GPU-capable host, generate SOG from the accepted PLY with the pinned `splat-transform` tool. Then import it into the Engine project. Import validates the project master PLY hash, checks PLY and SOG with `splat-transform --info`, compares Gaussian count and SH band count, checks the SOG metadata and archive CRCs, and records source/output integrity and conversion provenance. It does not recompress the asset and does not need GPU access:

```bash
exhibition-archive sog-import /path/to/project \
  /path/to/exhibition.sog \
  --converter-version 'splat-transform v3.10.3 (4f90012)' \
  --conversion-command 'splat-transform -g 0 --verbose --memory accepted-master.ply exhibition.sog -i 10' \
  --device 'Apple M4 Pro' --gpu-selection 0 --sh-iterations 10 \
  --execution-metadata-json '{"elapsed_seconds":17.274,"gpu_peak_memory":"122.3 MB","cpu_peak_memory":"1.21 GB"}'

exhibition-archive package /path/to/project
```

The Engine registers the exact input bytes at `output/artifacts/visitor-sog/exhibition.sog` with a private provenance record. The package stage validates that record against the current accepted master before selecting the SOG. A changed master hash invalidates the import; register a new SOG derivative after conversion from the new master.

## Replacing or reusing the model

To select another compatible model, place it under `assets/` and update the relevant `models` entry in `config/exhibition.json` with its relative URL and filename. Keep the archival PLY master in the Engine artifact store; include it in a website only when building the PLY publication profile. Rebuild with the Engine after edits so the generated package index covers every package file except itself.

`config/settings.json` contains the supported SuperSplat Experience Settings v2 rendering and camera settings. `config/locales.json` contains the two visitor UI translations. Exhibition titles and artwork metadata stay in their authored records and are not translated by the UI.

## Embedding the generated website

An existing website can host the package in an iframe. Use the deployed package URL, including its trailing slash, so relative assets resolve below the package path:

```html
<iframe
  src="/exhibitions/chuen-lung/"
  title="Chuen Lung exhibition"
  allow="fullscreen"
  style="display:block;width:100%;height:80vh;border:0"
></iframe>
```

If the host page owns the surrounding presentation, the Viewer can instead be created from the bundled official API. The following example loads the package metadata and external SOG, then applies the package's shared model transform through PlayCanvas entity methods exposed by the public `viewer.app` handle:

```js
import { createViewer } from './visitor/vendor/supersplat-viewer.js';

const base = new URL('./visitor/', document.baseURI);
const [metadata, settings] = await Promise.all([
  fetch(new URL('config/exhibition.json', base)).then(response => response.json()),
  fetch(new URL('config/settings.json', base)).then(response => response.json())
]);
const model = metadata.primary_format === 'sog' ? metadata.models.sog : metadata.models.ply;
if (!model?.available) throw new Error('The selected model asset is unavailable.');
const viewer = await createViewer({
  container: document.querySelector('#viewer'),
  contentUrl: new URL(model.url, base).href,
  contentFilename: model.filename,
  collisionUrl: new URL(metadata.collision.url, base).href,
  settings,
  ui: false
});
if (!viewer.state.loaded) {
  await new Promise(resolve => viewer.events.once('loaded:changed', resolve));
}
const transform = metadata.initial_camera.model_scene_transform_supersplat;
const splat = viewer.app.root.findComponents('gsplat')[0].entity;
const app = viewer.app;
const restoreAutoRender = app.autoRender;
const waitForFrameEnd = () => new Promise(resolve => app.once('frameend', resolve));
app.autoRender = true;
try {
  splat.setLocalPosition(...transform.translation);
  splat.setLocalRotation(...transform.rotationQuaternionXYZW);
  splat.setLocalScale(transform.uniformScale, transform.uniformScale, transform.uniformScale);
  viewer.state.cameraMode = 'fly';
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  viewer.resetCamera();
  await new Promise(resolve => setTimeout(resolve, 1300));
  await waitForFrameEnd();
  await waitForFrameEnd();
  app.renderNextFrame = true;
} finally {
  app.autoRender = restoreAutoRender;
}
```

The first complete frame precedes `state.loaded`, and the official Viewer may then switch to on-demand rendering. Keep the render loop active while applying a model transform and establishing the Fly spawn so the initial visible frame contains the transformed model. The host should call `viewer.destroy()` when removing the embedded viewer. The example deliberately uses the package-relative runtime rather than a third-party CDN. A host with a separate Viewer build can copy the SOG asset and use the same documented `createViewer` inputs; apply the matching scene transform when preserving this publication's camera and enclosure coordinates.

## Static hosting details

Serve the selected `.sog` or `.ply` and `.glb` as binary responses, preferably `application/octet-stream`, and allow byte-range requests for model files. Do not rewrite their bytes. No Python service is needed after the package is built. The selected model remains an independent payload asset; it is not embedded in HTML or JavaScript.

This package is a reproducible static publication candidate, not a Preservation Package. Its release authorization remains `NEEDS_REVIEW`.

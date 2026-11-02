# Integrating a Visitor Website Package

A Visitor package is a static website, not a single-file export. Serve it over HTTP or HTTPS so the browser can fetch its JSON, JavaScript, collision mesh, and Gaussian model as separate assets. The same relative paths work at a hosting root and below a nested URL prefix. Direct `file://` opening is not supported because modern browsers restrict fetches from local files.

## Package layout

The package keeps presentation, Viewer settings, language text, model assets, and the upstream runtime in separate files:

```text
index.html
assets/exhibition.ply
assets/exhibition.sog                 # present only after an accepted conversion
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

`assets/exhibition.ply` is the accepted reference master and remains unchanged. When a SOG derivative is available, `config/exhibition.json` names it as the primary model and the Viewer loads that separate `.sog` URL. The model format is selected from the configured filename extension. The SOG may also be copied into another application without copying the HTML page.

## Replacing or reusing the model

To select another compatible model, place it under `assets/` and update the relevant `models` entry in `config/exhibition.json` with its relative URL and filename. Preserve `assets/exhibition.ply` when retaining the accepted archive master. Recompute the package index after edits; the generated index covers every package file except itself.

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
const model = metadata.models.sog.available ? metadata.models.sog : metadata.models.ply;
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

Serve `.sog`, `.ply`, and `.glb` as binary responses, preferably `application/octet-stream`, and allow byte-range requests for large model files. Do not rewrite their bytes. No Python service is needed after the package is built. The `.sog` and `.ply` files remain independent payload assets; neither is embedded in HTML or JavaScript.

This package is a reproducible static publication candidate, not a Preservation Package. Its release authorization remains `NEEDS_REVIEW`.

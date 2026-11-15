import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { patchVisitorViewer, patchVisitorViewerText } from '../scripts/patch_visitor_viewer_xr.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const hash = value => createHash('sha256').update(value).digest('hex');

test('Viewer XR patch is deterministic, idempotent and matches the accepted pinned output', async () => {
  const stock = await readFile(path.join(root, 'templates/visitor/vendor/supersplat-viewer.js'), 'utf8');
  const patched = patchVisitorViewerText(stock);
  assert.equal(hash(patched), '94d9a4c4ad9612385708c21602d5a7f481d352468667dee6d3335beb6f4478ac');
  assert.equal(patchVisitorViewerText(patched), patched);
  assert.match(patched, /window\.__visitorXRDebug\?\.recenter/);
  assert.equal((patched.match(/rebaseXRHomeTransform/g) ?? []).length, 1);
});

test('Viewer XR patch generator refuses an unsupported stock bundle hash', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'visitor-xr-patch-'));
  const sourcePath = path.join(directory, 'unsupported.js');
  const destinationPath = path.join(directory, 'should-not-exist.js');
  try {
    await writeFile(sourcePath, 'not the pinned Viewer bundle');
    await assert.rejects(
      patchVisitorViewer({ sourcePath, destinationPath }),
      /unsupported SuperSplat Viewer bundle/
    );
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

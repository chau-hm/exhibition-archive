import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const esbuild = await import('esbuild');
async function trimLineEndWhitespace(file) {
  const content = await readFile(file, 'utf8');
  await writeFile(file, content.replace(/[ \t]+(?=\r?$)/gm, ''));
}
async function packageRoot(entry, packageName) {
  let directory = path.dirname(entry);
  while (directory !== path.dirname(directory)) {
    try {
      const packageJson = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8'));
      if (packageJson.name === packageName) return directory;
    } catch {
      // Keep walking until the owning package manifest is found.
    }
    directory = path.dirname(directory);
  }
  throw new Error('Could not locate package root for ' + packageName);
}
const viewerPath = await packageRoot(fileURLToPath(import.meta.resolve('@playcanvas/supersplat-viewer/viewer')), '@playcanvas/supersplat-viewer');
const playcanvasPath = await packageRoot(fileURLToPath(import.meta.resolve('playcanvas')), 'playcanvas');
const vendor = path.join(root, 'templates', 'visitor', 'vendor');
await mkdir(vendor, { recursive: true });
await esbuild.build({
  entryPoints: [path.join(root, 'templates', 'visitor', 'runtime.js')],
  bundle: true,
  format: 'esm',
  target: ['es2022'],
  outfile: path.join(vendor, 'supersplat-viewer.js'),
  sourcemap: false,
  legalComments: 'none',
  minify: true,
});
await esbuild.build({
  entryPoints: [path.join(root, 'templates', 'visitor', 'app.js')],
  bundle: true,
  external: ['../vendor/supersplat-viewer.js'],
  format: 'esm',
  target: ['es2022'],
  outfile: path.join(vendor, 'visitor-app.js'),
  sourcemap: false,
  legalComments: 'none',
  minify: true,
  define: { __EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__: 'false' },
});
await copyFile(path.join(viewerPath, 'dist', 'viewer.css'), path.join(vendor, 'supersplat-viewer.css'));
await copyFile(path.join(viewerPath, 'LICENSE'), path.join(vendor, 'SUPERSPLAT-VIEWER-LICENSE.txt'));
await copyFile(path.join(viewerPath, 'THIRD_PARTY_NOTICES.md'), path.join(vendor, 'THIRD_PARTY-NOTICES.md'));
await copyFile(path.join(playcanvasPath, 'LICENSE'), path.join(vendor, 'PLAYCANVAS-LICENSE.txt'));
// Keep upstream shader template whitespace semantically intact except for
// trailing line whitespace, so generated assets pass repository diff checks.
await Promise.all([
  trimLineEndWhitespace(path.join(vendor, 'supersplat-viewer.js')),
  trimLineEndWhitespace(path.join(vendor, 'THIRD_PARTY-NOTICES.md'))
]);
const viewer = JSON.parse(await readFile(path.join(viewerPath, 'package.json'), 'utf8'));
const engine = JSON.parse(await readFile(path.join(playcanvasPath, 'package.json'), 'utf8'));
await writeFile(path.join(vendor, 'runtime.json'), `${JSON.stringify({
  viewer: { name: viewer.name, version: viewer.version, license: viewer.license },
  engine: { name: engine.name, version: engine.version, license: engine.license }
}, null, 2)}\n`);
console.log(`Built SuperSplat Viewer ${viewer.version} on PlayCanvas ${engine.version} into ${path.relative(root, vendor)}`);

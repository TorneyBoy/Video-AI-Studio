// Copy the installed, pinned package's browser/native distributions unchanged.
// This is source packaging, not a runtime test or media processing step.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const packagePath = require.resolve('js-sha256/package.json');
const packageRoot = path.dirname(packagePath);
const metadata = JSON.parse(fs.readFileSync(packagePath, 'utf8'));
const sources = [
  ['build/sha256.js', 'web/vendor-sha256.js'],
  ['build/sha256.cjs', 'miniprogram/src/vendor/sha256.js'],
  ['index.d.ts', 'miniprogram/src/vendor/sha256.d.ts'],
  ['LICENSE.txt', 'third_party/js-sha256-LICENSE.txt'],
];
const files = {};
for (const [source, destination] of sources) {
  const target = path.join(root, destination);
  fs.mkdirSync(path.dirname(target), {recursive: true});
  const bytes = fs.readFileSync(path.join(packageRoot, source));
  fs.writeFileSync(target, bytes);
  files[destination] = crypto.createHash('sha256').update(bytes).digest('hex');
}
fs.writeFileSync(path.join(root, 'third_party/client-deps.json'), JSON.stringify({
  package: metadata.name, version: metadata.version, license: metadata.license, files,
  note: 'Pinned distributions copied unchanged; runtime acceptance belongs to the M2 stage gate.',
}, null, 2) + '\n');

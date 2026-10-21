const { spawnSync } = require('node:child_process');
const { mkdirSync, writeFileSync } = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const output = path.join(root, '.cache/test-build');
mkdirSync(output, { recursive: true });
writeFileSync(path.join(output, 'package.json'), '{"type":"commonjs"}\n');
const compile = spawnSync(process.execPath, [path.join(root, 'node_modules/typescript/bin/tsc'),
  '--outDir', output, '--rootDir', 'src', '--module', 'commonjs', '--moduleResolution', 'node',
  '--target', 'ES2022', '--lib', 'ES2022,DOM', '--strict', '--skipLibCheck',
  'src/data/api.ts', 'src/data/research.ts', 'src/data/route.ts', 'src/app/links.ts', 'src/features/research/mutationRecovery.ts'], { cwd: root, stdio: 'inherit' });
if (compile.status !== 0) process.exit(compile.status || 1);
const result = spawnSync(process.execPath, ['--test', path.join(__dirname, 'contracts.cjs')], { cwd: root, stdio: 'inherit' });
process.exit(result.status || 0);

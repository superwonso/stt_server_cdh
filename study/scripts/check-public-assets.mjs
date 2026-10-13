// Production public GETs only. Browser route fixtures never authenticate or
// call the service API. Run explicitly after Pages confirms the new release.
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const directory = fileURLToPath(new URL('..', import.meta.url));
const args = process.argv.slice(2);
let origin = 'https://superwonso.github.io';
let rehearsal = false;
if (args[0] === '--rehearsal') {
  origin = args[1] || '';
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin))
    throw new Error('Rehearsal requires an explicit loopback origin.');
  args.splice(0, 2);
  rehearsal = true;
}
if (args.some((value) => value !== '--list'))
  throw new Error('Only --list or --rehearsal <loopback origin> is supported.');

const result = spawnSync(
  process.execPath,
  [
    path.join(directory, 'node_modules/@playwright/test/cli.js'),
    'test',
    'e2e/learning.spec.ts',
    'e2e/structured-answer.spec.ts',
    'e2e/keyboard.spec.ts',
    '--reporter=list,json',
    '--output=test-results/public-assets',
    ...args,
  ],
  {
    cwd: directory,
    stdio: 'inherit',
    env: {
      ...process.env,
      STUDY_PUBLIC_ASSETS: '1',
      STUDY_PUBLIC_ASSETS_REHEARSAL: rehearsal ? '1' : '0',
      STUDY_TEST_URL: origin,
      PLAYWRIGHT_JSON_OUTPUT_FILE: path.join(directory, 'test-results/public-assets-e2e.json'),
    },
  },
);
if (result.error) throw result.error;
process.exit(result.status ?? 1);

import { readFile, writeFile, readdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
async function secure(directory) {
  for (const item of await readdir(directory, { withFileTypes: true })) {
    const filename = path.join(directory, item.name);
    if (item.isDirectory()) {
      await secure(filename);
      continue;
    }
    if (!item.name.endsWith('.html')) continue;
    let html = await readFile(filename, 'utf8');
    const hashes = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)]
      .filter((match) => !/\bsrc=/.test(match[1]) && match[2])
      .map((match) => `'sha256-${createHash('sha256').update(match[2]).digest('base64')}'`);
    const policy = `default-src 'self'; script-src 'self' ${[...new Set(hashes)].join(' ')}; style-src 'self'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-src 'none'`;
    html = html.replace(
      '<head>',
      `<head><meta http-equiv="Content-Security-Policy" content="${policy}"><meta name="referrer" content="no-referrer">`,
    );
    await writeFile(filename, html);
  }
}
await secure('out');
await writeFile('out/.nojekyll', '');
console.log('Static export: exact inline script hashes and same-origin CSP applied.');

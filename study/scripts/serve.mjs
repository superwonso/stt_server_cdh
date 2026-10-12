import http from 'node:http';
import path from 'node:path';
import { readFile, stat } from 'node:fs/promises';
import { gzipSync } from 'node:zlib';
const root = path.resolve('out');
const base = process.env.NEXT_PUBLIC_BASE_PATH || '/stt_server_cdh/study';
const port = Number(process.env.PORT || 3218);
const mime = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.json': 'application/json',
  '.css': 'text/css',
  '.woff2': 'font/woff2',
  '.otf': 'font/otf',
  '.ttf': 'font/ttf',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.txt': 'text/plain',
};
const fixture = `<!doctype html><html lang="ko"><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>암기 학습 개발 확인</title><link rel="icon" href="data:,"><style>body{margin:0}header{padding:16px;font:600 18px sans-serif;color:#23473a;background:#f7f5ee}iframe{width:100%;height:1600px;border:0}</style></head><body><header>여백 · 암기 학습</header><main><iframe title="암기 학습" src="${base}/" sandbox="allow-scripts allow-same-origin allow-downloads allow-modals"></iframe></main><script>const f=document.querySelector('iframe');window.addEventListener('message',e=>{if(e.origin!==location.origin||e.source!==f.contentWindow||e.data?.channel!=='yeobaek-study-v1')return;if(e.data.type==='ready')f.contentWindow.postMessage({channel:'yeobaek-study-v1',type:'context',scope:'${'a'.repeat(64)}',active:true},location.origin);if(e.data.type==='height')f.style.height=Math.max(700,Math.min(20000,e.data.height))+'px';});</script></body></html>`;
http
  .createServer(async (req, res) => {
    try {
      const url = new URL(req.url, `http://127.0.0.1:${port}`);
      if (url.pathname === '/__study-test/') {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(fixture);
        return;
      }
      if (!url.pathname.startsWith(`${base}/`)) {
        res.writeHead(404);
        res.end();
        return;
      }
      let filename = path.resolve(root, decodeURIComponent(url.pathname.slice(base.length + 1)));
      if (!filename.startsWith(root + path.sep) && filename !== root) throw new Error('path');
      if ((await stat(filename)).isDirectory()) filename = path.join(filename, 'index.html');
      const bytes = await readFile(filename);
      const compressed =
        /\bgzip\b/.test(req.headers['accept-encoding'] || '') &&
        /\.(html|js|mjs|json|css|svg|txt)$/.test(filename);
      res.writeHead(200, {
        'Content-Type': mime[path.extname(filename)] || 'application/octet-stream',
        'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff',
        Vary: 'Accept-Encoding',
        ...(compressed ? { 'Content-Encoding': 'gzip' } : {}),
      });
      res.end(compressed ? gzipSync(bytes) : bytes);
    } catch {
      res.writeHead(404);
      res.end('Not found');
    }
  })
  .listen(port, '127.0.0.1', () => console.log(`Study test server on loopback port ${port}`));

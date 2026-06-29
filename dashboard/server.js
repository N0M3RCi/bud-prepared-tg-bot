import express from 'express';
import { createServer } from 'http';

const app = express();
const server = createServer(app);

app.use(express.json({ limit: '1mb' }));

// ── SSE clients ────────────────────────────────────────────────────────────────
let clients = new Set();

app.get('/api/events/stream', (req, res) => {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'Access-Control-Allow-Origin': '*',
  });
  res.write('data: {"type":"connected"}\n\n');

  const client = { id: Date.now(), res };
  clients.add(client);
  req.on('close', () => clients.delete(client));
});

function broadcast(event, data) {
  const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const client of clients) {
    try { client.res.write(payload); } catch { clients.delete(client); }
  }
}

// ── Event ingestion ────────────────────────────────────────────────────────────
let reports = [];
const MAX_REPORTS = 200;

app.post('/api/events/report', (req, res) => {
  const report = { ...req.body, ts: Date.now() };
  reports.push(report);
  if (reports.length > MAX_REPORTS) reports = reports.slice(-MAX_REPORTS / 2);
  broadcast('report', report);
  res.json({ ok: true });
});

app.get('/api/events/history', (_req, res) => {
  res.json(reports);
});

// ── Serve static build ─────────────────────────────────────────────────────────
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import fs from 'fs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const dist = resolve(__dirname, 'dist');
if (fs.existsSync(dist)) {
  app.use(express.static(dist));
  app.get('/{*path}', (_req, res) => res.sendFile(resolve(dist, 'index.html')));
}

const PORT = process.env.PORT || 3001;
server.listen(PORT, () => console.log(`Dashboard server on :${PORT}`));

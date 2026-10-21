import crypto from 'crypto';
import express from 'express';
import { createServer } from 'http';

const app = express();
const server = createServer(app);

app.use(express.json({ limit: '1mb' }));

// ── SSE clients ────────────────────────────────────────────────────────────────
let clients = new Set();

function ingestOk(req) {
  const expected = process.env.DASHBOARD_INGEST_SECRET || '';
  const given = req.get('x-dashboard-ingest-secret') || req.query.secret || '';
  if (!expected) return false;
  const a = Buffer.from(String(given));
  const b = Buffer.from(expected);
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

app.get('/api/events/stream', (req, res) => {
  if (!ingestOk(req)) return res.status(401).end();
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
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
  if (!ingestOk(req)) return res.status(401).json({ error: 'Invalid ingest secret' });
  const report = { ...req.body, ts: Date.now() };
  reports.push(report);
  if (reports.length > MAX_REPORTS) reports = reports.slice(-MAX_REPORTS / 2);
  broadcast('report', report);
  res.json({ ok: true });
});

app.get('/api/events/history', (req, res) => {
  if (!ingestOk(req)) return res.status(401).json({ error: 'Invalid ingest secret' });
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

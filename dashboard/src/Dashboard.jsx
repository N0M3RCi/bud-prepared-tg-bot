import { useState, useEffect, useRef, useLayoutEffect } from 'react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';

/* ── Helpers ──────────────────────────────────────────────────────────── */
const fmt = (n) => (n ?? 0).toLocaleString();
const ago = (ts) => {
  if (!ts) return '—';
  const s = Math.floor((Date.now() - ts) / 1000);
  if (s < 5) return 'just now';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
};
const ts = () => new Date().toLocaleTimeString();

export default function Dashboard() {
  /* ── State ────────────────────────────────────────────────────────── */
  const [liveLogs, setLiveLogs] = useState([]);
  const [liveMessages, setLiveMessages] = useState([]);
  const [liveCommands, setLiveCommands] = useState([]);
  const [liveSubscribers, setLiveSubscribers] = useState([]);
  const [liveNewUsers, setLiveNewUsers] = useState([]);
  const [liveTokens, setLiveTokens] = useState([]);
  const [liveLatency, setLiveLatency] = useState([]);
  const [isLiveConnected, setIsLiveConnected] = useState(false);
  const [ingestReports, setIngestReports] = useState([]);
  const [lastIngestTs, setLastIngestTs] = useState(null);
  const [lastModel, setLastModel] = useState('—');

  /* ── Refs ──────────────────────────────────────────────────────────── */
  const userScrolled = useRef(false);
  const rootRef = useRef(null);

  /* ── SCROLL LOCK ──────────────────────────────────────────────────── */
  // CSS prevents document from scrolling (overflow:hidden on html/body).
  // #root is the only scroll container. We track user-initiated scroll on
  // #root and enforce scroll-to-top when new content arrives.

  useEffect(() => {
    const root = document.getElementById('root');
    rootRef.current = root;
    if (!root) return;
    const onUserScroll = () => {
      userScrolled.current = root.scrollTop > 0;
    };
    root.addEventListener('scroll', onUserScroll, { passive: true });
    return () => root.removeEventListener('scroll', onUserScroll);
  }, []);

  // After every render, force #root scroll to top if user hasn't scrolled
  useLayoutEffect(() => {
    if (userScrolled.current) return;
    const root = document.getElementById('root');
    if (root && root.scrollTop !== 0) {
      root.scrollTop = 0;
    }
    // Second pass after paint to catch any deferred layout changes
    requestAnimationFrame(() => {
      if (!userScrolled.current) {
        const el = document.getElementById('root');
        if (el && el.scrollTop !== 0) el.scrollTop = 0;
      }
    });
  });

  /* ── SSE connection ──────────────────────────────────────────────────── */
  useEffect(() => {
    const evtSource = new EventSource('/api/events/stream');
    evtSource.onopen = () => setIsLiveConnected(true);
    evtSource.onerror = () => setIsLiveConnected(false);

    evtSource.addEventListener('report', (e) => {
      try {
        const report = JSON.parse(e.data);
        setIngestReports((prev) => {
          const next = [report, ...prev];
          return next.slice(0, 100);
        });
        setLastIngestTs(report.ts);
        handleReport(report);
      } catch { /* ignore bad JSON */ }
    });

    return () => evtSource.close();
  }, []);

  function handleReport(report) {
    const log = `[${ts()}] ${report.event}${report.user ? ` (user ${report.user.id})` : ''}${report.message?.text ? `: ${report.message.text.slice(0, 100)}` : ''}`;
    setLiveLogs((prev) => [log, ...prev].slice(0, 100));

    if (report.event === 'message' || report.type === 'message') {
      setLiveMessages((prev) => [{ time: ts(), name: report.user?.first_name || `user${report.user?.id}` }, ...prev].slice(0, 50));
    }
    if (report.event === 'command' || report.type === 'command') {
      setLiveCommands((prev) => [{ time: ts(), command: report.extra?.command || report.message?.text || '' }, ...prev].slice(0, 50));
    }
    if (report.tokens) {
      setLiveTokens((prev) => [...prev, { time: ts(), tokens: report.tokens }].slice(-50));
    }
    if (report.event === 'new_user') {
      setLiveNewUsers((prev) => [{ time: ts(), name: report.user?.first_name || `user${report.user?.id}` }, ...prev].slice(0, 50));
    }
    if (report.event === 'subscriber_update') {
      setLiveSubscribers((prev) => [...prev, { time: ts(), count: report.extra?.count || 0 }].slice(-50));
    }
    if (report.model) {
      setLastModel(report.model);
    }
  }

  /* ── Charts data ─────────────────────────────────────────────────────── */
  const tokenData = liveTokens.map((t, i) => ({ idx: i, tokens: t.tokens }));
  const subData = liveSubscribers.map((s, i) => ({ idx: i, subs: s.count }));

  return (
    <div className="min-h-screen bg-slate-900 text-slate-200 p-4 md:p-6">

      {/* ── Header ──────────────────────────────────────────────────── */}
      <header className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-white">🤖 N0M3RCi Dashboard</h1>
        <span className={`flex items-center gap-2 text-sm ${isLiveConnected ? 'text-green-400' : 'text-red-400'}`}>
          <span className={`w-2 h-2 rounded-full ${isLiveConnected ? 'bg-green-400' : 'bg-red-400'}`} />
          {isLiveConnected ? 'Live' : 'Disconnected'}
        </span>
      </header>

      {/* ── Stats Cards ──────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <StatCard label="Messages" value={liveMessages.length} color="blue" />
        <StatCard label="Commands" value={liveCommands.length} color="green" />
        <StatCard label="Subscribers" value={liveSubscribers.length ? liveSubscribers.at(-1).count : 0} color="purple" />
        <StatCard label="New Users" value={liveNewUsers.length} color="amber" />
        <StatCard label="Tokens Used" value={liveTokens.reduce((a, b) => a + b.tokens, 0)} color="cyan" />
        <StatCard label="Latency" value="—" color="pink" />
        <StatCard label="Model" value={lastModel} color="indigo" />
        <StatCard label="Last Event" value={ago(lastIngestTs)} color="slate" />
      </div>

      {/* ── Charts ───────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
        <div className="bg-slate-800 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-slate-400 mb-3">Token Usage</h3>
          <ResponsiveContainer width="100%" height={180}>
            <LineChart data={tokenData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
              <XAxis dataKey="idx" stroke="#64748b" fontSize={10} />
              <YAxis stroke="#64748b" fontSize={10} />
              <Tooltip contentStyle={{ background: '#1e293b', border: 'none', borderRadius: 8, color: '#e2e8f0' }} />
              <Line type="monotone" dataKey="tokens" stroke="#06b6d4" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="bg-slate-800 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-slate-400 mb-3">Subscribers</h3>
          <ResponsiveContainer width="100%" height={180}>
            <LineChart data={subData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
              <XAxis dataKey="idx" stroke="#64748b" fontSize={10} />
              <YAxis stroke="#64748b" fontSize={10} />
              <Tooltip contentStyle={{ background: '#1e293b', border: 'none', borderRadius: 8, color: '#e2e8f0' }} />
              <Line type="monotone" dataKey="subs" stroke="#a855f7" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* ── Debug Log ───────────────────────────────────────────────── */}
      <div className="bg-slate-800 rounded-xl p-4 mb-6">
        <h3 className="text-sm font-semibold text-slate-400 mb-3">Debug Log</h3>
        <div className="terminal-container bg-slate-950 rounded-lg p-3 font-mono text-xs leading-relaxed h-48 overflow-y-auto whitespace-pre-wrap break-all"
             style={{ overflowAnchor: 'none', contain: 'layout' }}>
          {liveLogs.length === 0 && (
            <span className="text-slate-600">Waiting for events...</span>
          )}
          {liveLogs.map((log, i) => (
            <div key={i} className="text-green-400/90">{log}</div>
          ))}
        </div>
      </div>

      {/* ── Activity Tables ──────────────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
        <ActivityTable title="Messages" items={liveMessages} fields={['time', 'name']} />
        <ActivityTable title="Commands" items={liveCommands} fields={['time', 'command']} />
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <ActivityTable title="New Users" items={liveNewUsers} fields={['time', 'name']} />
        <ActivityTable title="Recent Reports" items={ingestReports.slice(0, 20)} fields={['ts', 'event', 'user']}
          renderRow={(r) => (
            <tr key={r.ts} className="text-xs border-b border-slate-700/50">
              <td className="py-1 px-2 text-slate-500">{ts()}</td>
              <td className="py-1 px-2 text-sky-300">{r.event}</td>
              <td className="py-1 px-2 text-slate-400">{r.user?.first_name || r.user?.username || '—'}</td>
            </tr>
          )} />
      </div>
    </div>
  );
}

/* ── Sub-components ─────────────────────────────────────────────────────── */
function StatCard({ label, value, color }) {
  const colorMap = { blue: 'border-blue-500/30', green: 'border-green-500/30', purple: 'border-purple-500/30', amber: 'border-amber-500/30', cyan: 'border-cyan-500/30', pink: 'border-pink-500/30', indigo: 'border-indigo-500/30', slate: 'border-slate-500/30' };
  return (
    <div className={`bg-slate-800 rounded-xl p-4 border-l-4 ${colorMap[color] || colorMap.blue}`}>
      <div className="text-xs text-slate-400 uppercase tracking-wider">{label}</div>
      <div className="text-xl font-bold text-white mt-1">{value}</div>
    </div>
  );
}

function ActivityTable({ title, items, fields, renderRow }) {
  return (
    <div className="bg-slate-800 rounded-xl p-4">
      <h3 className="text-sm font-semibold text-slate-400 mb-3">{title}</h3>
      <div className="max-h-48 overflow-y-auto" style={{ overscrollBehavior: 'contain' }}>
        {items.length === 0 ? (
          <p className="text-xs text-slate-600">No data yet</p>
        ) : (
          <table className="w-full text-left">
            <tbody>
              {renderRow ? items.map(renderRow) : items.map((item, i) => (
                <tr key={i} className="text-xs border-b border-slate-700/50">
                  {fields.map((f) => (
                    <td key={f} className="py-1 px-2 text-slate-400">{item[f] || '—'}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
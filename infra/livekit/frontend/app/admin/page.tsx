'use client';

import { useCallback, useEffect, useState } from 'react';

/**
 * 房间管理台 —— live.higcp.com/admin
 *
 * 为什么是自己写的：自建 LiveKit OSS 不带管理界面（官方 dashboard 是 Cloud
 * 的产品）。能力全部来自 RoomService RPC，这里只是把它摆出来。
 * 鉴权由 IAP 在边缘完成，这个页面本身不做登录。
 */

type Track = { sid: string; kind: string; muted: boolean; mime: string };
type P = {
  identity: string;
  name: string;
  sid: string;
  state: number;
  joinedAt: number;
  isAgent: boolean;
  tracks: Track[];
};
type Room = {
  name: string;
  sid: string;
  createdAt: number;
  numParticipants: number;
  participants: P[];
};

const STATE = ['JOINING', 'JOINED', 'ACTIVE', 'DISCONNECTED'];

function since(ts: number): string {
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
}

export default function Admin() {
  const [rooms, setRooms] = useState<Room[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [at, setAt] = useState<number>(0);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetch('/api/admin/rooms', { cache: 'no-store' });
      const d = await r.json();
      if (d.error) {
        setErr(d.error);
        return;
      }
      setErr(null);
      setRooms(d.rooms);
      setAt(d.at);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // 3 秒一刷。房间状态变化快（进出、静音），刷慢了看到的是过期画面；
  // 再快就没意义了，listParticipants 是逐房间一次 RPC。
  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  const act = async (body: Record<string, unknown>) => {
    setBusy(true);
    try {
      const r = await fetch('/api/admin/rooms', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (d.error) setErr(d.error);
      await load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <main style={S.page}>
      <header style={S.head}>
        <h1 style={S.h1}>LiveKit 房间管理台</h1>
        <div style={S.sub}>
          {at
            ? `更新于 ${new Date(at).toLocaleTimeString('zh-CN', { hour12: false })} · 每 3 秒自动刷新`
            : '加载中…'}
          {busy && ' · 执行中…'}
        </div>
      </header>

      {err && <div style={S.err}>出错：{err}</div>}

      {!err && rooms.length === 0 && (
        <div style={S.empty}>
          当前没有活跃房间。
          <br />
          <span style={{ color: '#5f6368', fontSize: 13 }}>
            房间是用完即走的：最后一个人离开后 SFU 会自动回收，这里空着是正常状态。
          </span>
        </div>
      )}

      {rooms.map((r) => (
        <section key={r.sid} style={S.card}>
          <div style={S.cardHead}>
            <div>
              <div style={S.room}>{r.name}</div>
              <div style={S.meta}>
                {r.sid} · 已存在 {since(r.createdAt)}
              </div>
            </div>
            <button
              style={S.danger}
              disabled={busy}
              onClick={() => {
                if (confirm(`关闭房间 ${r.name}？里面所有人会被断开。`))
                  act({ action: 'deleteRoom', room: r.name });
              }}
            >
              关闭房间
            </button>
          </div>

          <table style={S.table}>
            <thead>
              <tr>
                {['身份', '角色', '状态', '在线时长', '音轨', '操作'].map((h) => (
                  <th key={h} style={S.th}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {r.participants.map((p) => (
                <tr key={p.sid}>
                  <td style={S.td}>
                    <code style={S.code}>{p.identity}</code>
                  </td>
                  <td style={S.td}>
                    <span style={p.isAgent ? S.tagAgent : S.tagUser}>
                      {p.isAgent ? 'Agent' : '用户'}
                    </span>
                  </td>
                  <td style={S.td}>{STATE[p.state] ?? p.state}</td>
                  <td style={S.td}>{since(p.joinedAt)}</td>
                  <td style={S.td}>
                    {p.tracks.length === 0 ? (
                      <span style={{ color: '#9aa0a6' }}>—</span>
                    ) : (
                      p.tracks.map((t) => (
                        <span key={t.sid} style={t.muted ? S.trackMuted : S.track}>
                          {t.kind}
                          {t.mime ? `/${t.mime.split('/')[1]}` : ''}
                          {t.muted ? ' 静音' : ''}
                        </span>
                      ))
                    )}
                  </td>
                  <td style={S.td}>
                    {p.tracks
                      .filter((t) => t.kind === 'audio')
                      .map((t) => (
                        <button
                          key={t.sid}
                          style={S.btn}
                          disabled={busy}
                          onClick={() =>
                            act({
                              action: 'mute',
                              room: r.name,
                              identity: p.identity,
                              trackSid: t.sid,
                              muted: !t.muted,
                            })
                          }
                        >
                          {t.muted ? '取消静音' : '静音'}
                        </button>
                      ))}
                    <button
                      style={S.danger}
                      disabled={busy}
                      onClick={() => {
                        if (confirm(`把 ${p.identity} 踢出房间？`))
                          act({ action: 'removeParticipant', room: r.name, identity: p.identity });
                      }}
                    >
                      踢出
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}

      <footer style={S.foot}>
        这个页面坐在 IAP 后面，没有单独的登录。能打开它就说明已经通过 Google 账号鉴权。
      </footer>
    </main>
  );
}

// Material 风格、浅色、不用渐变
const S: Record<string, React.CSSProperties> = {
  page: {
    maxWidth: 1080,
    margin: '0 auto',
    padding: '32px 20px 64px',
    fontFamily: 'system-ui,-apple-system,"Noto Sans SC",sans-serif',
    color: '#202124',
    background: '#fff',
    minHeight: '100vh',
  },
  head: { marginBottom: 24 },
  h1: { fontSize: 24, fontWeight: 500, margin: 0, letterSpacing: '.2px' },
  sub: { fontSize: 13, color: '#5f6368', marginTop: 6 },
  err: {
    background: '#fce8e6',
    color: '#c5221f',
    border: '1px solid #f5c6c2',
    borderRadius: 8,
    padding: '12px 16px',
    marginBottom: 20,
    fontSize: 14,
  },
  empty: {
    border: '1px dashed #dadce0',
    borderRadius: 12,
    padding: '40px 20px',
    textAlign: 'center',
    color: '#3c4043',
    fontSize: 15,
  },
  card: {
    border: '1px solid #dadce0',
    borderRadius: 12,
    marginBottom: 20,
    overflow: 'hidden',
    boxShadow: '0 1px 2px rgba(60,64,67,.1)',
  },
  cardHead: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: '16px 20px',
    background: '#f8f9fa',
    borderBottom: '1px solid #dadce0',
  },
  room: { fontSize: 16, fontWeight: 500 },
  meta: { fontSize: 12, color: '#5f6368', marginTop: 4, fontFamily: 'ui-monospace,monospace' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 14 },
  th: {
    textAlign: 'left',
    padding: '10px 20px',
    color: '#5f6368',
    fontWeight: 500,
    fontSize: 12,
    textTransform: 'uppercase',
    letterSpacing: '.5px',
    borderBottom: '1px solid #e8eaed',
  },
  td: { padding: '12px 20px', borderBottom: '1px solid #f1f3f4', verticalAlign: 'middle' },
  code: {
    fontFamily: 'ui-monospace,monospace',
    fontSize: 13,
    background: '#f1f3f4',
    padding: '2px 6px',
    borderRadius: 4,
  },
  tagAgent: {
    background: '#e8f0fe',
    color: '#1967d2',
    padding: '2px 8px',
    borderRadius: 10,
    fontSize: 12,
  },
  tagUser: {
    background: '#e6f4ea',
    color: '#188038',
    padding: '2px 8px',
    borderRadius: 10,
    fontSize: 12,
  },
  track: {
    background: '#f1f3f4',
    color: '#3c4043',
    padding: '2px 8px',
    borderRadius: 4,
    fontSize: 12,
    marginRight: 6,
    fontFamily: 'ui-monospace,monospace',
  },
  trackMuted: {
    background: '#fce8e6',
    color: '#c5221f',
    padding: '2px 8px',
    borderRadius: 4,
    fontSize: 12,
    marginRight: 6,
    fontFamily: 'ui-monospace,monospace',
  },
  btn: {
    background: '#fff',
    border: '1px solid #dadce0',
    color: '#1a73e8',
    borderRadius: 4,
    padding: '5px 12px',
    fontSize: 13,
    cursor: 'pointer',
    marginRight: 8,
  },
  danger: {
    background: '#fff',
    border: '1px solid #f5c6c2',
    color: '#c5221f',
    borderRadius: 4,
    padding: '5px 12px',
    fontSize: 13,
    cursor: 'pointer',
  },
  foot: {
    marginTop: 32,
    fontSize: 12,
    color: '#5f6368',
    borderTop: '1px solid #e8eaed',
    paddingTop: 16,
  },
};

import { NextResponse } from 'next/server';
import { RoomServiceClient } from 'livekit-server-sdk';

/**
 * 房间管理 API —— 自建 LiveKit OSS **不带**任何管理界面（实测 1.13.6：
 * 7880 上只有 /rtc 信令和 /twirp/* 的 RPC，没有 UI，/debug/pprof 也是 404）。
 * 官方的 dashboard 是 LiveKit Cloud 的产品，不在 OSS 里。所以这一层是自己补的。
 *
 * 鉴权：**这里故意不做**。这个路由跟整个前端一样坐在 live.higcp.com 后面，
 * 而那个域名是 GCLB + IAP，未登录直接 401，连不到 Next.js。再叠一层自己写的
 * 鉴权只会多一处能配错的地方。⚠️ 前提是别把 /api/admin 加进 url-map 的
 * IAP 豁免路径。cc-alb 目前豁免的是 /assets/* 和 /feishu/*（两条都落到非 IAP
 * 的 cc-bs），而 host 规则是 *，所以 live.higcp.com 下的这两个前缀同样不鉴权。
 *
 * 连 SFU 走**内网**（VPC peering），不绕公网、不过 IAP —— 服务端到服务端，
 * 没有浏览器 cookie 可用，走公网那条路只会被 IAP 挡住。
 */

const SFU_ADMIN_URL = process.env.LIVEKIT_ADMIN_URL;
const API_KEY = process.env.LIVEKIT_API_KEY;
const API_SECRET = process.env.LIVEKIT_API_SECRET;

export const revalidate = 0;
export const dynamic = 'force-dynamic';

function client(): RoomServiceClient {
  // 缺配置就报错，不兜底猜一个默认值。猜错的后果是「页面能开、永远空房间」，
  // 比直接报错难查得多。
  if (!SFU_ADMIN_URL) throw new Error('LIVEKIT_ADMIN_URL 未配置（应为 SFU 的内网 http 地址）');
  if (!API_KEY || !API_SECRET) throw new Error('LIVEKIT_API_KEY / LIVEKIT_API_SECRET 未配置');
  return new RoomServiceClient(SFU_ADMIN_URL, API_KEY, API_SECRET);
}

export async function GET() {
  try {
    const svc = client();
    const rooms = await svc.listRooms();
    const detailed = await Promise.all(
      rooms.map(async (r) => {
        const ps = await svc.listParticipants(r.name);
        return {
          name: r.name,
          sid: r.sid,
          // protobuf 的 uint64 在 JS SDK 里是 bigint，JSON.stringify 会直接抛，
          // 必须显式转。creationTime 是秒不是毫秒。
          createdAt: Number(r.creationTime) * 1000,
          numParticipants: r.numParticipants,
          participants: ps.map((p) => ({
            identity: p.identity,
            name: p.name,
            sid: p.sid,
            // state: 0=JOINING 1=JOINED 2=ACTIVE 3=DISCONNECTED
            state: p.state,
            joinedAt: Number(p.joinedAt) * 1000,
            isAgent: p.identity.startsWith('agent-'),
            tracks: p.tracks.map((t) => ({
              sid: t.sid,
              kind: t.type === 1 ? 'video' : 'audio',
              muted: t.muted,
              mime: t.mimeType,
            })),
          })),
        };
      })
    );
    detailed.sort((a, b) => b.createdAt - a.createdAt);
    return NextResponse.json({ rooms: detailed, at: Date.now() });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 500 }
    );
  }
}

export async function POST(req: Request) {
  try {
    const svc = client();
    const { action, room, identity, trackSid, muted } = await req.json();
    switch (action) {
      case 'removeParticipant':
        await svc.removeParticipant(room, identity);
        break;
      case 'deleteRoom':
        // 这会把房间里所有人踢掉。agent 那边会收到 job 结束，属正常路径。
        await svc.deleteRoom(room);
        break;
      case 'mute':
        await svc.mutePublishedTrack(room, identity, trackSid, Boolean(muted));
        break;
      default:
        return NextResponse.json({ error: `未知 action: ${action}` }, { status: 400 });
    }
    return NextResponse.json({ ok: true });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 500 }
    );
  }
}

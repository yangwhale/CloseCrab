import { NextResponse } from 'next/server';
import { RoomServiceClient } from 'livekit-server-sdk';
import { checkNativeAuth } from '@/lib/native-auth';

/**
 * 「我能进哪些房间、哪些是活的」—— 给客户端（主要是 iOS app）用的房间目录。
 *
 * 跟 `/api/admin/rooms` 的区别：那个是运维视角，倒出全部房间和每个参与者的
 * 每条轨道；这个是用户视角，只回答两件事 —— 名单上有谁、谁现在能说话。
 * 没合并成一个接口是因为两边的**信任级别不同**：admin 那条只在 IAP 后面，
 * 这条要从 `/native/*` 暴露出去，回什么字段得掐着来。
 *
 * 名单来自 `ALLOWED_ROOMS`（和 `/api/token` 同一份真理，不另起一份，
 * 否则迟早出现「这里列得出、那里换不到 token」）。活没活来自 SFU。
 *
 * 为什么客户端不自己维护列表：加一个 bot 就得去每台设备上手动改一次，
 * 而且改错了要到点连接那一刻才报错。让服务端说了算，客户端只管显示。
 */

const SFU_ADMIN_URL = process.env.LIVEKIT_ADMIN_URL;
const API_KEY = process.env.LIVEKIT_API_KEY;
const API_SECRET = process.env.LIVEKIT_API_SECRET;

export const revalidate = 0;
export const dynamic = 'force-dynamic';

export async function GET(req: Request) {
  const gate = checkNativeAuth(req, 'rooms');
  if (!gate.ok) return new NextResponse(gate.message, { status: gate.status });

  try {
    if (!process.env.ALLOWED_ROOMS) {
      throw new Error('ALLOWED_ROOMS 未配置');
    }
    const allowed = process.env.ALLOWED_ROOMS.split(',')
      .map((s) => s.trim())
      .filter(Boolean);

    // SFU 查不通不该让整个列表挂掉 —— 名单本身是静态的，
    // 「在不在线」只是锦上添花。降级成「全部未知」，客户端照样能选房间。
    let live: Map<string, { participants: number; agents: number }> | null = null;
    try {
      if (!SFU_ADMIN_URL) throw new Error('LIVEKIT_ADMIN_URL 未配置');
      if (!API_KEY || !API_SECRET) throw new Error('LIVEKIT_API_KEY / LIVEKIT_API_SECRET 未配置');
      const svc = new RoomServiceClient(SFU_ADMIN_URL, API_KEY, API_SECRET);
      const rooms = await svc.listRooms(allowed);
      live = new Map(
        await Promise.all(
          rooms.map(async (r) => {
            const ps = await svc.listParticipants(r.name);
            return [
              r.name,
              {
                participants: ps.filter((p) => !p.identity.startsWith('agent-')).length,
                agents: ps.filter((p) => p.identity.startsWith('agent-')).length,
              },
            ] as const;
          })
        )
      );
    } catch (e) {
      console.error('列房间失败，降级成只回名单：', e);
    }

    return NextResponse.json({
      rooms: allowed.map((name) => {
        const info = live?.get(name);
        return {
          name,
          // null = 没查到 SFU，别让客户端把「不知道」画成「离线」
          online: live === null ? null : info !== undefined,
          // 有 agent 在岗才是真的「能说话」。房间在、agent 不在，
          // 表现就是 2026-09-13 那个「进去了但没人应」。
          ready: live === null ? null : (info?.agents ?? 0) > 0,
          participants: info?.participants ?? null,
        };
      }),
      at: Date.now(),
    });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 500 }
    );
  }
}

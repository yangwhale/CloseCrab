import { NextResponse } from 'next/server';
import { AccessToken, type AccessTokenOptions, type VideoGrant } from 'livekit-server-sdk';
import { RoomConfiguration } from '@livekit/protocol';

type ConnectionDetails = {
  serverUrl: string;
  roomName: string;
  participantName: string;
  participantToken: string;
};

// NOTE: you are expected to define the following environment variables in `.env.local`:
const API_KEY = process.env.LIVEKIT_API_KEY;
const API_SECRET = process.env.LIVEKIT_API_SECRET;
const LIVEKIT_URL = process.env.LIVEKIT_URL;

// don't cache the results
export const revalidate = 0;

export async function POST(req: Request) {
  // 上游这里原本是「NODE_ENV 不是 development 就直接 throw」。systemd 起的是
  // `pnpm start`，NODE_ENV=production，所以照原样部署这条路由必然 500。
  //
  // 上游的警告本身是对的：这个端点谁调谁就能拿到房间 token，没有任何鉴权。
  // 所以不是把它删掉，而是换成一个**必须显式打开**的开关 —— 默认仍然拒绝，
  // 谁开的、在哪开的都留痕（.env.local 里那一行）。
  // 真要长期公开，前面得加一层（IAP / Caddy basicauth / 自己的登录态）。
  if (
    process.env.NODE_ENV !== 'development' &&
    process.env.IS_VERCEL_PREVIEW !== 'true' &&
    process.env.ALLOW_INSECURE_TOKEN !== 'true'
  ) {
    throw new Error(
      'THIS API ROUTE IS INSECURE. DO NOT USE THIS ROUTE IN PRODUCTION WITHOUT AN AUTHENTICATION LAYER.'
    );
  }

  try {
    if (LIVEKIT_URL === undefined) {
      throw new Error('LIVEKIT_URL is not defined');
    }
    if (API_KEY === undefined) {
      throw new Error('LIVEKIT_API_KEY is not defined');
    }
    if (API_SECRET === undefined) {
      throw new Error('LIVEKIT_API_SECRET is not defined');
    }

    // Parse room config from request body.
    const body = await req.json();
    const roomConfig = body?.room_config
      ? RoomConfiguration.fromJson(body.room_config, { ignoreUnknownFields: true })
      : new RoomConfiguration();

    // ── 房间名从查询串来：`/api/token?room=bunny` ────────────────────
    // 一个 bot 一个房间，手机和笔记本进同一个房间、各自是一个参与者。
    // 不带 ?room= 时保持上游原来的随机房间行为 —— 老链接不受影响。
    //
    // 房间名会一路传到 agent 那边被拿去拼人格文件路径，所以必须白名单，
    // 不能只做字符校验：任何人都能调这个端点，`?room=<别人的 bot>` 等于
    // 直接拨进别人的助理。
    const roomParam = new URL(req.url).searchParams.get('room');
    let roomName: string;
    if (roomParam) {
      // 配置缺失就 500，**不要**悄悄退回随机房间 —— 那会让「白名单没配」
      // 表现成「功能好像能用但每次都是新房间」，最难查的那种。
      if (!process.env.ALLOWED_ROOMS) {
        throw new Error('ALLOWED_ROOMS is not defined but ?room= was requested');
      }
      const allowed = process.env.ALLOWED_ROOMS.split(',')
        .map((s) => s.trim())
        .filter(Boolean);
      if (!allowed.includes(roomParam)) {
        return new NextResponse(`room not allowed: ${roomParam}`, { status: 400 });
      }
      roomName = roomParam;
    } else {
      roomName = `voice_assistant_room_${Math.floor(Math.random() * 10_000)}`;
    }

    // Generate participant token
    const participantName = 'user';
    // 上游用 `Math.random() * 10_000` 生成 identity。以前每个窗口都是自己的
    // 随机房间，撞车也没人看得见；现在大家进同一个房间，**撞 identity 就是
    // 事故** —— LiveKit 规定一个房间里同一个 identity 只能有一个连接，
    // 手机一进来就把笔记本踢下线，而且看起来像「随机掉线」。
    // 1/10000 在两台设备上不算小，换成 UUID。
    const participantIdentity = `voice_assistant_user_${crypto.randomUUID()}`;

    const participantToken = await createParticipantToken(
      { identity: participantIdentity, name: participantName },
      roomName,
      roomConfig
    );

    // Return connection details
    const data: ConnectionDetails = {
      serverUrl: LIVEKIT_URL,
      roomName,
      participantName,
      participantToken,
    };
    const headers = new Headers({
      'Cache-Control': 'no-store',
    });
    return NextResponse.json(data, { headers });
  } catch (error) {
    if (error instanceof Error) {
      console.error(error);
      return new NextResponse(error.message, { status: 500 });
    }
  }
}

function createParticipantToken(
  userInfo: AccessTokenOptions,
  roomName: string,
  roomConfig: RoomConfiguration | undefined
): Promise<string> {
  const at = new AccessToken(API_KEY, API_SECRET, {
    ...userInfo,
    ttl: '15m',
  });
  const grant: VideoGrant = {
    room: roomName,
    roomJoin: true,
    canPublish: true,
    canPublishData: true,
    canSubscribe: true,
  };
  at.addGrant(grant);

  if (roomConfig) {
    at.roomConfig = roomConfig;
  }

  return at.toJwt();
}

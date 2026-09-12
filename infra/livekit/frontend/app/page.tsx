import { App } from '@/components/app/app';

// Next.js 15 起 `searchParams` 是个 Promise，必须 await ——
// 直接当对象用编译期就报错，不是运行时才发现。
export default async function Page({ searchParams }: { searchParams: Promise<{ room?: string }> }) {
  const { room } = await searchParams;
  return <App agentName={process.env.AGENT_NAME} roomName={room} />;
}

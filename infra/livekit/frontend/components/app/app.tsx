'use client';

import { useMemo } from 'react';
import { TokenSource } from 'livekit-client';
import { useSession } from '@livekit/components-react';
import { WarningIcon } from '@phosphor-icons/react/dist/ssr';
import { AgentSessionProvider } from '@/components/agents-ui/agent-session-provider';
import { StartAudioButton } from '@/components/agents-ui/start-audio-button';
import { ViewController } from '@/components/app/view-controller';
import { Toaster } from '@/components/ui/sonner';
import { useAgentErrors } from '@/hooks/useAgentErrors';
import { useDebugMode } from '@/hooks/useDebug';

const IN_DEVELOPMENT = process.env.NODE_ENV !== 'production';

function AppSetup() {
  useDebugMode({ enabled: IN_DEVELOPMENT });
  useAgentErrors();

  return null;
}

interface AppProps {
  agentName?: string;
  /** 网址上的 `?room=` —— 决定进哪个 bot 的房间。不给就让后端随机开一个。 */
  roomName?: string;
}

export function App({ agentName, roomName }: AppProps) {
  // 房间名只能走查询串：`TokenSource.endpoint()` 只收一个 URL，
  // 没有给请求体加字段的钩子（想塞 body 得自己实现整个 TokenSource）。
  const tokenSource = useMemo(
    () =>
      TokenSource.endpoint(
        roomName ? `/api/token?room=${encodeURIComponent(roomName)}` : '/api/token'
      ),
    [roomName]
  );

  const session = useSession(tokenSource, agentName ? { agentName } : undefined);

  return (
    <AgentSessionProvider session={session}>
      <AppSetup />
      <main className="grid h-svh grid-cols-1 place-content-center">
        <ViewController />
      </main>
      <StartAudioButton label="Start Audio" />
      <Toaster
        icons={{
          warning: <WarningIcon weight="bold" />,
        }}
        position="top-center"
        className="toaster group"
        style={
          {
            '--normal-bg': 'var(--popover)',
            '--normal-text': 'var(--popover-foreground)',
            '--normal-border': 'var(--border)',
          } as React.CSSProperties
        }
      />
    </AgentSessionProvider>
  );
}

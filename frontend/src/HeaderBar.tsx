import { Share2, PanelRightClose, PanelRight } from 'lucide-react'
import { VoiceUser } from './useVoiceChat'

const USER_COLORS = [
  'hsl(142,70%,50%)',
  'hsl(200,80%,55%)',
  'hsl(30,90%,55%)',
  'hsl(260,70%,60%)',
  'hsl(340,80%,60%)',
]

interface HeaderBarProps {
  users: VoiceUser[]
  username: string
  isConnected: boolean
  isListenerActive: boolean
  sidebarOpen: boolean
  onToggleSidebar: () => void
}

export default function HeaderBar({
  users,
  username,
  isConnected,
  isListenerActive,
  sidebarOpen,
  onToggleSidebar,
}: HeaderBarProps) {
  return (
    <header className="h-12 flex items-center justify-between px-4 bg-card border-b border-border shrink-0">
      <div className="flex items-center gap-3">
        <div className="w-7 h-7 rounded-lg bg-primary flex items-center justify-center">
          <span className="text-primary-foreground text-xs font-bold">✦</span>
        </div>
        <h1 className="text-sm font-semibold text-foreground">AI Brainstorm Canvas</h1>
        <div className="flex items-center gap-1.5">
          <span
            className={`w-1.5 h-1.5 rounded-full transition-colors ${
              isConnected ? 'bg-online' : 'bg-muted-foreground'
            }`}
          />
          <span className="text-[10px] text-muted-foreground">
            {isListenerActive ? 'agent acting…' : isConnected ? 'Live' : 'Connecting…'}
          </span>
        </div>
      </div>

      <div className="flex items-center gap-3">
        {users.length > 0 && (
          <div className="flex items-center -space-x-2">
            {users.slice(0, 5).map((u, i) => (
              <div key={u.username} className="relative">
                <div
                  className="w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold border-2 border-card"
                  style={{ backgroundColor: u.isAI ? 'hsl(270,70%,55%)' : USER_COLORS[i % USER_COLORS.length] }}
                >
                  <span className="text-[11px] font-bold" style={{ color: '#0a0a0f' }}>
                    {u.isAI ? '✦' : u.username[0].toUpperCase()}
                  </span>
                </div>
                {u.speaking && (
                  <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-online border-2 border-card" />
                )}
                {u.username === username && (
                  <span className="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-primary border-2 border-card" />
                )}
              </div>
            ))}
          </div>
        )}

        <div className="w-px h-5 bg-border" />

        <button className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-secondary text-secondary-foreground hover:bg-muted transition-colors">
          <Share2 className="w-3.5 h-3.5" />
          Share
        </button>

        <button
          onClick={onToggleSidebar}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-secondary text-secondary-foreground hover:bg-muted transition-colors"
        >
          {sidebarOpen ? (
            <PanelRightClose className="w-3.5 h-3.5" />
          ) : (
            <PanelRight className="w-3.5 h-3.5" />
          )}
          Voice
        </button>
      </div>
    </header>
  )
}

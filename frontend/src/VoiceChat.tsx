import { useRef, useEffect, useState, ChangeEvent } from 'react'
import {
  Mic, MicOff, ChevronDown, ChevronUp,
  Video, VideoOff, Volume2, PhoneOff, Send, ImagePlus,
} from 'lucide-react'
import { VoiceUser, TranscriptEntry, ChatMessage } from './useVoiceChat'
import { GenerationSettings } from './GenerationContext'

const USER_COLORS = [
  'hsl(142,70%,50%)',
  'hsl(200,80%,55%)',
  'hsl(30,90%,55%)',
  'hsl(260,70%,60%)',
  'hsl(340,80%,60%)',
]
const AI_COLOR = 'hsl(270,70%,55%)'

interface VoiceChatProps {
  users: VoiceUser[]
  transcripts: TranscriptEntry[]
  chatMessages: ChatMessage[]
  sendChatMessage: (content: string, msgType?: 'text' | 'image') => void
  isMuted: boolean
  isConnected: boolean
  isListenerActive: boolean
  toggleMute: () => void
  username?: string
  settings: GenerationSettings
  setSettings: (patch: Partial<GenerationSettings>) => void
}

function formatTime(ts: number) {
  const d = new Date(ts)
  return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0')
}

export default function VoiceChat({
  users,
  transcripts,
  chatMessages,
  sendChatMessage,
  isMuted,
  isConnected,
  isListenerActive,
  toggleMute,
  username = '',
  settings,
  setSettings,
}: VoiceChatProps) {
  const transcriptRef = useRef<HTMLDivElement>(null)
  const chatEndRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const [transcriptExpanded, setTranscriptExpanded] = useState(false)
  const [settingsExpanded, setSettingsExpanded] = useState(false)
  const [chatInput, setChatInput] = useState('')
  const [isCameraOn, setIsCameraOn] = useState(false)

  const projectName = 'First project'

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatMessages])

  useEffect(() => {
    if (transcriptExpanded) {
      transcriptRef.current?.scrollTo({ top: transcriptRef.current.scrollHeight, behavior: 'smooth' })
    }
  }, [transcripts, transcriptExpanded])

  function sendChat() {
    const text = chatInput.trim()
    if (!text) return
    sendChatMessage(text, 'text')
    setChatInput('')
  }

  function handleImageSelect(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    if (file.size > 2 * 1024 * 1024) {
      alert('Image too large — max 2 MB')
      return
    }
    const reader = new FileReader()
    reader.onloadend = () => {
      sendChatMessage(reader.result as string, 'image')
    }
    reader.readAsDataURL(file)
    // Reset so the same file can be sent again
    e.target.value = ''
  }

  return (
    <div className="h-full flex flex-col bg-card overflow-hidden">

      {/* ── Call interface ──────────────────────────────────────── */}
      <div className="shrink-0 bg-[hsl(240,15%,10%)] rounded-xl mx-3 mt-3 p-4 flex flex-col items-center gap-3">

        {/* Project label */}
        <span className="text-xs font-semibold text-secondary-foreground bg-secondary/60 rounded-full px-3 py-0.5">
          {projectName}
          {isListenerActive && (
            <span className="ml-2 text-[9px] text-ai-border italic font-normal">agent acting…</span>
          )}
        </span>

        {/* User avatars */}
        <div className="flex items-center gap-3 flex-wrap justify-center min-h-[56px]">
          {users.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">
              {isConnected ? 'No users connected…' : 'Connecting…'}
            </p>
          ) : (
            users.map((u, i) => (
              <div key={u.username} className="flex flex-col items-center gap-1">
                <div className="relative">
                  <div
                    className={`w-14 h-14 rounded-full flex items-center justify-center text-base font-bold transition-shadow ${
                      u.speaking ? 'shadow-[0_0_0_3px_hsl(142,70%,50%)]' : ''
                    }`}
                    style={{ backgroundColor: u.isAI ? AI_COLOR : USER_COLORS[i % USER_COLORS.length] }}
                  >
                    <span style={{ color: '#0a0a0f' }}>
                      {u.isAI ? '✦' : u.username[0].toUpperCase()}
                    </span>
                  </div>
                  {/* Muted badge */}
                  {isMuted && u.username === username && (
                    <span className="absolute bottom-0 right-0 w-5 h-5 rounded-full bg-card border border-border flex items-center justify-center">
                      <MicOff className="w-2.5 h-2.5 text-destructive" />
                    </span>
                  )}
                  {/* AI pulse indicator */}
                  {u.isAI && (
                    <span className="absolute bottom-0 right-0 w-3.5 h-3.5 rounded-full border-2 border-card bg-[hsl(270,70%,55%)] animate-pulse" />
                  )}
                </div>
                <span className="text-[10px] text-muted-foreground max-w-[64px] truncate text-center">
                  {u.isAI ? 'Higgs AI' : (u.username === username ? 'You' : u.username.split(' ')[0])}
                </span>
              </div>
            ))
          )}
        </div>

        {/* Call control buttons row */}
        <div className="flex items-center gap-2">
          {/* Mic */}
          <button
            onClick={toggleMute}
            title={isMuted ? 'Unmute' : 'Mute'}
            className={`w-9 h-9 rounded-lg flex items-center justify-center transition-colors ${
              isMuted
                ? 'bg-destructive/20 text-destructive border border-destructive/40 hover:bg-destructive/30'
                : 'bg-secondary text-secondary-foreground border border-border hover:bg-secondary/70'
            }`}
          >
            {isMuted ? <MicOff className="w-4 h-4" /> : <Mic className="w-4 h-4" />}
          </button>

          {/* Camera */}
          <button
            onClick={() => setIsCameraOn((v) => !v)}
            title={isCameraOn ? 'Turn off camera' : 'Turn on camera'}
            className={`w-9 h-9 rounded-lg flex items-center justify-center transition-colors ${
              isCameraOn
                ? 'bg-secondary text-secondary-foreground border border-border hover:bg-secondary/70'
                : 'bg-secondary/40 text-muted-foreground border border-border/50 hover:bg-secondary/60'
            }`}
          >
            {isCameraOn ? <Video className="w-4 h-4" /> : <VideoOff className="w-4 h-4" />}
          </button>

          {/* Speaker */}
          <button
            title="Audio settings"
            className="w-9 h-9 rounded-lg flex items-center justify-center bg-secondary text-secondary-foreground border border-border hover:bg-secondary/70 transition-colors"
          >
            <Volume2 className="w-4 h-4" />
          </button>

          {/* End call */}
          <button
            title="Leave call"
            className="w-9 h-9 rounded-lg flex items-center justify-center bg-destructive text-white hover:bg-destructive/80 transition-colors"
          >
            <PhoneOff className="w-4 h-4" />
          </button>
        </div>

        {/* Mute status pill */}
        <button
          onClick={toggleMute}
          className={`text-[11px] font-medium px-3 py-1 rounded-full border transition-colors ${
            isMuted
              ? 'bg-destructive/10 text-destructive border-destructive/30 hover:bg-destructive/20'
              : 'bg-online/10 text-online border-online/30 hover:bg-online/20'
          }`}
        >
          {isMuted ? 'Muted — click to unmute' : 'Live — click to mute'}
        </button>
      </div>

      {/* ── Transcript (collapsible, compact) ──────────────────── */}
      <div className="shrink-0 border-b border-border">
        <button
          onClick={() => setTranscriptExpanded(!transcriptExpanded)}
          className="w-full flex items-center justify-between px-4 py-2 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider hover:bg-secondary/30 transition-colors"
        >
          <span>Transcript</span>
          {transcriptExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        </button>
        {transcriptExpanded && (
          <div ref={transcriptRef} className="max-h-24 overflow-y-auto px-3 pb-2 space-y-1.5">
            {transcripts.length === 0 ? (
              <p className="text-xs text-muted-foreground italic">nothing yet…</p>
            ) : (
              transcripts.map((entry, i) => (
                <div key={i} className="flex gap-2">
                  <span
                    className="text-[10px] font-semibold shrink-0"
                    style={{ color: entry.username === username ? 'hsl(200,80%,55%)' : 'hsl(var(--muted-foreground))' }}
                  >
                    {entry.username.split(' ')[0]}:
                  </span>
                  <span className="text-[10px] text-secondary-foreground leading-relaxed break-words">{entry.text}</span>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      {/* ── Chat ───────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-h-0 border-b border-border">
        <div className="px-4 py-2 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider shrink-0">
          Team Chat
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-3 py-1 space-y-2">
          {chatMessages.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">no messages yet…</p>
          ) : (
            chatMessages.map((msg) => {
              const isMe = msg.username === username
              const isAI = msg.isAI
              return (
                <div key={msg.id} className={`flex flex-col gap-0.5 ${isMe ? 'items-end' : 'items-start'}`}>
                  {!isMe && (
                    <span
                      className="text-[10px] px-1 font-semibold"
                      style={{ color: isAI ? AI_COLOR : 'hsl(var(--muted-foreground))' }}
                    >
                      {isAI ? '✦ Higgs AI' : msg.username.split(' ')[0]}
                    </span>
                  )}
                  <div className={`flex items-end gap-1.5 ${isMe ? 'flex-row-reverse' : ''}`}>
                    {msg.msgType === 'image' ? (
                      <img
                        src={msg.content}
                        alt="shared"
                        className="max-w-[200px] max-h-[200px] rounded-xl object-cover"
                      />
                    ) : (
                      <div
                        className={`max-w-[200px] px-3 py-2 rounded-2xl text-xs leading-relaxed break-words ${
                          isAI
                            ? 'bg-[hsl(270,70%,55%)]/15 text-foreground border border-[hsl(270,70%,55%)]/30 rounded-tl-sm'
                            : isMe
                            ? 'bg-ai-border/20 text-foreground rounded-tr-sm'
                            : 'bg-secondary text-secondary-foreground rounded-tl-sm'
                        }`}
                      >
                        {msg.content}
                      </div>
                    )}
                    <span className="text-[9px] text-muted-foreground shrink-0">{formatTime(msg.ts)}</span>
                  </div>
                </div>
              )
            })
          )}
          <div ref={chatEndRef} />
        </div>

        {/* Input */}
        <div className="shrink-0 px-3 py-2 flex items-center gap-2">
          {/* Image upload button */}
          <button
            onClick={() => fileInputRef.current?.click()}
            title="Attach image"
            className="text-muted-foreground hover:text-secondary-foreground transition-colors"
          >
            <ImagePlus className="w-4 h-4" />
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={handleImageSelect}
          />

          <input
            type="text"
            value={chatInput}
            onChange={(e) => setChatInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && sendChat()}
            placeholder="Type something…"
            className="flex-1 bg-transparent text-xs text-foreground placeholder:text-muted-foreground outline-none"
          />
          <button
            onClick={sendChat}
            disabled={!chatInput.trim()}
            className="w-7 h-7 rounded-full bg-secondary flex items-center justify-center text-secondary-foreground hover:bg-secondary/70 disabled:opacity-40 transition-colors"
          >
            <Send className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* ── Generation Settings ─────────────────────────────────── */}
      <div className="shrink-0">
        <button
          onClick={() => setSettingsExpanded(!settingsExpanded)}
          className="w-full flex items-center justify-between px-4 py-2.5 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider hover:bg-secondary/30 transition-colors"
        >
          <span>Generation Settings</span>
          {settingsExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        </button>

        {settingsExpanded && (
          <div className="px-4 pb-4 space-y-3">
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1.5">Image</p>
              <div className="flex gap-2">
                <select
                  className="flex-1 bg-secondary text-secondary-foreground border border-border rounded-md px-2 py-1.5 text-xs outline-none focus:ring-1 focus:ring-ring cursor-pointer"
                  value={settings.imageModel}
                  onChange={(e) => setSettings({ imageModel: e.target.value as GenerationSettings['imageModel'] })}
                >
                  <option value="seedream">Seedream v4</option>
                  <option value="flux">Flux 2 Pro</option>
                </select>
                <select
                  className="bg-secondary text-secondary-foreground border border-border rounded-md px-2 py-1.5 text-xs outline-none focus:ring-1 focus:ring-ring cursor-pointer"
                  value={settings.imageResolution}
                  onChange={(e) => setSettings({ imageResolution: e.target.value })}
                >
                  <option value="1K">1K</option>
                  <option value="2K">2K</option>
                  <option value="4K">4K</option>
                </select>
                <select
                  className="bg-secondary text-secondary-foreground border border-border rounded-md px-2 py-1.5 text-xs outline-none focus:ring-1 focus:ring-ring cursor-pointer"
                  value={settings.imageAspectRatio}
                  onChange={(e) => setSettings({ imageAspectRatio: e.target.value })}
                >
                  <option value="16:9">16:9</option>
                  <option value="4:3">4:3</option>
                  <option value="1:1">1:1</option>
                  <option value="9:16">9:16</option>
                </select>
              </div>
            </div>

            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1.5">Video</p>
              <div className="flex gap-2">
                <select
                  className="flex-1 bg-secondary text-secondary-foreground border border-border rounded-md px-2 py-1.5 text-xs outline-none focus:ring-1 focus:ring-ring cursor-pointer"
                  value={settings.videoModel}
                  onChange={(e) => setSettings({ videoModel: e.target.value as GenerationSettings['videoModel'] })}
                >
                  <option value="dop_standard">DoP Standard</option>
                  <option value="dop_turbo">DoP Turbo</option>
                  <option value="kling">Kling 3.0</option>
                </select>
                <select
                  className="bg-secondary text-secondary-foreground border border-border rounded-md px-2 py-1.5 text-xs outline-none focus:ring-1 focus:ring-ring cursor-pointer"
                  value={settings.videoDuration}
                  onChange={(e) => setSettings({ videoDuration: Number(e.target.value) })}
                >
                  <option value={3}>3s</option>
                  <option value={5}>5s</option>
                </select>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

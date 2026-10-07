import { useEffect, useRef, useState, useCallback } from 'react'
import { StreamAction } from './api'

export type VoiceUser = {
  username: string
  speaking: boolean
  isAI?: boolean
}

export type TranscriptEntry = {
  username: string
  text: string
  ts: number
}

export type ChatMessage = {
  id: string
  username: string
  content: string
  msgType: 'text' | 'image'
  ts: number
  isAI?: boolean
}

export type VoiceChatCallbacks = {
  onAgentAction: (action: StreamAction) => void
  onCanvasRestoreFull: (snapshot: unknown) => void
  onCanvasSnapshot: (shapes: unknown[]) => void
}

export type CursorEntry = { x: number; y: number }

type UseVoiceChatReturn = {
  users: VoiceUser[]
  transcripts: TranscriptEntry[]
  cursors: Record<string, CursorEntry>
  chatMessages: ChatMessage[]
  isMuted: boolean
  isConnected: boolean
  isListenerActive: boolean
  toggleMute: () => void
  sendWsMessage: (msg: Record<string, unknown>) => void
  sendChatMessage: (content: string, msgType?: 'text' | 'image') => void
}

const _API_BASE = (import.meta.env.VITE_API_URL ?? '').replace(/\/$/, '')
const WS_URL = (_API_BASE || window.location.origin).replace(/^http/, 'ws') + '/ws'
const FALLBACK_ICE: RTCIceServer[] = [{ urls: 'stun:stun.l.google.com:19302' }]

async function fetchIceServers(): Promise<RTCIceServer[]> {
  const domain = import.meta.env.VITE_METERED_DOMAIN
  const apiKey = import.meta.env.VITE_METERED_API_KEY
  if (!domain || !apiKey) return FALLBACK_ICE
  try {
    const res = await fetch(`https://${domain}/api/v1/turn/credentials?apiKey=${apiKey}`)
    const servers = await res.json()
    return Array.isArray(servers) && servers.length > 0 ? servers : FALLBACK_ICE
  } catch {
    return FALLBACK_ICE
  }
}
const MAX_TRANSCRIPTS = 20
const CHUNK_INTERVAL_MS = 5000
const RECORDER_WARMUP_MS = 300

export function useVoiceChat(
  roomId: string,
  username: string,
  callbacks: VoiceChatCallbacks,
): UseVoiceChatReturn {
  const [users, setUsers] = useState<VoiceUser[]>([])
  const [transcripts, setTranscripts] = useState<TranscriptEntry[]>([])
  const [cursors, setCursors] = useState<Record<string, CursorEntry>>({})
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([])
  const [isMuted, setIsMuted] = useState(false)
  const [isConnected, setIsConnected] = useState(false)
  const [isListenerActive, setIsListenerActive] = useState(false)

  const wsRef = useRef<WebSocket | null>(null)
  const peersRef = useRef<Map<string, RTCPeerConnection>>(new Map())
  const audioElemsRef = useRef<Map<string, HTMLAudioElement>>(new Map())
  const localStreamRef = useRef<MediaStream | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const isMutedRef = useRef(false)
  const iceServersRef = useRef<RTCIceServer[]>(FALLBACK_ICE)
const livekitRoomRef = useRef<any>(null)
  const speakingTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const audioCtxRef = useRef<AudioContext | null>(null)
  const nextPlayTimeRef = useRef<number>(0)
  // Keep callbacks in a ref so the WS onmessage closure always sees the latest version.
  const callbacksRef = useRef(callbacks)
  callbacksRef.current = callbacks

  const addTranscript = useCallback((entry: TranscriptEntry) => {
    setTranscripts((prev) => [...prev.slice(-(MAX_TRANSCRIPTS - 1)), entry])
  }, [])

  const setSpeaking = useCallback((u: string, speaking: boolean) => {
    setUsers((prev) =>
      prev.map((user) => (user.username === u ? { ...user, speaking } : user)),
    )
  }, [])

  const clearSpeakingTimer = useCallback((u: string) => {
    const t = speakingTimersRef.current.get(u)
    if (t) clearTimeout(t)
  }, [])

  const markSpeaking = useCallback(
    (u: string) => {
      clearSpeakingTimer(u)
      setSpeaking(u, true)
      const t = setTimeout(() => setSpeaking(u, false), 1500)
      speakingTimersRef.current.set(u, t)
    },
    [clearSpeakingTimer, setSpeaking],
  )

  const sendWsMessage = useCallback((msg: Record<string, unknown>) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg))
    }
  }, [])

  const sendChatMessage = useCallback((content: string, msgType: 'text' | 'image' = 'text') => {
    sendWsMessage({ type: 'chat_message', content, msgType, ts: Date.now() })
  }, [sendWsMessage])

  // --- WebRTC helpers ---

  const createPeer = useCallback(
    (remoteUsername: string, polite: boolean): RTCPeerConnection => {
      const pc = new RTCPeerConnection({ iceServers: iceServersRef.current })

      localStreamRef.current?.getTracks().forEach((track) => {
        pc.addTrack(track, localStreamRef.current!)
      })

      pc.onicecandidate = ({ candidate }) => {
        if (candidate && wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(JSON.stringify({ type: 'ice', to: remoteUsername, candidate }))
        }
      }

      pc.ontrack = ({ streams }) => {
        let audio = audioElemsRef.current.get(remoteUsername)
        if (!audio) {
          audio = new Audio()
          audioElemsRef.current.set(remoteUsername, audio)
        }
        audio.srcObject = streams[0]
        audio.play().catch(() => {})
      }

      if (!polite) {
        pc.onnegotiationneeded = async () => {
          try {
            const offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            wsRef.current?.send(
              JSON.stringify({ type: 'offer', to: remoteUsername, sdp: pc.localDescription }),
            )
          } catch (e) {
            console.error('offer error', e)
          }
        }
      }

      peersRef.current.set(remoteUsername, pc)
      return pc
    },
    [],
  )

  const closePeer = useCallback((remoteUsername: string) => {
    peersRef.current.get(remoteUsername)?.close()
    peersRef.current.delete(remoteUsername)
    const audio = audioElemsRef.current.get(remoteUsername)
    if (audio) {
      audio.srcObject = null
      audioElemsRef.current.delete(remoteUsername)
    }
  }, [])

  // --- MediaRecorder + VAD ---

  const startRecorder = useCallback((stream: MediaStream) => {
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : 'audio/webm'

    const audioCtx = new AudioContext()
    const analyser = audioCtx.createAnalyser()
    analyser.fftSize = 512
    const source = audioCtx.createMediaStreamSource(stream)
    source.connect(analyser)
    const dataArray = new Uint8Array(analyser.frequencyBinCount)

    function getRms(): number {
      analyser.getByteTimeDomainData(dataArray)
      let sum = 0
      for (const v of dataArray) {
        const normalized = v / 128 - 1
        sum += normalized * normalized
      }
      return Math.sqrt(sum / dataArray.length)
    }

    function startNewChunk() {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
      if (isMutedRef.current) {
        setTimeout(startNewChunk, CHUNK_INTERVAL_MS)
        return
      }

      const recorder = new MediaRecorder(stream, { mimeType })
      recorderRef.current = recorder

      let maxRms = 0
      let sampleInterval: ReturnType<typeof setInterval>

      recorder.ondataavailable = (e) => {
        clearInterval(sampleInterval)
        const hasVoice = maxRms > 0.08
        console.log(`[vad] maxRms=${maxRms.toFixed(4)} hasVoice=${hasVoice}`)
        if (!hasVoice || e.data.size < 100) {
          startNewChunk()
          return
        }
        const reader = new FileReader()
        reader.onloadend = () => {
          const b64 = (reader.result as string).split(',')[1]
          if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({ type: 'audio_chunk', data: b64 }))
          }
          startNewChunk()
        }
        reader.readAsDataURL(e.data)
      }

      recorder.start()
      setTimeout(() => {
        sampleInterval = setInterval(() => {
          maxRms = Math.max(maxRms, getRms())
        }, 200)
        setTimeout(() => {
          if (recorder.state === 'recording') recorder.stop()
        }, CHUNK_INTERVAL_MS)
      }, RECORDER_WARMUP_MS)
    }

    startNewChunk()
  }, [])


  // --- Main WS + setup effect ---

  useEffect(() => {
    let cancelled = false

    async function setup() {
      iceServersRef.current = await fetchIceServers()

      let stream: MediaStream
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false })
      } catch (e) {
        console.warn('Microphone access denied, voice disabled', e)
        stream = new MediaStream()
      }
      localStreamRef.current = stream
      if (cancelled) {
        stream.getTracks().forEach((t) => t.stop())
        return
      }

      const ws = new WebSocket(`${WS_URL}/${roomId}/${encodeURIComponent(username)}`)
      wsRef.current = ws

      ws.onopen = () => {
        if (!cancelled) {
          setIsConnected(true)
          startRecorder(stream)
          // audio_relay disabled — LiveKit handles peer audio
        }
      }

      ws.onerror = (e) => console.error('[ws] error', e)

      ws.onclose = () => {
        if (!cancelled) setIsConnected(false)
      }

      ws.onmessage = async (evt) => {
        if (cancelled) return
        let msg: Record<string, any>
        try {
          msg = JSON.parse(evt.data)
        } catch {
          return
        }
        const t = msg.type

        if (t === 'room_update') {
          const incoming: VoiceUser[] = (msg.users as { username: string; isAI?: boolean }[]).map((u) => ({
            username: u.username,
            speaking: false,
            isAI: u.isAI ?? false,
          }))
          setUsers(incoming)
        } else if (t === 'existing_peers') {
          for (const peer of msg.peers as string[]) {
            if (!peersRef.current.has(peer)) createPeer(peer, false)
          }
        } else if (t === 'canvas_restore_full') {
          if (msg.snapshot) {
            callbacksRef.current.onCanvasRestoreFull(msg.snapshot)
          }
        } else if (t === 'canvas_snapshot') {
          callbacksRef.current.onCanvasSnapshot((msg.shapes ?? []) as unknown[])
        } else if (t === 'offer') {
          const from: string = msg.from
          let pc = peersRef.current.get(from)
          if (!pc) pc = createPeer(from, true)
          await pc.setRemoteDescription(new RTCSessionDescription(msg.sdp))
          const answer = await pc.createAnswer()
          await pc.setLocalDescription(answer)
          ws.send(JSON.stringify({ type: 'answer', to: from, sdp: pc.localDescription }))
        } else if (t === 'answer') {
          const pc = peersRef.current.get(msg.from)
          if (pc) await pc.setRemoteDescription(new RTCSessionDescription(msg.sdp))
        } else if (t === 'ice') {
          const pc = peersRef.current.get(msg.from)
          if (pc && msg.candidate) {
            try {
              await pc.addIceCandidate(new RTCIceCandidate(msg.candidate))
            } catch (_) {}
          }
        } else if (t === 'transcript') {
          markSpeaking(msg.username)
          addTranscript({ username: msg.username, text: msg.text, ts: Date.now() })
        } else if (t === 'agent_action') {
          console.log('[agent_action] received:', msg.action)
          setIsListenerActive(true)
          callbacksRef.current.onAgentAction(msg.action as StreamAction)
          setTimeout(() => setIsListenerActive(false), 2000)
        } else if (t === 'audio_relay') {
          try {
            const bin = atob(msg.data as string)
            const bytes = new Uint8Array(bin.length)
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)

            if (!audioCtxRef.current) {
              audioCtxRef.current = new AudioContext({ latencyHint: 'interactive' })
            }
            const ctx = audioCtxRef.current
            if (ctx.state === 'suspended') ctx.resume()

            ctx.decodeAudioData(bytes.buffer.slice(0), (buffer) => {
              const source = ctx.createBufferSource()
              source.buffer = buffer
              source.connect(ctx.destination)
              const now = ctx.currentTime
              // Schedule back-to-back; catch up immediately if we fall behind
              const startAt = Math.max(now + 0.01, nextPlayTimeRef.current)
              source.start(startAt)
              nextPlayTimeRef.current = startAt + buffer.duration
            })
          } catch {}
        } else if (t === 'chat_message') {
          setChatMessages((prev) => [
            ...prev.slice(-199),
            {
              id: crypto.randomUUID(),
              username: msg.username as string,
              content: msg.content as string,
              msgType: (msg.msgType as 'text' | 'image') ?? 'text',
              ts: msg.ts as number,
              isAI: (msg.isAI as boolean) ?? false,
            },
          ])
        } else if (t === 'cursor_move') {
          setCursors((prev) => ({ ...prev, [msg.username as string]: { x: msg.x as number, y: msg.y as number } }))
        } else if (t === 'user_left') {
          closePeer(msg.username)
          setCursors((prev) => { const next = { ...prev }; delete next[msg.username as string]; return next })
        }
      }
    }

    setup()

    return () => {
      cancelled = true
      recorderRef.current?.stop()
      peersRef.current.forEach((pc) => pc.close())
      peersRef.current.clear()
      audioElemsRef.current.forEach((a) => {
        a.srcObject = null
      })
      audioElemsRef.current.clear()
      wsRef.current?.close()
      localStreamRef.current?.getTracks().forEach((t) => t.stop())
    }
  }, [roomId, username]) // eslint-disable-line react-hooks/exhaustive-deps

  // --- LiveKit audio call ---
  useEffect(() => {
    let cancelled = false

    async function joinLiveKit() {
      try {
        const res = await fetch(`${_API_BASE}/api/livekit-token?room=${roomId}&username=${encodeURIComponent(username)}`)
        const { token, url } = await res.json()
        if (!token || !url || cancelled) return

        const { Room } = await import('livekit-client')
        const room = new Room()
        livekitRoomRef.current = room
        await room.connect(url, token)
        await room.localParticipant.setMicrophoneEnabled(true)
      } catch (e) {
        console.warn('[livekit] failed to connect', e)
      }
    }

    joinLiveKit()

    return () => {
      cancelled = true
      livekitRoomRef.current?.disconnect()
      livekitRoomRef.current = null
    }
  }, [roomId, username])

  const toggleMute = useCallback(() => {
    const next = !isMutedRef.current
    isMutedRef.current = next
    setIsMuted(next)
    localStreamRef.current?.getAudioTracks().forEach((t) => { t.enabled = !next })
    livekitRoomRef.current?.localParticipant.setMicrophoneEnabled(!next)
  }, [])

  return { users, transcripts, cursors, chatMessages, isMuted, isConnected, isListenerActive, toggleMute, sendWsMessage, sendChatMessage }
}

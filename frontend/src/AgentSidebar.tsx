import { MutableRefObject, useState, useRef, useEffect, KeyboardEvent } from 'react'
import { Editor, createShapeId, TLShapeId, toRichText, AssetRecordType } from 'tldraw'
import { Send, Paperclip, Lock } from 'lucide-react'
import { streamMessage, startGeneration, proxyImageUrl, proxyUrl, StreamAction } from './api'
import { getCanvasSnapshot } from './canvasUtils'
import { useGenerationContext, GenerationSettings } from './GenerationContext'

interface AgentSidebarProps {
  editorRef: MutableRefObject<Editor | null>
  externalNotice?: {
    id: string
    content: string
  } | null
  onExternalNoticeConsumed?: (id: string) => void
}

type ChatMessage = {
  role: 'user' | 'assistant'
  content: string
}

function agentId(id: string): TLShapeId {
  return (id.startsWith('shape:') ? id : `shape:${id}`) as TLShapeId
}

function findFreePosition(
  editor: Editor,
  nearX: number,
  nearY: number,
  excludeId?: TLShapeId
): { x: number; y: number } {
  const shapes = editor.getCurrentPageShapes().filter((s) => s.id !== excludeId)
  const SIZE = 64
  const offsets = [
    [-90, -90], [0, -110], [90, -90],
    [-110, 0], [110, 0],
    [-90, 90], [0, 110], [90, 90],
  ]
  for (const [dx, dy] of offsets) {
    const x = nearX + dx
    const y = nearY + dy
    const clear = shapes.every((shape) => {
      const b = editor.getShapePageBounds(shape)
      if (!b) return true
      return x + SIZE < b.minX || x > b.maxX || y + SIZE < b.minY || y > b.maxY
    })
    if (clear) return { x, y }
  }
  return { x: nearX - 90, y: nearY - 110 }
}

function dimensionsFromAspectRatio(aspectRatio: string): { w: number; h: number } {
  const [wr, hr] = aspectRatio.split(':').map(Number)
  if (!wr || !hr) return { w: 640, h: 360 }
  const BASE = 640
  if (hr > wr) return { w: Math.round(BASE * wr / hr), h: BASE }
  return { w: BASE, h: Math.round(BASE * hr / wr) }
}

function ensureAiCircle(
  editor: Editor,
  circleRef: { current: TLShapeId | null },
  nearX: number,
  nearY: number
) {
  const pos = findFreePosition(editor, nearX, nearY, circleRef.current ?? undefined)
  if (circleRef.current && editor.getShape(circleRef.current)) {
    editor.updateShapes([{ id: circleRef.current, type: 'geo', x: pos.x, y: pos.y }])
  } else {
    const id = createShapeId()
    editor.createShapes([{
      id,
      type: 'geo',
      x: pos.x,
      y: pos.y,
      props: {
        geo: 'ellipse' as const,
        w: 64, h: 64,
        richText: toRichText('AI'),
        color: 'violet' as any,
        fill: 'solid' as const,
      },
    }])
    circleRef.current = id
  }
}

export function applyAction(
  editor: Editor,
  action: StreamAction,
  onGenerationComplete: (gen: import('./GenerationContext').PendingGeneration) => void,
  onThinkingStart: (gen: import('./GenerationContext').ThinkingGeneration) => void,
  onThinkingEnd: (id: string) => void,
  settings: GenerationSettings,
) {
  const t = action._type

  if (t === 'create_note') {
    const id = action.shapeId ? agentId(action.shapeId as string) : createShapeId()
    editor.createShapes([{
      id,
      type: 'note',
      x: action.x as number,
      y: action.y as number,
      props: {
        richText: toRichText((action.text as string) ?? ''),
        color: ((action.color as string) ?? 'yellow') as any,
        size: 'm',
      },
    }])

  } else if (t === 'create_shape') {
    const id = action.shapeId ? agentId(action.shapeId as string) : createShapeId()
    editor.createShapes([{
      id,
      type: 'geo',
      x: action.x as number,
      y: action.y as number,
      props: {
        geo: ((action.geo as string) ?? 'rectangle') as any,
        w: (action.w as number) ?? 160,
        h: (action.h as number) ?? 80,
        richText: toRichText((action.text as string) ?? ''),
        color: ((action.color as string) ?? 'blue') as any,
        fill: 'solid' as const,
      },
    }])

  } else if (t === 'create_text') {
    const id = action.shapeId ? agentId(action.shapeId as string) : createShapeId()
    editor.createShapes([{
      id,
      type: 'text',
      x: action.x as number,
      y: action.y as number,
      props: {
        richText: toRichText((action.text as string) ?? ''),
        color: ((action.color as string) ?? 'black') as any,
        size: 'm' as const,
        autoSize: true,
      },
    }])

  } else if (t === 'create_image') {
    const url = (action.url as string) ?? ''
    if (!url) return

    const id = action.shapeId ? agentId(action.shapeId as string) : createShapeId()
    const x = (action.x as number) ?? 200
    const y = (action.y as number) ?? 200
    const w = (action.w as number) ?? 300
    const h = (action.h as number) ?? 200
    const assetId = AssetRecordType.createId()

    editor.createAssets([{
      type: 'image',
      id: assetId,
      typeName: 'asset',
      props: {
        w,
        h,
        name: (action.shapeId as string) ?? 'moodboard-image',
        isAnimated: false,
        mimeType: 'image/jpeg',
        src: proxyImageUrl(url),
      },
      meta: { originalUrl: url },
    }])

    editor.createShapes([{
      id,
      type: 'image',
      x,
      y,
      props: {
        w,
        h,
        assetId,
        playing: false,
        url: '',
        crop: null,
        flipX: false,
        flipY: false,
      },
    }])

  } else if (t === 'create_arrow') {
    const id = action.shapeId ? agentId(action.shapeId as string) : createShapeId()
    const x1 = (action.x1 as number) ?? 0
    const y1 = (action.y1 as number) ?? 0
    const x2 = (action.x2 as number) ?? 200
    const y2 = (action.y2 as number) ?? 0
    const minX = Math.min(x1, x2)
    const minY = Math.min(y1, y2)

    editor.createShapes([{
      id,
      type: 'arrow',
      x: minX,
      y: minY,
      props: {
        start: { x: x1 - minX, y: y1 - minY },
        end: { x: x2 - minX, y: y2 - minY },
        richText: toRichText((action.text as string) ?? ''),
        color: ((action.color as string) ?? 'black') as any,
        arrowheadEnd: 'arrow' as const,
        arrowheadStart: 'none' as const,
        bend: 0,
      },
    }])

    const bindings: Parameters<typeof editor.createBindings>[0] = []
    if (action.fromId) {
      const fromId = agentId(action.fromId as string)
      if (editor.getShape(fromId)) {
        bindings.push({
          type: 'arrow',
          fromId: id,
          toId: fromId,
          props: { terminal: 'start', normalizedAnchor: { x: 0.5, y: 0.5 }, isExact: false, isPrecise: false },
        })
      }
    }
    if (action.toId) {
      const toId = agentId(action.toId as string)
      if (editor.getShape(toId)) {
        bindings.push({
          type: 'arrow',
          fromId: id,
          toId: toId,
          props: { terminal: 'end', normalizedAnchor: { x: 0.5, y: 0.5 }, isExact: false, isPrecise: false },
        })
      }
    }
    if (bindings.length > 0) editor.createBindings(bindings)

  } else if (t === 'move_shape') {
    const id = action.id as TLShapeId
    const shape = editor.getShape(id)
    if (shape) {
      editor.updateShapes([{ id: shape.id, type: shape.type, x: action.x as number, y: action.y as number }])
    }
  } else if (t === 'update_text') {
    const id = action.id as TLShapeId
    const shape = editor.getShape(id)
    if (shape) {
      editor.updateShapes([{ id: shape.id, type: shape.type, props: { richText: toRichText(action.text as string) } } as any])
    }
  } else if (t === 'delete_shape') {
    editor.deleteShapes([action.id as TLShapeId])

  } else if (t === 'generate_image') {
    const x = (action.x as number) ?? 200
    const y = (action.y as number) ?? 200
    const prompt = (action.prompt as string) ?? ''
    const thinkingId = `thinking-img-${Date.now()}`
    const { w, h } = dimensionsFromAspectRatio(settings.imageAspectRatio)

    const placeholderId = createShapeId()
    editor.createShapes([{
      id: placeholderId,
      type: 'geo',
      x, y,
      props: {
        geo: 'rectangle' as const,
        w, h,
        richText: toRichText(`Generating image…\n"${prompt.slice(0, 80)}"`),
        color: 'violet' as any,
        fill: 'semi' as const,
        dash: 'dashed' as const,
      },
    }])

    onThinkingStart({ id: thinkingId, x, y, w, h, prompt, type: 'image' })

    startGeneration({
      type: 'image', prompt, x, y,
      model: settings.imageModel,
      resolution: settings.imageResolution,
      aspect_ratio: settings.imageAspectRatio,
    }, (status) => {
      if (status.status !== 'completed' && status.status !== 'failed') return
      onThinkingEnd(thinkingId)
      if (status.status === 'failed') {
        editor.updateShapes([{
          id: placeholderId, type: 'geo',
          props: { richText: toRichText(`❌ Generation failed\n${status.error ?? ''}`), color: 'red' as any },
        }])
        return
      }
      editor.deleteShapes([placeholderId])
      if (status.url) {
        const imageId = createShapeId()
        const assetId = AssetRecordType.createId()
        editor.createAssets([{
          type: 'image', id: assetId, typeName: 'asset',
          props: { w, h, name: prompt.slice(0, 40), isAnimated: false, mimeType: 'image/png', src: proxyUrl(status.url!) },
          meta: { originalUrl: status.url },
        }])
        editor.createShapes([{
          id: imageId, type: 'image', x, y, opacity: 0.4,
          props: { w, h, assetId, playing: false, url: '', crop: null, flipX: false, flipY: false },
        }])
        onGenerationComplete({
          shapeId: imageId as unknown as string,
          assetId: assetId as unknown as string,
          x, y, w, h, prompt, mediaUrl: status.url, type: 'image',
        })
      }
    })

  } else if (t === 'generate_video') {
    const x = (action.x as number) ?? 400
    const y = (action.y as number) ?? 200
    const prompt = (action.prompt as string) ?? ''
    const sourceShapeId = action.sourceImageShapeId
      ? agentId(action.sourceImageShapeId as string)
      : undefined

    let imageUrl: string | undefined
    if (sourceShapeId) {
      const srcShape = editor.getShape(sourceShapeId)
      if (srcShape) {
        const srcProps = srcShape.props as Record<string, unknown>
        if (srcProps.assetId) {
          const asset = editor.getAsset(srcProps.assetId as any)
          if (asset?.props && 'src' in asset.props) {
            imageUrl = asset.props.src as string
          }
        }
      }
    }

    if (imageUrl) {
      const thinkingId = `thinking-vid-${Date.now()}`

      const placeholderId = createShapeId()
      editor.createShapes([{
        id: placeholderId,
        type: 'geo',
        x, y,
        props: {
          geo: 'rectangle' as const,
          w: 640, h: 360,
          richText: toRichText(`Generating video…\n"${prompt.slice(0, 80)}"`),
          color: 'violet' as any,
          fill: 'semi' as const,
          dash: 'dashed' as const,
        },
      }])

      onThinkingStart({ id: thinkingId, x, y, w: 640, h: 360, prompt, type: 'video' })

      startGeneration({
        type: 'video', prompt, x, y, image_url: imageUrl,
        model: settings.videoModel,
        duration: settings.videoDuration,
      }, (status) => {
        if (status.status !== 'completed' && status.status !== 'failed') return
        onThinkingEnd(thinkingId)
        if (status.status === 'failed') {
          editor.updateShapes([{
            id: placeholderId, type: 'geo',
            props: { richText: toRichText(`❌ Generation failed\n${status.error ?? ''}`), color: 'red' as any },
          }])
          return
        }
        editor.deleteShapes([placeholderId])
        if (status.url) {
          const videoId = createShapeId()
          const assetId = AssetRecordType.createId()
          editor.createAssets([{
            type: 'video', id: assetId, typeName: 'asset',
            props: { w: 640, h: 360, name: prompt.slice(0, 40), isAnimated: true, mimeType: 'video/mp4', src: proxyUrl(status.url!) },
            meta: { originalUrl: status.url },
          }])
          editor.createShapes([{
            id: videoId, type: 'video', x, y, opacity: 0.4,
            props: { w: 640, h: 360, assetId, playing: true, url: '' },
          }])
          onGenerationComplete({
            shapeId: videoId as unknown as string,
            assetId: assetId as unknown as string,
            x, y, w: 640, h: 360, prompt, mediaUrl: status.url, type: 'video',
          })
        }
      })
    }
  }
  // 'message' is handled in the sidebar
}

export default function AgentSidebar({
  editorRef,
  externalNotice = null,
  onExternalNoticeConsumed,
}: AgentSidebarProps) {
  const { onGenerationComplete, onThinkingStart, onThinkingEnd, settings } = useGenerationContext()
  const aiCircleRef = useRef<TLShapeId | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'assistant',
      content: "Hi! I'm your AI brainstorm partner. Tell me what to add to the canvas — I can create sticky notes, shapes, move things around, and help organize ideas.",
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    if (!externalNotice) return

    setMessages((prev) => [...prev, { role: 'assistant', content: externalNotice.content }])
    onExternalNoticeConsumed?.(externalNotice.id)
  }, [externalNotice, onExternalNoticeConsumed])

  async function handleSend() {
    const text = input.trim()
    if (!text || loading) return
    if (!editorRef.current) {
      console.warn('[chat] send blocked: editor not mounted yet')
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: 'Canvas is still loading. Try again in a moment.' },
      ])
      return
    }

    console.info('[chat] handleSend', { textLength: text.length })

    setInput('')
    setMessages((prev) => [...prev, { role: 'user', content: text }])
    setLoading(true)

    const canvasSnapshot = getCanvasSnapshot(editorRef.current)
    let gotMessage = false

    await streamMessage(
      text,
      canvasSnapshot,
      (action) => {
        if (action._type === 'message') {
          gotMessage = true
          setMessages((prev) => [...prev, { role: 'assistant', content: action.text as string }])
        } else if (editorRef.current) {
          const ax = (action.x as number) ?? (action.x1 as number) ?? null
          const ay = (action.y as number) ?? (action.y1 as number) ?? null
          if (ax !== null && ay !== null) {
            ensureAiCircle(editorRef.current, aiCircleRef, ax, ay)
          }
          applyAction(editorRef.current, action, onGenerationComplete, onThinkingStart, onThinkingEnd, settings)
        }
      },
      () => {
        if (!gotMessage) {
          setMessages((prev) => [...prev, { role: 'assistant', content: 'Done!' }])
        }
        setLoading(false)
      },
      (err) => {
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: `Error: ${err.message}` },
        ])
        setLoading(false)
      }
    )
  }

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="h-full flex flex-col bg-card border-t border-border">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-border shrink-0">
        <div className="flex items-center gap-2">
          <Lock className="w-3 h-3 text-muted-foreground" />
          <span className="text-[10px] text-muted-foreground font-medium">AI Agent — private canvas chat</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span
            className={`w-1.5 h-1.5 rounded-full ${loading ? 'bg-ai-border animate-hf-pulse' : 'bg-muted-foreground'}`}
          />
          {loading && <span className="text-[10px] text-ai-border italic">thinking…</span>}
        </div>
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-3 space-y-3 min-h-0">
        {messages.map((msg, i) => (
          <div key={i} className={`flex gap-2 ${msg.role === 'assistant' ? 'pl-1 border-l-2 border-ai-border' : 'justify-end'}`}>
            {msg.role === 'assistant' && (
              <div className="w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-semibold shrink-0 mt-0.5 bg-ai-border/20 text-ai-border">
                ✦
              </div>
            )}
            <div className={msg.role === 'assistant' ? 'min-w-0' : 'max-w-[80%]'}>
              {msg.role === 'assistant' && (
                <div className="flex items-baseline gap-2 mb-0.5">
                  <span className="text-xs font-semibold text-foreground">
                    AI Agent
                    <span className="ml-1 text-[9px] px-1 py-0.5 rounded bg-ai-border/20 text-ai-border font-medium">AI</span>
                  </span>
                </div>
              )}
              <p
                className={`text-xs leading-relaxed whitespace-pre-wrap break-words ${
                  msg.role === 'assistant' ? 'text-secondary-foreground' : 'text-foreground bg-secondary rounded-lg px-3 py-2'
                }`}
              >
                {msg.content}
              </p>
            </div>
          </div>
        ))}
      </div>

      {/* Input bar */}
      <div className="px-4 pb-3 pt-2 shrink-0">
        <div className="flex items-center gap-2 bg-secondary rounded-lg px-3 py-2">
          <button className="text-muted-foreground hover:text-foreground transition-colors">
            <Paperclip className="w-4 h-4" />
          </button>
          <input
            className="flex-1 bg-transparent text-xs text-foreground placeholder:text-muted-foreground outline-none"
            placeholder="Ask the AI agent… (Enter to send)"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={loading}
          />
          <button
            onClick={handleSend}
            disabled={loading || !input.trim()}
            className="text-primary hover:text-primary/80 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Send className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  )
}

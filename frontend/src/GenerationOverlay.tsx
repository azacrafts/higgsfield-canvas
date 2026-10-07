import { useEditor, createShapeId, TLShapeId, toRichText, AssetRecordType, useValue } from 'tldraw'
import { useGenerationContext, PendingGeneration, ThinkingGeneration } from './GenerationContext'
import { startGeneration, proxyUrl } from './api'

export default function GenerationOverlay() {
  const editor = useEditor()
  const {
    pendingGenerations,
    thinkingGenerations,
    settings,
    onGenerationComplete,
    onThinkingStart,
    onThinkingEnd,
    onApprove,
    onDismiss,
  } = useGenerationContext()

  const thinkingPos = useValue(
    'thinking-positions',
    () =>
      thinkingGenerations.map((gen) => {
        const zoom = editor.getZoomLevel()
        const vp = editor.pageToViewport({ x: gen.x + gen.w / 2, y: gen.y })
        return { ...gen, vpX: vp.x, vpY: vp.y, zoom }
      }),
    [thinkingGenerations, editor]
  )

  const pendingPos = useValue(
    'pending-positions',
    () =>
      pendingGenerations
        .map((gen) => {
          const shape = editor.getShape(gen.shapeId as TLShapeId)
          if (!shape) return null
          const zoom = editor.getZoomLevel()
          const vp = editor.pageToViewport({ x: shape.x, y: shape.y })
          return { ...gen, vpX: vp.x, vpY: vp.y, scaledW: gen.w * zoom }
        })
        .filter((p): p is NonNullable<typeof p> => p !== null),
    [pendingGenerations, editor]
  )

  function handleAnimate(gen: PendingGeneration) {
    onApprove(gen.shapeId, 'image')

    const vidX = gen.x + gen.w + 24
    const vidY = gen.y
    const thinkingId = `thinking-vid-${Date.now()}`

    const placeholderId = createShapeId()
    editor.createShapes([{
      id: placeholderId,
      type: 'geo',
      x: vidX, y: vidY,
      props: {
        geo: 'rectangle' as const,
        w: gen.w, h: gen.h,
        richText: toRichText(`Generating video…\n"${gen.prompt.slice(0, 80)}"`),
        color: 'violet' as any,
        fill: 'semi' as const,
        dash: 'dashed' as const,
      },
    }])

    onThinkingStart({ id: thinkingId, x: vidX, y: vidY, w: gen.w, h: gen.h, prompt: gen.prompt, type: 'video' })

    startGeneration(
      {
        type: 'video',
        prompt: `Smooth cinematic camera movement. ${gen.prompt}`,
        x: vidX, y: vidY,
        image_url: gen.mediaUrl,
        model: settings.videoModel,
        duration: settings.videoDuration,
      },
      (status) => {
        if (status.status !== 'completed' && status.status !== 'failed') return
        onThinkingEnd(thinkingId)
        if (status.status === 'failed') {
          editor.updateShapes([{
            id: placeholderId, type: 'geo',
            props: { richText: toRichText(`❌ Video failed\n${status.error ?? ''}`), color: 'red' as any },
          }])
          return
        }
        editor.deleteShapes([placeholderId])
        if (status.url) {
          const videoId = createShapeId()
          const assetId = AssetRecordType.createId()
          editor.createAssets([{
            type: 'video', id: assetId, typeName: 'asset',
            props: {
              w: gen.w, h: gen.h, name: gen.prompt.slice(0, 40),
              isAnimated: true, mimeType: 'video/mp4', src: proxyUrl(status.url!),
            },
            meta: { originalUrl: status.url },
          }])
          editor.createShapes([{
            id: videoId, type: 'video', x: vidX, y: vidY, opacity: 0.4,
            props: { w: gen.w, h: gen.h, assetId, playing: true, url: '' },
          }])
          onGenerationComplete({
            shapeId: videoId as unknown as string,
            assetId: assetId as unknown as string,
            x: vidX, y: vidY, w: gen.w, h: gen.h,
            prompt: gen.prompt, mediaUrl: status.url, type: 'video',
          })
        }
      }
    )
  }

  if (thinkingPos.length === 0 && pendingPos.length === 0) return null

  return (
    <div className="absolute inset-0 pointer-events-none z-[300]">

      {/* Thinking panels */}
      {thinkingPos.map((gen) => (
        <div
          key={gen.id}
          className="absolute pointer-events-auto flex items-center gap-2 bg-background/90 border border-ai-border/50 rounded-xl px-3.5 py-1.5 shadow-lg backdrop-blur-sm whitespace-nowrap"
          style={{ left: gen.vpX, top: gen.vpY - 48, transform: 'translateX(-50%)' }}
        >
          <span className="w-1.5 h-1.5 rounded-full bg-ai-border animate-hf-pulse shrink-0" />
          <span className="text-xs text-muted-foreground font-medium">
            {gen.type === 'image' ? '🖼' : '🎬'}&nbsp;
            <span className="text-foreground/80">Generating {gen.type}…</span>
          </span>
          <span className="text-[11px] text-muted-foreground italic max-w-[200px] overflow-hidden text-ellipsis">
            "{gen.prompt.slice(0, 48)}{gen.prompt.length > 48 ? '…' : ''}"
          </span>
        </div>
      ))}

      {/* Accept / Dismiss / Animate panels */}
      {pendingPos.map((pos) => (
        <div
          key={pos.shapeId}
          className="absolute pointer-events-auto flex items-center gap-2 bg-background/90 border border-ai-border/50 rounded-xl px-3.5 py-1.5 shadow-lg backdrop-blur-sm whitespace-nowrap"
          style={{ left: pos.vpX + pos.scaledW / 2, top: pos.vpY - 48, transform: 'translateX(-50%)' }}
        >
          <span className="text-xs text-muted-foreground font-medium">
            {pos.type === 'image' ? '🖼' : '🎬'}&nbsp;
            <span className="text-ai-border">{pos.type === 'image' ? 'Image' : 'Video'} ready</span>
          </span>
          <button
            onClick={() => onApprove(pos.shapeId, pos.type)}
            className="px-3 py-1 text-xs font-semibold rounded-md bg-online/20 text-online border border-online/40 hover:bg-online/30 transition-colors"
          >
            ✓ Accept
          </button>
          <button
            onClick={() => onDismiss(pos.shapeId)}
            className="px-3 py-1 text-xs font-semibold rounded-md bg-destructive/20 text-destructive border border-destructive/40 hover:bg-destructive/30 transition-colors"
          >
            ✕ Dismiss
          </button>
          {pos.type === 'image' && (
            <button
              onClick={() => handleAnimate(pos)}
              className="px-3 py-1 text-xs font-semibold rounded-md bg-ai-border/20 text-ai-border border border-ai-border/40 hover:bg-ai-border/30 transition-colors"
            >
              ▶ Animate
            </button>
          )}
        </div>
      ))}

    </div>
  )
}

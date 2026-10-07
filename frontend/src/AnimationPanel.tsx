import { useState } from 'react'
import { useEditor, useValue, createShapeId, AssetRecordType, toRichText } from 'tldraw'
import { useGenerationContext } from './GenerationContext'
import { startGeneration, uploadLocalImage, proxyUrl } from './api'

export default function AnimationPanel() {
  const editor = useEditor()
  const { pendingGenerations, onThinkingStart, onThinkingEnd, onGenerationComplete } = useGenerationContext()

  const [expanded, setExpanded] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [videoModel, setVideoModel] = useState('dop_standard')
  const [duration, setDuration] = useState(5)
  const [uploading, setUploading] = useState(false)

  const selected = useValue(
    'selected-image',
    () => {
      const shapes = editor.getSelectedShapes()
      if (shapes.length !== 1) return null
      const shape = shapes[0]
      if (shape.type !== 'image') return null
      const props = shape.props as unknown as Record<string, unknown>
      const assetId = props.assetId
      if (!assetId) return null
      const asset = editor.getAsset(assetId as any)
      if (!asset) return null
      const src = (asset.props as any).src as string
      const originalUrl = (asset.meta as any)?.originalUrl as string | undefined
      const bounds = editor.getShapePageBounds(shape)
      if (!bounds) return null
      const vp = editor.pageToViewport({ x: bounds.midX, y: bounds.maxY })
      return {
        shapeId: shape.id as string,
        src,
        originalUrl,
        x: shape.x,
        y: shape.y,
        w: (props.w as number) ?? 640,
        h: (props.h as number) ?? 360,
        vpX: vp.x,
        vpY: vp.y,
      }
    },
    [editor]
  )

  const isPending = selected ? pendingGenerations.some((g) => g.shapeId === selected.shapeId) : false

  if (!selected || isPending) return null

  const rawUrl = selected.originalUrl ?? selected.src
  const isLocal = rawUrl.startsWith('data:') || rawUrl.startsWith('blob:')

  async function handleAnimate() {
    if (!selected) return
    setExpanded(false)

    let imageUrl = rawUrl
    if (isLocal) {
      setUploading(true)
      try {
        let dataUrl = rawUrl
        if (rawUrl.startsWith('blob:')) {
          const blob = await fetch(rawUrl).then((r) => r.blob())
          dataUrl = await new Promise<string>((resolve) => {
            const reader = new FileReader()
            reader.onload = () => resolve(reader.result as string)
            reader.readAsDataURL(blob)
          })
        }
        imageUrl = await uploadLocalImage(dataUrl)
      } catch (e: any) {
        setUploading(false)
        alert(`Failed to upload image: ${e.message}`)
        return
      }
      setUploading(false)
    }

    const vidX = selected.x + selected.w + 24
    const vidY = selected.y
    const thinkingId = `thinking-anim-${Date.now()}`
    const label = prompt.trim() || 'cinematic animation'

    const placeholderId = createShapeId()
    editor.createShapes([{
      id: placeholderId,
      type: 'geo',
      x: vidX, y: vidY,
      props: {
        geo: 'rectangle' as const,
        w: selected.w, h: selected.h,
        richText: toRichText(`Generating video…\n"${label.slice(0, 80)}"`),
        color: 'violet' as any,
        fill: 'semi' as const,
        dash: 'dashed' as const,
      },
    }])

    onThinkingStart({ id: thinkingId, x: vidX, y: vidY, w: selected.w, h: selected.h, prompt: label, type: 'video' })

    startGeneration(
      {
        type: 'video',
        prompt: prompt.trim() || 'smooth cinematic camera movement',
        x: vidX, y: vidY,
        image_url: imageUrl,
        model: videoModel,
        duration,
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
              w: selected.w, h: selected.h, name: label.slice(0, 40),
              isAnimated: true, mimeType: 'video/mp4', src: proxyUrl(status.url!),
            },
            meta: { originalUrl: status.url },
          }])
          editor.createShapes([{
            id: videoId, type: 'video', x: vidX, y: vidY, opacity: 0.4,
            props: { w: selected.w, h: selected.h, assetId, playing: true, url: '' },
          }])
          onGenerationComplete({
            shapeId: videoId as unknown as string,
            assetId: assetId as unknown as string,
            x: vidX, y: vidY, w: selected.w, h: selected.h,
            prompt: label, mediaUrl: status.url, type: 'video',
          })
        }
      }
    )
  }

  return (
    <div
      className="absolute pointer-events-auto z-[400] flex flex-col items-center gap-0"
      style={{ left: selected.vpX, top: selected.vpY + 10, transform: 'translateX(-50%)' }}
    >
      {/* Trigger button */}
      <button
        onClick={() => !uploading && setExpanded((v) => !v)}
        title={uploading ? 'Uploading image…' : 'Animate image'}
        className={`w-8 h-8 rounded-lg border text-sm flex items-center justify-center shadow-md transition-all ${
          uploading
            ? 'cursor-wait bg-background/90 border-ai-border/50 text-ai-border'
            : expanded
            ? 'bg-ai-border text-background border-ai-border cursor-pointer'
            : 'bg-background/90 border-ai-border/50 text-ai-border hover:bg-ai-border/20 cursor-pointer'
        }`}
      >
        {uploading ? '⏳' : '🎬'}
      </button>

      {/* Expanded panel */}
      {expanded && (
        <div className="mt-1.5 bg-background/95 border border-ai-border/50 rounded-xl p-2.5 flex flex-col gap-1.5 shadow-xl backdrop-blur-sm min-w-[240px]">
          {isLocal && (
            <p className="text-[11px] text-muted-foreground">
              📤 Local image — will be uploaded first
            </p>
          )}
          <input
            autoFocus
            type="text"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleAnimate()}
            placeholder="Describe the motion… (optional)"
            className="bg-secondary text-foreground border border-border rounded-md px-2 py-1.5 text-xs outline-none focus:ring-1 focus:ring-ring w-full placeholder:text-muted-foreground"
          />
          <div className="flex gap-1.5">
            <select
              value={videoModel}
              onChange={(e) => setVideoModel(e.target.value)}
              className="flex-1 bg-secondary text-secondary-foreground border border-border rounded-md px-2 py-1.5 text-xs outline-none focus:ring-1 focus:ring-ring cursor-pointer"
            >
              <option value="dop_standard">DoP Standard</option>
              <option value="dop_turbo">DoP Turbo</option>
              <option value="kling">Kling 3.0</option>
            </select>
            <select
              value={duration}
              onChange={(e) => setDuration(Number(e.target.value))}
              className="bg-secondary text-secondary-foreground border border-border rounded-md px-2 py-1.5 text-xs outline-none focus:ring-1 focus:ring-ring cursor-pointer"
            >
              <option value={3}>3s</option>
              <option value={5}>5s</option>
              <option value={7}>7s</option>
              <option value={10}>10s</option>
            </select>
            <button
              onClick={handleAnimate}
              className="px-3 py-1.5 text-xs font-semibold rounded-md bg-ai-border text-background hover:bg-ai-border/80 transition-colors whitespace-nowrap"
            >
              ▶ Go
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

import { Editor } from 'tldraw'
import { CanvasShape, CanvasSnapshot } from './api'

function extractShapeText(props: Record<string, unknown>): string {
  const rawText = props.text ?? props.richText
  if (typeof rawText === 'string') {
    return rawText
  }
  if (rawText && typeof rawText === 'object' && 'content' in rawText) {
    const doc = rawText as { content?: Array<{ content?: Array<{ text?: string }> }> }
    return (doc.content ?? [])
      .map((p) => (p.content ?? []).map((leaf) => leaf.text ?? '').join(''))
      .join('\n')
  }
  return ''
}

function getShapeUrl(
  editor: Editor,
  shape: { type: string; props: Record<string, unknown> },
): string | undefined {
  if ((shape.type !== 'image' && shape.type !== 'video') || !shape.props.assetId) {
    return undefined
  }

  const asset = editor.getAsset(shape.props.assetId as any)
  if (!asset?.props || !('src' in asset.props)) {
    return undefined
  }

  const originalUrl = (asset.meta as { originalUrl?: string } | undefined)?.originalUrl
  const src = originalUrl ?? (asset.props.src as string)
  const proxyMatch = src.match(/\/api\/proxy-(?:image|media)\?url=(.+)/)
  return proxyMatch ? decodeURIComponent(proxyMatch[1]) : src
}

function getArrowBindings(
  editor: Editor,
  shape: { type: string },
): Pick<CanvasShape, 'fromId' | 'toId'> {
  if (shape.type !== 'arrow') {
    return {}
  }

  const bindings = editor.getBindingsFromShape(shape as any, 'arrow') as Array<{
    toId: string
    props?: { terminal?: string }
  }>

  let fromId: string | undefined
  let toId: string | undefined

  for (const binding of bindings) {
    if (binding.props?.terminal === 'start') {
      fromId = binding.toId
    } else if (binding.props?.terminal === 'end') {
      toId = binding.toId
    }
  }

  return { fromId, toId }
}

export function getCanvasState(editor: Editor): CanvasShape[] {
  return editor.getCurrentPageShapes().map((shape) => {
    const props = shape.props as Record<string, unknown>
    const text = extractShapeText(props)
    const url = getShapeUrl(editor, { type: shape.type, props })
    const { fromId, toId } = getArrowBindings(editor, shape)

    return {
      id: shape.id,
      type: shape.type,
      x: Math.round(shape.x),
      y: Math.round(shape.y),
      text,
      color: (props.color as string) ?? '',
      w: props.w as number | undefined,
      h: props.h as number | undefined,
      geo: props.geo as string | undefined,
      url,
      fromId,
      toId,
    }
  })
}

export function getCanvasSnapshot(editor: Editor): CanvasSnapshot {
  const viewport = editor.getViewportPageBounds()
  return {
    shapes: getCanvasState(editor),
    viewport: {
      x: viewport.x,
      y: viewport.y,
      w: viewport.w,
      h: viewport.h,
    },
    selected_ids: editor.getSelectedShapeIds().map(String),
  }
}

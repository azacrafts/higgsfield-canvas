export type CanvasShape = {
  id: string
  type: string
  x: number
  y: number
  text: string
  color: string
  w?: number
  h?: number
  geo?: string
  url?: string
  fromId?: string
  toId?: string
}

export type CanvasSnapshot = {
  shapes: CanvasShape[]
  viewport?: {
    x: number
    y: number
    w: number
    h: number
  }
  selected_ids?: string[]
}

// Raw action from the stream (uses _type discriminator)
export type StreamAction = Record<string, unknown> & { _type: string }

const API_BASE = (import.meta.env.VITE_API_URL ?? '').replace(/\/$/, '')

/** Proxy a Higgsfield CDN URL through the local backend to avoid CORS. */
export function proxyUrl(url: string): string {
  return `${API_BASE}/api/proxy-media?url=${encodeURIComponent(url)}`
}

/** Proxy an external image URL through the backend to avoid CORS. */
export function proxyImageUrl(url: string): string {
  return `${API_BASE}/api/proxy-image?url=${encodeURIComponent(url)}`
}

export type GenerationStatus = {
  status: string
  url?: string | null
  error?: string
}

export type MoodboardVerification = {
  should_trigger: boolean
  reason?: string
  query?: string
  trigger_message?: string
}

export async function streamMessage(
  message: string,
  canvasSnapshot: CanvasSnapshot,
  onAction: (action: StreamAction) => void,
  onDone: () => void,
  onError: (err: Error) => void,
  roomId = 'main',
  options?: {
    anchorShapeId?: string
  },
): Promise<void> {
  let response: Response
  console.info('[chat] sending request', {
    apiBase: API_BASE,
    roomId,
    shapes: canvasSnapshot.shapes.length,
    selected: canvasSnapshot.selected_ids?.length ?? 0,
    hasViewport: Boolean(canvasSnapshot.viewport),
  })
  try {
    response = await fetch(`${API_BASE}/api/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message,
        room_id: roomId,
        canvas_state: canvasSnapshot.shapes,
        canvas_snapshot: canvasSnapshot,
        anchor_shape_id: options?.anchorShapeId,
      }),
    })
  } catch (e) {
    console.error('[chat] request failed before reaching backend', e)
    onError(e instanceof Error ? e : new Error('Network error'))
    return
  }

  if (!response.ok) {
    console.error('[chat] backend responded with error', response.status)
    onError(new Error(`Backend error: ${response.status}`))
    return
  }

  const reader = response.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const raw = line.slice(6).trim()
        if (!raw) continue

        let event: { type: string; action?: StreamAction; error?: string }
        try {
          event = JSON.parse(raw)
        } catch {
          continue
        }

        if (event.type === 'action' && event.action) {
          onAction(event.action)
        } else if (event.type === 'done') {
          onDone()
          return
        } else if (event.error) {
          onError(new Error(event.error))
          return
        }
      }
    }
  } catch (e) {
    onError(e instanceof Error ? e : new Error('Stream read error'))
  } finally {
    reader.releaseLock()
  }

  onDone()
}

export async function verifyMoodboardTrigger(
  roomId: string,
  shapeId: string,
  text: string,
): Promise<MoodboardVerification> {
  const response = await fetch(`${API_BASE}/api/moodboard/verify`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      room_id: roomId,
      shape_id: shapeId,
      text,
    }),
  })

  if (!response.ok) {
    throw new Error(`Moodboard verify failed: ${response.status}`)
  }

  return response.json() as Promise<MoodboardVerification>
}

/** Upload a local data/blob URL image to a public host, returns a public URL. */
export async function uploadLocalImage(dataUrl: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/upload-image`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ data_url: dataUrl }),
  })
  const data = await res.json()
  if (data.error) throw new Error(data.error)
  return data.url as string
}

export async function startGeneration(
  params: {
    type: 'image' | 'video'
    prompt: string
    x: number
    y: number
    image_url?: string
    model?: string
    resolution?: string
    aspect_ratio?: string
    duration?: number
  },
  onStatusUpdate: (status: GenerationStatus) => void
): Promise<void> {
  let res: Response
  try {
    res = await fetch(`${API_BASE}/api/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    })
  } catch {
    onStatusUpdate({ status: 'failed', error: 'Network error' })
    return
  }

  const data = await res.json()
  if (data.error || !data.request_id) {
    onStatusUpdate({ status: 'failed', error: data.error ?? 'Unknown error' })
    return
  }

  const requestId = data.request_id as string

  // Subscribe to status SSE
  let sseRes: Response
  try {
    sseRes = await fetch(`${API_BASE}/api/generation-status/${requestId}`)
  } catch {
    onStatusUpdate({ status: 'failed', error: 'Status stream error' })
    return
  }

  const reader = sseRes.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const raw = line.slice(6).trim()
        if (!raw) continue

        try {
          const status = JSON.parse(raw) as GenerationStatus
          onStatusUpdate(status)
          if (status.status === 'completed' || status.status === 'failed') return
        } catch {
          continue
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

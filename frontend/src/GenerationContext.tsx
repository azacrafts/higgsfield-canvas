import { createContext, useContext } from 'react'

export type PendingGeneration = {
  shapeId: string
  assetId: string
  x: number
  y: number
  w: number
  h: number
  prompt: string
  mediaUrl: string
  type: 'image' | 'video'
}

export type ThinkingGeneration = {
  id: string
  x: number
  y: number
  w: number
  h: number
  prompt: string
  type: 'image' | 'video'
}

export type GenerationSettings = {
  imageModel: 'seedream' | 'flux'
  videoModel: 'dop_standard' | 'dop_turbo' | 'kling'
  imageResolution: string   // '1K' | '2K' | '4K'
  imageAspectRatio: string  // '16:9' | '4:3' | '1:1' | '9:16'
  videoDuration: number     // 3 | 5
}

export const DEFAULT_SETTINGS: GenerationSettings = {
  imageModel: 'seedream',
  videoModel: 'dop_standard',
  imageResolution: '2K',
  imageAspectRatio: '16:9',
  videoDuration: 3,
}

type GenerationContextType = {
  pendingGenerations: PendingGeneration[]
  thinkingGenerations: ThinkingGeneration[]
  settings: GenerationSettings
  setSettings: (patch: Partial<GenerationSettings>) => void
  onGenerationComplete: (gen: PendingGeneration) => void
  onThinkingStart: (gen: ThinkingGeneration) => void
  onThinkingEnd: (id: string) => void
  onApprove: (shapeId: string, type: 'image' | 'video') => void
  onDismiss: (shapeId: string) => void
}

export const GenerationContext = createContext<GenerationContextType | null>(null)

export function useGenerationContext() {
  const ctx = useContext(GenerationContext)
  if (!ctx) throw new Error('useGenerationContext used outside GenerationContext.Provider')
  return ctx
}

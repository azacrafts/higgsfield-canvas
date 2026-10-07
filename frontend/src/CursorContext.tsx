import { createContext, useContext } from 'react'
import type { CursorEntry } from './useVoiceChat'

export const CursorContext = createContext<Record<string, CursorEntry>>({})
export const useCursors = () => useContext(CursorContext)

/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL?: string
  readonly VITE_TLDRAW_LICENSE_KEY?: string
  readonly VITE_METERED_DOMAIN?: string
  readonly VITE_METERED_API_KEY?: string
  readonly VITE_DAILY_ROOM_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

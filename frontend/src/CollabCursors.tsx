import { useEditor } from 'tldraw'
import { useCursors } from './CursorContext'

const COLORS = ['#e84393', '#7c3aed', '#2563eb', '#059669', '#d97706', '#dc2626']
const AI_CURSOR_COLOR = '#9333ea'

function userColor(username: string): string {
  let hash = 0
  for (const c of username) hash = (hash * 31 + c.charCodeAt(0)) & 0xffffffff
  return COLORS[Math.abs(hash) % COLORS.length]
}

export default function CollabCursors() {
  const editor = useEditor()
  const cursors = useCursors()

  return (
    <>
      {Object.entries(cursors).map(([username, { x, y }]) => {
        const vp = editor.pageToViewport({ x, y })
        const isAI = username === 'Higgs AI'
        const color = isAI ? AI_CURSOR_COLOR : userColor(username)
        return (
          <div
            key={username}
            style={{
              position: 'absolute',
              left: vp.x,
              top: vp.y,
              pointerEvents: 'none',
              zIndex: 500,
              transform: 'translate(0, 0)',
            }}
          >
            {isAI ? (
              /* Diamond cursor for AI */
              <svg width="22" height="22" viewBox="0 0 22 22" style={{ display: 'block' }}>
                <polygon
                  points="11,2 20,11 11,20 2,11"
                  fill={color}
                  stroke="white"
                  strokeWidth="1.5"
                  strokeLinejoin="round"
                />
                <text x="11" y="15" textAnchor="middle" fontSize="8" fill="white" fontWeight="bold">✦</text>
              </svg>
            ) : (
              <svg width="20" height="20" viewBox="0 0 20 20" style={{ display: 'block' }}>
                <path
                  d="M4 1l13 7.5-7 1.5-3 6z"
                  fill={color}
                  stroke="white"
                  strokeWidth="1.2"
                  strokeLinejoin="round"
                />
              </svg>
            )}
            <div
              style={{
                background: color,
                color: '#fff',
                fontSize: 11,
                fontFamily: 'system-ui, sans-serif',
                fontWeight: 600,
                padding: '2px 6px',
                borderRadius: 4,
                whiteSpace: 'nowrap',
                marginTop: 2,
                boxShadow: '0 1px 4px rgba(0,0,0,0.3)',
                ...(isAI ? { animation: 'pulse 2s cubic-bezier(0.4,0,0.6,1) infinite' } : {}),
              }}
            >
              {isAI ? '✦ Higgs AI' : username}
            </div>
          </div>
        )
      })}
    </>
  )
}

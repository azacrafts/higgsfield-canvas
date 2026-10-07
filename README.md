# Higgsfield Canvas

Higgsfield Canvas is a collaborative AI whiteboard for visual planning sessions. It combines a shared `tldraw` canvas, streaming agent actions, voice-triggered commands, Pinterest moodboards, and Higgsfield image or video generation in one workspace.

The important design choice in this repo is that the AI does not just answer in chat. It acts on the board itself by creating, moving, updating, connecting, and restoring canvas objects in context.

## Highlights

- Real-time shared canvas with room-scoped state, cursor sync, and full snapshot restore
- AI chat that streams ordered canvas actions instead of plain text-only replies
- Context-aware board editing that uses the current canvas snapshot, selection, and layout
- Voice pipeline with transcription, wake-word detection, command accumulation, and agent execution
- Pinterest moodboards that can be triggered from chat or from a sticky note on the board
- Image and video generation workflows backed by the Higgsfield platform
- SQLite persistence for room state and transcript history
- Human-in-the-loop approval flow for generated media

## How It Works

```text
React + tldraw frontend
  |- Chat sidebar streams requests to FastAPI over SSE
  |- Voice and collaboration state flows over WebSocket
  |- Media generation uses REST + SSE status updates
  `- Canvas changes are mirrored as full snapshots for restore

FastAPI backend
  |- Anthropic-powered canvas agents and classifier
  |- LangGraph-based context agent for board-aware edits
  |- Groq Whisper transcription for voice input
  |- Pinterest scraping for moodboard references
  |- Higgsfield API for image and video generation
  `- SQLite for room snapshots and transcripts
```

Two backend paths matter most:

- `POST /api/chat/stream` is the main entrypoint for AI-driven board edits.
- `WS /ws/{room_id}/{username}` handles collaboration, cursor sync, transcripts, canvas restore, and voice-triggered actions.

If the context-aware agent fails, the backend falls back to a legacy prompt-based streaming agent rather than failing the whole request.

## Stack

### Frontend

- React 18
- TypeScript
- Vite
- `tldraw`
- `react-resizable-panels`
- LiveKit client

### Backend

- FastAPI
- LangGraph
- Anthropic SDK
- Groq SDK
- SQLite with `aiosqlite`
- `pinscrape`
- Pillow
- Uvicorn

## Repository Layout

```text
higgsfield/
|- backend/
|  |- agent/          # canvas agent prompts, tools, graph
|  |- context/        # context-aware agent, diffing, retrieval, storage
|  |- main.py         # FastAPI app, WebSocket entrypoint, generation routes
|  |- ws_handler.py   # collaboration and voice message dispatch
|  |- voice_pipeline.py
|  |- db.py           # SQLite persistence
|  `- requirements.txt
|- frontend/
|  |- src/
|  |  |- App.tsx
|  |  |- AgentSidebar.tsx
|  |  |- CanvasPane.tsx
|  |  |- VoiceChat.tsx
|  |  `- useVoiceChat.ts
|  |- package.json
|  `- vite.config.ts
|- KEY_FEATURES.md
|- VIDEO_DEMO_SCRIPT.md
`- requirements-lock.txt
```

## Getting Started

### Prerequisites

- Python 3.10+
- Node.js 20+
- npm
- API keys for Anthropic, Groq, and Higgsfield

### 1. Start the backend

Use the repository-level lockfile for Python setup. It includes packages that the backend imports at runtime, including Pillow and `pinscrape`.

```powershell
cd backend
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r ..\requirements-lock.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

macOS or Linux:

```bash
cd backend
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r ../requirements-lock.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The backend listens on `http://localhost:8000`.

### 2. Start the frontend

```powershell
cd frontend
npm install
npm run dev
```

The Vite dev server runs on `http://localhost:5173`.

### 3. Open the app

Visit `http://localhost:5173` and open a second browser window or tab to test collaboration.

Important current behavior:

- The backend supports room-based collaboration.
- The current frontend is wired to the hard-coded room `main` in `frontend/src/App.tsx`.
- Usernames are generated automatically per browser session.

## Configuration

### Backend environment variables

Create `backend/.env` with the values your setup needs.

Example:

```env
ANTHROPIC_API_KEY=your_anthropic_key
GROQ_API_KEY=your_groq_key
HF_API_KEY=your_higgsfield_key
HF_API_SECRET=your_higgsfield_secret
PUBLIC_URL=http://localhost:8000
```

| Variable | Required | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Yes | Main chat agent and transcript classifier |
| `GROQ_API_KEY` | Yes | Voice transcription via Whisper |
| `HF_API_KEY` | Yes for media generation | Higgsfield platform authentication |
| `HF_API_SECRET` | Yes for media generation | Higgsfield platform authentication |
| `PUBLIC_URL` | No | Enables Higgsfield webhook callbacks to `/api/generation-webhook` |
| `LIVEKIT_URL` | No | LiveKit server URL for optional audio room support |
| `LIVEKIT_API_KEY` | No | LiveKit token issuance |
| `LIVEKIT_API_SECRET` | No | LiveKit token issuance |

The backend also exposes tuning knobs through `backend/config.py`, including:

- `canvas_agent_model`
- `chat_agent_model`
- `classifier_model`
- `command_flush_delay_s`
- `db_save_debounce_s`
- `agent_max_turns`
- `context_debug`

### Frontend environment variables

Put frontend env vars in a standard Vite env file in `frontend/`, such as `frontend/.env.local`.

Example:

```env
VITE_API_URL=http://localhost:8000
VITE_TLDRAW_LICENSE_KEY=your_tldraw_license_key
```

| Variable | Required | Purpose |
| --- | --- | --- |
| `VITE_API_URL` | No | Backend origin, defaults to `http://localhost:8000` |
| `VITE_TLDRAW_LICENSE_KEY` | No | `tldraw` license key if your setup requires one |
| `VITE_METERED_DOMAIN` | No | Metered TURN domain for better peer connectivity |
| `VITE_METERED_API_KEY` | No | Metered TURN API key |
| `VITE_DAILY_ROOM_URL` | No | Declared in types, not currently referenced in app code |

## Core Product Flows

### AI chat to board actions

1. The frontend sends the user prompt plus the current canvas snapshot to `POST /api/chat/stream`.
2. The backend runs the context-aware agent and streams actions back as SSE events.
3. The frontend applies those actions directly to the `tldraw` canvas.

### Sticky-note moodboard trigger

1. A user finishes editing a note that looks like a moodboard request.
2. The frontend asks the backend to verify intent with `POST /api/moodboard/verify`.
3. The backend fetches Pinterest references and places the moodboard near the triggering note when possible.

### Voice-triggered commands

1. The browser captures microphone audio and sends chunks over WebSocket.
2. The backend transcribes speech with Groq Whisper.
3. Wake-word and classifier logic decide whether the transcript is a board command.
4. Confirmed commands are passed into the canvas agent and broadcast back as board actions.

### Image and video generation

1. The frontend submits a generation request to `POST /api/generate`.
2. The backend submits the job to Higgsfield and starts polling for status.
3. The frontend subscribes to `GET /api/generation-status/{request_id}` via SSE.
4. Generated assets appear on the board with an approval or dismissal flow.

## API Surface

| Route | Method | Purpose |
| --- | --- | --- |
| `/api/chat/stream` | `POST` | Stream canvas actions for chat requests |
| `/api/agent/stream` | `POST` | Direct context-agent streaming endpoint |
| `/api/moodboard/verify` | `POST` | Verify sticky-note moodboard intent |
| `/api/generate` | `POST` | Start image or video generation |
| `/api/generation-status/{request_id}` | `GET` | Stream generation job status |
| `/api/generation-webhook` | `POST` | Optional Higgsfield completion webhook |
| `/api/upload-image` | `POST` | Upload local data URLs to a public host |
| `/api/proxy-image` | `GET` | Proxy Pinterest images to avoid CORS issues |
| `/api/proxy-media` | `GET` | Proxy generated Higgsfield media |
| `/api/livekit-token` | `GET` | Issue a LiveKit token when credentials are configured |
| `/health` | `GET` | Health check |
| `/ws/{room_id}/{username}` | `WebSocket` | Collaboration, cursor sync, voice, and canvas restore |

## Persistence

Room state is stored in `backend/higgsfield.db`.

The database currently persists:

- simplified canvas state for agent context
- full canvas snapshots for accurate restore on rejoin
- voice transcripts by room and username

## Running Tests

The repo includes lightweight backend tests around context diffing, moodboard placement, and chat routing.

```powershell
pytest backend/test_context_agent.py
```

There is no frontend test suite or CI pipeline configured in this repository yet.

## Production Build

Build the frontend first:

```powershell
cd frontend
npm run build
```

Then run the backend normally:

```powershell
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

If `frontend/dist` exists, `backend/main.py` serves the built SPA and its `/assets` bundle.

## Notes and Caveats

- `PUBLIC_URL` is optional. Without it, media generation still works because the backend falls back to polling.
- Pinterest fetching depends on external availability and can time out.
- Voice features require microphone permission and a browser with MediaRecorder support.
- The optional LiveKit token route requires valid LiveKit credentials and the Python dependency that provides `livekit.api`.
- The current UI does not expose a room picker even though the backend supports arbitrary room IDs.

## Supporting Docs

- [Key features](./KEY_FEATURES.md)
- [Demo script](./VIDEO_DEMO_SCRIPT.md)

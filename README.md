# Higgsfield Canvas

A full-stack collaborative AI whiteboard for visual planning, brainstorming, and team workflows.

The project combines a shared visual canvas with AI-assisted interactions, allowing users to organize ideas, collaborate, and work visually in a single workspace.

## Features

- Collaborative visual whiteboard
- Shared canvas for planning and brainstorming
- AI-assisted interactions
- Full-stack architecture
- Frontend and backend separation
- Team-oriented workflow
- Interactive visual workspace

## Tech Stack

### Frontend

- TypeScript
- React
- tldraw

### Backend

- Python
- FastAPI

### Deployment

- Render

## Project Structure

```text
higgsfield-canvas/
├── backend/          # Backend API and server logic
├── frontend/         # Frontend application
├── requirements.txt # Python dependencies
├── render.yaml       # Deployment configuration
└── README.md
```

## Architecture

```text
User
  ↓
Frontend
  ↓
Collaborative Canvas
  ↓
Backend API
  ↓
AI / Application Logic
```

## Running Locally

Clone the repository:

```bash
git clone https://github.com/azacrafts/higgsfield-canvas.git
cd higgsfield-canvas
```

Install backend dependencies:

```bash
pip install -r requirements.txt
```

Then follow the frontend setup instructions inside the `frontend` directory.

## Purpose

The project was developed as a collaborative AI-powered visual workspace, combining whiteboard-style interaction with intelligent assistance for planning and teamwork.

## Future Improvements

- Improve real-time collaboration
- Expand AI-assisted workflows
- Add more canvas tools
- Improve user and session management
- Add persistent project storage
- Improve deployment and scalability

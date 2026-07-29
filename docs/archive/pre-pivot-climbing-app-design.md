# Design Notes

## Milestone 1 Persistence Architecture

The backend route catalog uses SQLite with SQLModel tables:

- `Gym` stores gym metadata, GPS coordinates, and the grading system used at that gym.
- `WallSection` groups routes by physical wall area inside a gym.
- `Route` stores a crowdsourced V-scale grade (`grade_display` + ordinal `v_grade`), beginner-friendly tag (VB–V1 unless movement tags veto), hold detections, contributor, image filename, and optional self-reported movement attributes.
- `ClimbLog` records user attempts/sends against routes.

## Backend Layering

The backend is organized into three layers:

- `backend/api/` — FastAPI routers (one per resource: detection, gyms, routes, climb logs). Routers handle HTTP concerns only.
- `backend/services/` — business logic: grade normalization and beginner tagging (`grading.py`), route creation/serialization (`route_service.py`), and Gemini-based duplicate matching (`gemini_matcher.py`).
- `backend/db/` — persistence: SQLModel tables (`models.py`), engine/session setup (`database.py`), repository classes that own all queries (`repositories.py`), and startup seeding (`seed.py`).

FastAPI handlers never construct queries directly; they go through repositories. Shared filesystem paths live in `backend/config.py`.

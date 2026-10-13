# Employee Attendance & Analytics API

A FastAPI and MongoDB service for punch-in and punch-out, HR corrections with an audit trail, and attendance analytics. All the code is in `app/main.py`.

## Setup

You need Python 3.11+ and MongoDB 6.0 or newer.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
docker run -d -p 27017:27017 mongo:7
```

Copy `.env.example` to `.env` and set `MONGO_URI` and `MONGO_DB`. Real environment variables win over `.env`. Then start the server:

```bash
uvicorn app.main:app --port 8000
```

Indexes are created when the app starts. `GET /health` returns 200 once MongoDB answers. The API docs are at `http://localhost:8000/docs`.

## Testing

I built and tested this against MongoDB 7.0.43 running in Docker. `tests/smoke_test.py` runs against a live server and passes all its checks. It checks all four analytics endpoints against plain-Python expected values, parallel requests (create, punch-in, punch-out, corrections), and the explain plans on 100,000 attendance records (`IXSCAN`, no `COLLSCAN`).

```bash
python tests/smoke_test.py --big
```

Careful: it deletes both collections in `MONGO_DB`, so use a scratch database.

## Notes

- The API uses epoch milliseconds. MongoDB stores UTC datetimes. Dates, months and shift times are strings in IST.
- Analytics read the stored derived values. They never recompute from punch times.
- Duplicate employees and duplicate punch-ins are stopped by unique indexes. Punch-out and corrections use a conditional update, so only one of two simultaneous requests wins. A correction that loses a race is retried on the fresh record.
- A correction that changes none of `status`, `punch_in` or `punch_out` returns 422.
- The decisions are in `DECISIONS.md` and the starter-code defects are in `REVIEW.md`.

## Status

All endpoints in `openapi.yaml` are implemented. Nothing is missing.
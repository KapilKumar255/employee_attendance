# Employee Attendance API

FastAPI service for employee records, attendance punch-in/punch-out, regularization,
and attendance analytics backed by MongoDB.

## Requirements

- Python 3.10+
- MongoDB 6.0+

## Run locally on Windows

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a `.env` file with:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance
```

Start the API:

```powershell
uvicorn app.main:app --port 8000 --reload
```

Readiness check:

```text
http://localhost:8000/health
```

Interactive API documentation is available at:

```text
http://localhost:8000/docs
```

The sample MongoDB data can be loaded with:

```powershell
python sample_seed.py
```

## Notes

- The `GET /admin/explain/{endpoint}` API is currently not implemented in
  `app/main.py`; 

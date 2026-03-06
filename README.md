# Job Application Tracker

Automatically parses job-related emails using LLaMA via Groq and syncs 
extracted data (company, role, status) to a Google Sheet nightly.

## Stack
- FastAPI, APScheduler
- Gmail API, Google Sheets API
- Groq (LLaMA 3.1) for LLM extraction
- Pydantic for validation

## Setup
1. Add credentials (see `.env.example`)
2. `pip install -r requirements.txt`
3. `uvicorn main:app --reload`

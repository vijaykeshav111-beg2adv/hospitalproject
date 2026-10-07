# Vijay Vargiya Group of Hospitals - merged project

This project is the hospital platform built on the original `workshop_ml` project.

## Original ML Workshop features retained

- `src/` ML pipeline: ingestion, transformation, training and prediction
- `artifacts/model.pkl` and `artifacts/preprocessor.pkl`
- pretrained NLP: POS tagging, NER, sentiment analysis and word embeddings
- `/predict` and `/pretrained-nlp` routes
- original MCP medical prototype, now backed by hospital MySQL
- original ecommerce MCP prototype retained separately under `mcp_server_ecom/`

## Hospital features

- FastAPI hospital platform under `app/`
- MySQL-only database
- authentication and RBAC
- patients, doctors, specialties
- schedules, leaves, slots, appointments and queue
- consultations, medical records/files
- medicines and prescriptions
- invoices and payments
- notifications and WhatsApp integration
- AI conversations, memory, safety, monitoring and analytics
- Groq-only AI integration
- controlled hospital AI tools

## Running

1. Create MySQL database and user using `sql/01_setup_database.sql`.
2. Copy `.env.example` to `.env` and set `DATABASE_URL`/DB settings, `JWT_SECRET`, and `GROQ_API_KEY`.
3. Install `requirements.txt`.
4. Download the original NLP resources:

   `python nlp_pretrained/download_nltk_data.py`

5. Start the hospital application:

   `uvicorn app.main:app --reload`

Hospital UI: `/ui`

Original ML Workshop compatibility:
- `/ml`
- `/predict`
- `/pretrained-nlp`

The hospital database is the only application database. The original workshop's prototype SQLite medical database was replaced by MySQL-backed compatibility functions.

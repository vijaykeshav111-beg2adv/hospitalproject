# Project Evolution

## Original baseline

The initial project was a FastAPI workshop/ML application. Its original `main.py` exposed a COVID prediction form, a health endpoint, and a pretrained NLP page. The original structure used `src/components`, `src/pipeline`, model training/prediction utilities, Jinja templates, and an NLP package.

The original roadmap then proposed evolving the application into a hospital assistant with login/logout, a Groq/Gemini chatbot, doctor availability, appointment slots, database-backed booking, injury-image routing, and patient history.

The exact original source files supplied with the project are preserved under `docs/original_baseline/` for traceability. They are documentation/history only and are not imported into the production application.

## Current target

The active application is the Vijay Vargiya Group of Hospitals platform. It uses FastAPI + MySQL, JWT/RBAC, hospital domain services, controlled AI tools, Groq as the only LLM provider, appointment/slot management, clinical records, prescriptions, billing/payments, notifications/WhatsApp, analytics, audit logging, and background jobs.

## Cleanup rule

Legacy COVID prediction and standalone workshop NLP code is not merged into the active `app/` package. The historical baseline is retained only to document the project's origin. SQLite is not a supported runtime backend. Groq is the only LLM provider.

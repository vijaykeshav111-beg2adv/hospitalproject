"""Compatibility routes preserving the original ML Workshop features.

These routes intentionally remain separate from the hospital business APIs.
The original workshop model, preprocessing artifacts and pretrained NLP modules
are reused unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from src.exception import CustomException
from src.logger import get_logger
from src.pipeline.predict_pipeline import CustomData, PredictPipeline
from nlp_pretrained.ner_tagger import get_pos_tags, extract_entities
from nlp_pretrained.embedding import most_similar_words
from nlp_pretrained.sentiment_analyzer import analyze_sentiment

log = get_logger(__name__)
router = APIRouter(tags=["Original ML Workshop"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "ml_templates"))


@router.get("/ml", response_class=HTMLResponse, include_in_schema=True)
async def ml_home(request: Request):
    """Original workshop landing page."""
    return templates.TemplateResponse(request, "index.html")


@router.get("/predict", response_class=HTMLResponse)
async def predict_form(request: Request):
    return templates.TemplateResponse(request, "predict.html", {"result": None})


@router.post("/predict", response_class=HTMLResponse)
async def predict_result(
    request: Request,
    age: int = Form(...),
    gender: str = Form(...),
    fever: float = Form(...),
    cough: str = Form(...),
    city: str = Form(...),
):
    try:
        data = CustomData(
            age=age, gender=gender, fever=fever, cough=cough, city=city
        ).get_data_as_dataframe()
        result, probability = PredictPipeline().predict(data)
        return templates.TemplateResponse(
            request,
            "predict.html",
            {
                "result": result,
                "probability": probability,
                "form_data": {
                    "age": age, "gender": gender, "fever": fever,
                    "cough": cough, "city": city,
                },
            },
        )
    except Exception as exc:
        raise CustomException(exc, sys) from exc


@router.get("/pretrained-nlp", response_class=HTMLResponse)
async def pretrained_nlp_form(request: Request):
    return templates.TemplateResponse(
        request,
        "pretrained_nlp.html",
        {"result": None, "form_data": {"query": ""}, "error": None},
    )


@router.post("/pretrained-nlp", response_class=HTMLResponse)
async def pretrained_nlp_analysis(request: Request, query: str = Form(...)):
    query = query.strip()
    if not query:
        return templates.TemplateResponse(
            request,
            "pretrained_nlp.html",
            {
                "result": None,
                "form_data": {"query": ""},
                "error": "Please enter some text...",
            },
        )

    try:
        pos_tags = get_pos_tags(query)
        entities = extract_entities(query)
        sentiment = analyze_sentiment(query)
        similar_words = []
        words = query.split()
        if words:
            try:
                similar_words = most_similar_words(words[0].lower(), topn=5)
            except Exception as exc:
                log.warning("Embedding lookup failed: %s", exc)

        return templates.TemplateResponse(
            request,
            "pretrained_nlp.html",
            {
                "result": {
                    "query": query,
                    "pos_tags": pos_tags,
                    "entities": entities,
                    "similar_words": similar_words,
                    "sentiment": sentiment,
                },
                "form_data": {"query": query},
                "error": None,
            },
        )
    except Exception as exc:
        log.exception("Pretrained NLP analysis failed")
        return templates.TemplateResponse(
            request,
            "pretrained_nlp.html",
            {"result": None, "form_data": {"query": query}, "error": str(exc)},
        )

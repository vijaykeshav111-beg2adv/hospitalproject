"""Optional NLP support signals for the AI front desk.

These come from the project's ORIGINAL ML Workshop (`nlp_pretrained/`) and are
used exactly the way the design intends - as *supporting signals*, never as
decision makers:

    VADER sentiment   -> distress / urgency metadata, shown to the staff portal
                         and added to escalations. It NEVER decides a specialty
                         and NEVER triggers an emergency on its own.
    GloVe embeddings  -> a last-resort similarity hint for a word the local
                         dictionary does not know, used only when Groq is not
                         available. Word-level similarity, not a medical
                         classifier.

Both are optional and imported defensively: if the packages or the NLTK/GloVe
data are missing, every function returns None and the front desk continues
exactly as before. Nothing here can crash a patient's chat.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("vvh.ai.nlp")

# ---------------------------------------------------------------------------
# switches (env only, so nothing has to change in code to turn them off)
# ---------------------------------------------------------------------------
def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def sentiment_enabled() -> bool:
    return _flag("NLP_SENTIMENT", True)


def embeddings_enabled() -> bool:
    """Off by default: GloVe downloads a ~66 MB model the first time it runs."""
    return _flag("NLP_EMBEDDINGS", False)


# ---------------------------------------------------------------------------
# VADER sentiment (supporting signal only)
# ---------------------------------------------------------------------------
_sentiment_fn = None
_sentiment_loaded = False


def _load_sentiment():
    """Import the project's own sentiment analyzer, once.

    Uses ``nlp_pretrained.sentiment_analyzer.analyze_sentiment`` so the ML
    Workshop module stays the single implementation.
    """
    global _sentiment_fn, _sentiment_loaded
    if _sentiment_loaded:
        return _sentiment_fn
    _sentiment_loaded = True
    if not sentiment_enabled():
        return None
    try:
        # The project's analyzer needs the VADER lexicon. Checking here keeps the
        # failure quiet instead of letting it raise inside the chat path.
        import nltk

        nltk.data.find("sentiment/vader_lexicon.zip")
    except Exception:  # noqa: BLE001 - data not downloaded yet
        log.info("NLP sentiment disabled: NLTK vader_lexicon not found "
                 "(run nlp_pretrained/download_nltk_data.py)")
        return None
    try:
        from nlp_pretrained.sentiment_analyzer import analyze_sentiment

        _sentiment_fn = analyze_sentiment
    except Exception:  # noqa: BLE001 - optional dependency / missing NLTK data
        log.info("NLP sentiment not available (install requirements-ml.txt and run "
                 "nlp_pretrained/download_nltk_data.py)")
        _sentiment_fn = None
    return _sentiment_fn


def sentiment_hint(text: str) -> dict | None:
    """Distress metadata for a patient message, or None when unavailable.

    Returns ``{"compound": -0.62, "urgency_hint": "High", "distress": True}``.
    Never used to route, diagnose or declare an emergency - it only tells the
    staff portal that the patient sounds distressed, and it can raise the
    escalation detail.
    """
    global _sentiment_fn
    fn = _load_sentiment()
    if fn is None or not (text or "").strip():
        return None
    try:
        scores = fn(text)
    except Exception:  # noqa: BLE001
        # Typically the NLTK 'vader_lexicon' data is missing. Disable the signal
        # for the rest of the process instead of logging a traceback per message.
        log.info("Sentiment signal disabled for this process (run "
                 "nlp_pretrained/download_nltk_data.py to enable it)")
        _sentiment_fn = None
        return None
    if not isinstance(scores, dict):
        return None
    compound = scores.get("compound")
    try:
        compound = float(compound)
    except (TypeError, ValueError):
        return None
    return {
        "compound": compound,
        "urgency_hint": scores.get("urgency_hint"),
        "distress": compound <= -0.4,
    }


# ---------------------------------------------------------------------------
# GloVe word embeddings (last-resort hint, off by default)
# ---------------------------------------------------------------------------
def embedding_suggest(tokens: list[str], candidates: set[str], *, threshold: float = 0.55,
                      max_tokens: int = 4) -> dict[str, str]:
    """Suggest a canonical term for unknown words via word-vector similarity.

    Only called when the local dictionary AND Groq both failed. Word-level
    similarity is a weak signal, so the threshold is high and anything accepted
    is merged into the local term list for the MySQL keyword matcher - the
    deterministic router still makes the final decision.

    Returns ``{"tavda": "fever"}`` or ``{}`` when embeddings are unavailable.
    """
    if not embeddings_enabled() or not tokens or not candidates:
        return {}
    try:
        from nlp_pretrained.embedding import word_similarity
    except Exception:  # noqa: BLE001 - gensim / model download not available
        log.info("NLP embeddings not available (pip install -r requirements-ml.txt)")
        return {}

    suggestions: dict[str, str] = {}
    for token in tokens[:max_tokens]:
        best_term, best_score = None, 0.0
        for term in candidates:
            if " " in term or len(term) < 4:
                continue
            try:
                score = word_similarity(token, term)
            except Exception:  # noqa: BLE001 - word not in the GloVe vocabulary
                continue
            if score > best_score:
                best_term, best_score = term, score
        if best_term and best_score >= threshold:
            suggestions[token] = best_term
            log.info("GloVe hint: %r ~ %r (%.2f)", token, best_term, best_score)
    return suggestions


def status() -> dict:
    """Diagnostics for the staff portal / tests."""
    return {
        "sentiment_enabled": sentiment_enabled(),
        "sentiment_available": _load_sentiment() is not None,
        "embeddings_enabled": embeddings_enabled(),
    }

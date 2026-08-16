import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException

from config import load_config
from lmstudio_client import ReviewError, review_with_lmstudio
from models import ReviewRequest, ReviewResponse


CONFIG = load_config()

app = FastAPI(
    title="StockReviewAI Server",
    version="1.0.0",
    root_path="/stockreview",
)


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("StockReviewAI")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    log_path = Path(__file__).with_name(CONFIG.server.log_file)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s\t%(levelname)s\t%(message)s"))
    logger.addHandler(handler)
    return logger


LOGGER = _build_logger()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/review", response_model=ReviewResponse)
async def review(request: ReviewRequest) -> ReviewResponse:
    try:
        return await review_with_lmstudio(
            request,
            CONFIG.lmstudio,
            LOGGER,
            CONFIG.geocoding,
        )
    except ReviewError as exc:
        LOGGER.error("review_failed filename=%s error=%s", request.filename, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc

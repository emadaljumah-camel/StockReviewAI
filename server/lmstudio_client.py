import base64
import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from models import LMStudioSettings, ReviewRequest, ReviewResponse
from prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, user_prompt


class ReviewError(RuntimeError):
    pass


def _validated_jpeg_base64(value: str) -> str:
    raw_value = value.strip()
    if raw_value.startswith("data:image/"):
        _, _, raw_value = raw_value.partition(",")

    try:
        image_bytes = base64.b64decode(raw_value, validate=True)
    except Exception as exc:
        raise ReviewError("Image payload was not valid base64") from exc

    if not image_bytes.startswith(b"\xff\xd8\xff"):
        raise ReviewError("Image payload was not a valid JPEG")

    return raw_value


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```json") and value.endswith("```"):
        return value[7:-3].strip()
    if value.startswith("```") and value.endswith("```"):
        return value[3:-3].strip()
    return value


def _choice_content(response: dict[str, Any]) -> str:
    try:
        choice = response["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content") or choice.get("text")
    except (KeyError, IndexError, TypeError) as exc:
        raise ReviewError("LM Studio response did not include choices[0].message.content") from exc

    if not isinstance(content, str) or not content.strip():
        raise ReviewError("LM Studio response content was empty")
    return content


def _build_payload(request: ReviewRequest, settings: LMStudioSettings) -> dict[str, Any]:
    image_base64 = _validated_jpeg_base64(request.image_base64)
    return {
        "model": settings.model,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "stock_review_result",
                "strict": True,
                "schema": RESPONSE_SCHEMA,
            },
        },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt(request.filename, request.threshold)},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                        },
                    },
                ],
            },
        ],
    }


async def review_with_lmstudio(
    request: ReviewRequest,
    settings: LMStudioSettings,
    logger: logging.Logger,
) -> ReviewResponse:
    payload = _build_payload(request, settings)
    last_error: Exception | None = None

    for attempt in range(settings.retries + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
                response = await client.post(settings.endpoint, json=payload)
                if response.status_code >= 400:
                    error_body = response.text.strip()[:1000]
                    raise ReviewError(
                        f"LM Studio HTTP {response.status_code}: {error_body or response.reason_phrase}"
                    )

            raw = response.json()
            content = _choice_content(raw)
            parsed = json.loads(_strip_json_fence(content))
            result = ReviewResponse.model_validate(parsed)

            logger.info(
                "review filename=%s verdict=%s score=%s reason=%s issues=%s",
                request.filename,
                result.verdict,
                result.score,
                result.reason,
                "; ".join(result.issues),
            )
            return result
        except (httpx.HTTPError, json.JSONDecodeError, ValidationError, ReviewError) as exc:
            last_error = exc
            logger.warning(
                "review_attempt_failed filename=%s attempt=%s error=%s",
                request.filename,
                attempt + 1,
                str(exc),
            )

    raise ReviewError(f"LM Studio review failed: {last_error}")

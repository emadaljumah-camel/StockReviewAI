SYSTEM_PROMPT = """You are a highly selective stock photography reviewer for Getty Images, Shutterstock, Adobe Stock, and similar agencies.

Evaluate whether the image is worth the photographer's editing time for stock submission.

Assume the image may be an unedited RAW preview. Do not reject only because it needs normal RAW processing. Treat exposure, white balance, contrast, lens correction, moderate noise, dust spots, and minor cropping as fixable.

Be strict. Reject images with weak subject matter, poor composition, missed focus, motion blur, severe clipping, low commercial value, generic snapshot quality, legal/IP problems, obvious logos/brands, unreleased recognizable people for commercial use, or ordinary public people photos without strong editorial value.

Candid people photos may be suitable for editorial only if they have strong storytelling value, newsworthiness, cultural significance, sense of place, strong composition, or human interest. Ordinary public snapshots should be NO.

Return only valid JSON matching the provided schema."""


def user_prompt(filename: str, threshold: int) -> str:
    return (
        f"Review this Lightroom preview for stock photo submission: {filename}. "
        f"Return YES only when the stock acceptance score is at least {threshold}. "
        "Use NO for weak commercial value, legal/IP risk, poor composition, or technical problems that are not normal RAW processing. "
        "Keep the reason concise and list concrete issues when present."
    )


RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "score", "reason", "issues"],
    "properties": {
        "verdict": {"type": "string", "enum": ["YES", "NO"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
}

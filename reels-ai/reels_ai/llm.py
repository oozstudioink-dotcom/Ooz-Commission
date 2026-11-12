"""One place that asks a language model a question and gets structured JSON back.

Two models are available: Gemini (the free tier of its API: costs nothing, but is sometimes busy)
and Claude (paid per use, reliable). Routine reading and planning jobs go to Gemini first and
fall back to Claude; jobs where the wording matters go straight to Claude. Every caller gets the
same thing back: the parsed answer, "by" (which model answered) and, for Claude, "tokens" so
the cost can be counted. Gemini's answers cost nothing and carry no tokens.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]


def ask_claude(system: str, schema: dict, text: str, model: str, max_tokens: int = 4000, effort: str = "low",
               client=None) -> dict:
    import anthropic
    client = client or anthropic.Anthropic()
    with client.beta.messages.stream(
        model=model, max_tokens=max_tokens, system=system,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": text}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"Claude gave no answer ({msg.stop_reason})")
    out = json.loads(next(b.text for b in msg.content if b.type == "text"))
    out["tokens"] = {"in": msg.usage.input_tokens, "out": msg.usage.output_tokens}
    out["by"] = "claude"
    return out


def fits(out, schema: dict) -> bool:
    """Does an answer have the shape asked for? (Claude's is guaranteed; Gemini's is checked.)"""
    kind = schema.get("type")
    if kind == "object":
        return (isinstance(out, dict) and all(k in out for k in schema.get("required", []))
                and all(fits(out[k], s) for k, s in schema.get("properties", {}).items() if k in out))
    if kind == "array":
        return isinstance(out, list) and all(fits(x, schema.get("items", {})) for x in out)
    if kind == "string":
        return isinstance(out, str)
    if kind == "integer":
        return isinstance(out, int) and not isinstance(out, bool)
    if kind == "boolean":
        return isinstance(out, bool)
    return True


def ask_gemini(system: str, schema: dict, text: str, key: str, models: list[str] | None = None,
               endpoint: str = GEMINI, timeout: int = 90) -> dict:
    """The free tier is sometimes busy (HTTP 429/503): each model is tried once, then it gives up."""
    key = re.sub(r"\s+", "", key or "")
    if not key:
        raise RuntimeError("no Gemini key")
    prompt = (f"{system}\n\nAnswer with JSON only, exactly matching this JSON Schema:\n{json.dumps(schema)}"
              f"\n\n{text}")
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}).encode()
    last = "no model answered"
    for model in models or GEMINI_MODELS:
        req = urllib.request.Request(endpoint.format(model=model), data=body,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = json.load(r)["candidates"][0]["content"]["parts"][0]["text"]
            out = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip())
            if not fits(out, schema):
                raise ValueError("the answer didn't have the shape asked for")
            out["by"] = "gemini"
            return out
        except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError) as e:
            last = f"{model}: {str(e)[:100]}"
    raise RuntimeError(f"Gemini didn't answer ({last})")


def ask(system: str, schema: dict, text: str, model: str = "claude-opus-5-5", max_tokens: int = 4000,
        effort: str = "low", route: dict | None = None, client=None) -> dict:
    """route: {"first": "gemini" | "claude", "gemini_key": str, "claude": bool}. None = Claude only.
    The first choice is tried, then the other; raises when neither can answer."""
    route = route or {"first": "claude", "claude": True}
    order = ["gemini", "claude"] if route.get("first") == "gemini" else ["claude", "gemini"]
    errors = []
    for who in order:
        try:
            if who == "gemini" and route.get("gemini_key"):
                return ask_gemini(system, schema, text, route["gemini_key"])
            if who == "claude" and route.get("claude", True):
                return ask_claude(system, schema, text, model, max_tokens, effort, client)
        except Exception as e:  # noqa: BLE001 - try the other model
            errors.append(f"{who}: {str(e)[:120]}")
            print(f"warn: {errors[-1]}")
    raise RuntimeError("; ".join(errors) or "no model is available (no Gemini key, and Claude is off or over its limit)")

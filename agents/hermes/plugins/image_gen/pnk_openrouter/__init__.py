"""Pinkleberry OpenRouter image-generation backend for Hermes ``image_gen``.

OpenRouter exposes image models (Gemini, Flux, GPT-Image, Recraft, ...) through
the **chat/completions** endpoint with a ``modalities`` field — *not* a DALL-E
style ``/images/generations`` endpoint. The generated images come back as base64
data URLs at ``choices[0].message.images[]``. This backend speaks that wire
format and registers as the ``pnk-openrouter`` image_gen provider.

Activate by setting ``image_gen.provider: pnk-openrouter`` in ``config.yaml``:

    image_gen:
      provider: pnk-openrouter
      pnk-openrouter:
        model: google/gemini-2.5-flash-image     # any OpenRouter image model id
        base_url: https://openrouter.ai/api/v1    # or an upstream gateway route
        modalities: [image, text]                 # use [image] for image-only models

Model selection precedence (first hit wins):
  1. ``OPENROUTER_IMAGE_MODEL`` env var
  2. ``image_gen.pnk-openrouter.model``
  3. ``image_gen.model`` (when it names a known model)
  4. :data:`DEFAULT_MODEL`

This file is portable / topology-free: every endpoint and key is read from
config or env at runtime. Real keys are expected to be injected by an upstream
gateway (so the container never holds one); a direct OpenRouter key in
``OPENROUTER_API_KEY`` also works.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)

logger = logging.getLogger(__name__)

PROVIDER = "pnk-openrouter"
DEFAULT_MODEL = "google/gemini-2.5-flash-image"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# Curated subset of OpenRouter's image models (the full set is discoverable via
# the models API: GET /api/v1/models?output_modalities=image). ``image_only``
# marks models that reject ``["image","text"]`` and need ``["image"]``.
_MODELS: Dict[str, Dict[str, Any]] = {
    "google/gemini-2.5-flash-image": {
        "display": "Gemini 2.5 Flash Image",
        "speed": "fast",
        "strengths": "Cheap, fast, image+text, accepts image input (editing). Default.",
        "image_only": False,
    },
    "google/gemini-3.1-flash-image-preview": {
        "display": "Gemini 3.1 Flash Image (preview)",
        "speed": "fast",
        "strengths": "Newer Gemini flash image",
        "image_only": False,
    },
    "openai/gpt-5-image": {
        "display": "GPT-5 Image",
        "speed": "medium",
        "strengths": "Strong prompt adherence, image+text",
        "image_only": False,
    },
    "black-forest-labs/flux.2-pro": {
        "display": "FLUX.2 Pro",
        "speed": "medium",
        "strengths": "Top dedicated image quality",
        "image_only": True,
    },
    "x-ai/grok-imagine-image-quality": {
        "display": "Grok Imagine (quality)",
        "speed": "medium",
        "strengths": "Flat per-image price",
        "image_only": True,
    },
}

_DATA_URL_RE = re.compile(r"^data:(?P<mime>image/[\w.+-]+);base64,(?P<data>.+)$", re.DOTALL)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _image_gen_config() -> Dict[str, Any]:
    """Read the ``image_gen`` section from config.yaml ({} on any failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _sub_config() -> Dict[str, Any]:
    sub = _image_gen_config().get(PROVIDER)
    return sub if isinstance(sub, dict) else {}


def _resolve_model() -> str:
    env_override = os.environ.get("OPENROUTER_IMAGE_MODEL")
    if env_override:
        return env_override
    sub = _sub_config()
    if isinstance(sub.get("model"), str) and sub["model"].strip():
        return sub["model"].strip()
    top = _image_gen_config().get("model")
    if isinstance(top, str) and top in _MODELS:
        return top
    return DEFAULT_MODEL


def _resolve_base_url() -> str:
    sub = _sub_config()
    url = sub.get("base_url") or os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL
    return str(url).rstrip("/")


def _resolve_modalities(model: str) -> List[str]:
    sub = _sub_config()
    configured = sub.get("modalities")
    if isinstance(configured, list) and configured:
        return [str(m) for m in configured]
    if _MODELS.get(model, {}).get("image_only"):
        return ["image"]
    return ["image", "text"]


def _api_key() -> str:
    # Real key is normally injected by an upstream gateway (it overwrites the
    # Authorization header), so a placeholder is fine when going through one.
    return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY") or "gateway-injected"


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class PnkOpenRouterImageGenProvider(ImageGenProvider):
    """OpenRouter ``chat/completions`` + ``modalities`` image backend."""

    @property
    def name(self) -> str:
        return PROVIDER

    @property
    def display_name(self) -> str:
        return "OpenRouter (Pinkleberry)"

    def is_available(self) -> bool:
        try:
            import httpx  # noqa: F401
        except ImportError:
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": "varies",
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "OpenRouter (Pinkleberry)",
            "badge": "paid",
            "tag": "Any OpenRouter image model via chat/completions + modalities",
            "env_vars": [
                {
                    "key": "OPENROUTER_API_KEY",
                    "prompt": "OpenRouter API key (omit if an upstream gateway injects it)",
                    "url": "https://openrouter.ai/keys",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider=PROVIDER,
                aspect_ratio=aspect,
            )

        try:
            import httpx
        except ImportError:
            return error_response(
                error="httpx is not installed",
                error_type="missing_dependency",
                provider=PROVIDER,
                aspect_ratio=aspect,
            )

        model = _resolve_model()
        base_url = _resolve_base_url()
        modalities = _resolve_modalities(model)
        # chat/completions has no standard size parameter; embed the aspect as a hint.
        content = f"{prompt}\n\n(Render at {aspect} aspect ratio.)"
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "modalities": modalities,
            # Route only to zero-data-retention endpoints. This provider is
            # OpenRouter-only, so the flag is unconditional: a model with no ZDR
            # endpoint should fail loudly rather than silently fall back.
            "provider": {"zdr": True},
        }
        headers = {
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        }
        endpoint = f"{base_url}/chat/completions"

        try:
            resp = httpx.post(endpoint, json=payload, headers=headers, timeout=180.0)
        except Exception as exc:
            return error_response(
                error=f"OpenRouter request failed: {exc}",
                error_type="api_error",
                provider=PROVIDER,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if resp.status_code != 200:
            return error_response(
                error=f"OpenRouter HTTP {resp.status_code}: {resp.text[:300]}",
                error_type="api_error",
                provider=PROVIDER,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            body = resp.json()
            message = body["choices"][0]["message"]
            images = message.get("images") or []
        except Exception as exc:
            return error_response(
                error=f"Unexpected OpenRouter response shape: {exc}",
                error_type="empty_response",
                provider=PROVIDER,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        image_ref = _extract_and_save(images, model)
        if image_ref is None:
            return error_response(
                error="OpenRouter returned no usable image",
                error_type="empty_response",
                provider=PROVIDER,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {"modalities": modalities}
        revised = message.get("content")
        if isinstance(revised, str) and revised.strip():
            extra["revised_prompt"] = revised.strip()

        return success_response(
            image=image_ref,
            model=model,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=PROVIDER,
            extra=extra,
        )


def _extract_and_save(images: List[Any], model: str) -> Optional[str]:
    """Pull the first image out of ``message.images`` and cache it locally.

    Returns the saved path, or ``None`` if nothing usable was found. Raises
    nothing — save failures are swallowed and surface as ``None``.
    """
    slug = model.split("/")[-1].replace(":", "-")
    for item in images:
        url: Optional[str] = None
        if isinstance(item, dict):
            iu = item.get("image_url")
            if isinstance(iu, dict):
                url = iu.get("url")
            elif isinstance(iu, str):
                url = iu
            url = url or item.get("url")
        elif isinstance(item, str):
            url = item
        if not url:
            continue
        try:
            match = _DATA_URL_RE.match(url)
            if match:
                return str(save_b64_image(match.group("data"), prefix=f"pnk-openrouter_{slug}"))
            if url.startswith("http://") or url.startswith("https://"):
                return str(save_url_image(url, prefix=f"pnk-openrouter_{slug}"))
            # bare base64 with no data: prefix
            return str(save_b64_image(url, prefix=f"pnk-openrouter_{slug}"))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to save OpenRouter image: %s", exc)
            continue
    return None


def register(ctx) -> None:
    """Plugin entry point — wire the provider into the image_gen registry."""
    ctx.register_image_gen_provider(PnkOpenRouterImageGenProvider())

"""RemoteCLIP text embedding service.

Wraps `open_clip` + the RemoteCLIP ViT-B/32 checkpoint to turn text (collection
descriptions at index time, the query string at search time) into 512-dim,
L2-normalized vectors that live in the same space as RemoteCLIP image
embeddings — so later phases can add cross-modal (text query -> item thumbnail)
retrieval without changing the vector store.

Loading is **lazy and process-wide**: the model (and its ~hundreds-of-MB
checkpoint) is fetched from the HuggingFace hub and instantiated on first use,
then cached. Nothing loads at import time, so importing this module (or the app)
stays cheap.

The encode functions are synchronous and CPU/GPU-bound. Async callers MUST
offload them, e.g. ``await asyncio.to_thread(embed_texts, batch)``, so the event
loop is never blocked during inference.
"""

from __future__ import annotations

import logging
import threading

from app.config import get_settings

logger = logging.getLogger(__name__)

# RemoteCLIP ViT-B/32 produces 512-dim embeddings. Must match the pgvector
# column dimension (see the resize_embedding_to_512 migration).
EMBEDDING_DIM = 512

# A healthy RemoteCLIP load leaves 0 missing keys; a wrong checkpoint/model
# leaves ~all of them missing. Refuse to serve if more than this fraction of the
# model's parameters weren't loaded (see _check_state_dict_applied).
_MAX_MISSING_FRACTION = 0.05

# NOTE ON SCORE FLATNESS: RemoteCLIP's *text* encoder is anisotropic — unrelated
# short strings sit ~0.76-0.80 cosine apart. Flat similarity scores across
# distinct collections are therefore expected from a correctly-loaded model, NOT
# a sign of untrained weights. Ranking discrimination for short queries comes
# from the lexical path, not from spreading these cosines.


def _check_state_dict_applied(n_missing: int, n_total: int) -> None:
    """Raise if the checkpoint applied to too few of the model's parameters.

    ``load_state_dict(strict=False)`` silently tolerates a total key mismatch,
    leaving the randomly-initialized weights in place — an untrained encoder
    would then serve traffic. This turns that silent failure into a loud one.
    """
    if n_total and n_missing > _MAX_MISSING_FRACTION * n_total:
        raise RuntimeError(
            f"RemoteCLIP checkpoint applied to only {n_total - n_missing}/{n_total} "
            f"model parameters ({n_missing} missing) — checkpoint/model mismatch; "
            "refusing to serve randomly-initialized weights."
        )

# Lazily-initialized singleton + a lock so a race during first use can't build
# the model twice.
_embedder: "RemoteCLIPEmbedder | None" = None
_lock = threading.Lock()


class RemoteCLIPEmbedder:
    """Holds the loaded RemoteCLIP model and tokenizer and encodes text."""

    def __init__(self) -> None:
        # Imports are deferred to construction so the heavy ML stack (torch,
        # open_clip) is only imported when embeddings are actually needed.
        import open_clip
        import torch
        from huggingface_hub import hf_hub_download

        settings = get_settings()
        self._torch = torch
        self.device = _resolve_device(settings.embedding_device, torch)
        self.batch_size = settings.embedding_batch_size

        logger.info(
            "Loading RemoteCLIP (%s, %s) on %s",
            settings.embedding_model_name,
            settings.remoteclip_checkpoint,
            self.device,
        )

        model, _, _ = open_clip.create_model_and_transforms(settings.embedding_model_name)
        self.tokenizer = open_clip.get_tokenizer(settings.embedding_model_name)

        ckpt_path = hf_hub_download(
            repo_id=settings.remoteclip_repo,
            filename=settings.remoteclip_checkpoint,
            token=settings.hf_token,  # None = anonymous (rate-limited)
        )
        state_dict = torch.load(ckpt_path, map_location="cpu")
        missing, unexpected = model.load_state_dict(state_dict, strict=False)
        # Fail loudly if the checkpoint didn't actually apply (would leave the
        # random init in place under strict=False).
        _check_state_dict_applied(len(missing), len(model.state_dict()))
        if missing or unexpected:
            # RemoteCLIP checkpoints are plain state dicts; a few non-weight keys
            # (e.g. logit_scale/position_ids) may differ. Log but don't fail.
            logger.warning(
                "RemoteCLIP load: %d missing / %d unexpected keys (non-fatal)",
                len(missing),
                len(unexpected),
            )

        self.model = model.to(self.device).eval()

        # Fail fast if the checkpoint's text tower doesn't match EMBEDDING_DIM,
        # which would silently break the pgvector column.
        probe = self.embed_texts(["dimension probe"])
        if len(probe[0]) != EMBEDDING_DIM:
            raise RuntimeError(
                f"RemoteCLIP text embedding dim {len(probe[0])} != expected {EMBEDDING_DIM}; "
                "check embedding_model_name / checkpoint."
            )
        logger.info("RemoteCLIP ready (dim=%d)", EMBEDDING_DIM)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts into L2-normalized 512-dim vectors (batched)."""
        torch = self._torch
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            tokens = self.tokenizer(batch).to(self.device)
            with torch.no_grad():
                feats = self.model.encode_text(tokens)
                feats = feats / feats.norm(dim=-1, keepdim=True)
            out.extend(feats.float().cpu().tolist())
        return out

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string into one 512-dim vector."""
        return self.embed_texts([text])[0]


def _resolve_device(preference: str, torch) -> str:
    """Resolve the torch device, honoring 'auto'."""
    if preference and preference != "auto":
        return preference
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def get_embedder() -> RemoteCLIPEmbedder:
    """Return the process-wide embedder, constructing it on first call."""
    global _embedder
    if _embedder is None:
        with _lock:
            if _embedder is None:
                _embedder = RemoteCLIPEmbedder()
    return _embedder


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Convenience wrapper: embed many texts via the shared embedder."""
    return get_embedder().embed_texts(texts)


def embed_query(text: str) -> list[float]:
    """Convenience wrapper: embed one query string via the shared embedder."""
    return get_embedder().embed_query(text)

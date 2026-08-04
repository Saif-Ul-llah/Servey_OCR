"""Adapters for the candidate recognizers evaluated in Phase 0.

A note on engine choice, because the earlier plan document got this wrong:
there is no "EasyOCR handwritten model". EasyOCR and PaddleOCR both ship
*scene-text* recognizers trained on printed signage; they read this notebook's
neat block hand better than nothing, but they are not handwriting models. The
only off-the-shelf handwriting recognizers worth benchmarking here are the
TrOCR ``*-handwritten`` checkpoints, which are slower on CPU. The bake-off
exists to measure that trade-off on the real pages rather than argue about it.

Every adapter imports its backend inside ``_load`` so that an uninstalled
package produces an unavailable engine, not a crash.
"""

from __future__ import annotations

import numpy as np

from local_ocr.ocr.base import ALPHABETS, Engine, Field, Recognition, postprocess


def _to_rgb(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return np.stack([image] * 3, axis=-1)
    return image[:, :, :3]


class EasyOCREngine(Engine):
    """EasyOCR's English recognizer. Fast on CPU, scene-text trained.

    Takes a per-call ``allowlist``, which is the one place the field alphabet can
    be pushed down into the decoder rather than applied afterwards.
    """

    name = "easyocr"

    def __init__(self, languages: tuple[str, ...] = ("en",)):
        super().__init__()
        self._languages = list(languages)
        self._reader = None

    def _load(self) -> None:
        try:
            import easyocr
        except ImportError as exc:  # pragma: no cover - depends on environment
            self.unavailable_reason = f"easyocr not installed ({exc})"
            return
        self._reader = easyocr.Reader(self._languages, gpu=False, verbose=False)

    def _recognise(self, image: np.ndarray, field: Field) -> Recognition:
        if self._reader is None:
            return Recognition(text="", confidence=0.0, engine=self.name)
        results = self._reader.readtext(
            _to_rgb(image),
            detail=1,
            paragraph=False,
            allowlist=ALPHABETS[field],
        )
        if not results:
            return Recognition(text="", confidence=0.0, engine=self.name)
        # A cell crop should hold one line; join left-to-right if it split.
        results.sort(key=lambda r: r[0][0][0])
        text = " ".join(r[1] for r in results)
        confidence = float(min(r[2] for r in results))
        return Recognition(text=postprocess(text, field), confidence=confidence, engine=self.name)


class PaddleOCREngine(Engine):
    """PaddleOCR's recognition head. The fastest CPU option in the bake-off."""

    name = "paddleocr"

    def __init__(self, lang: str = "en"):
        super().__init__()
        self._lang = lang
        self._ocr = None

    def _load(self) -> None:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:  # pragma: no cover - depends on environment
            self.unavailable_reason = f"paddleocr not installed ({exc})"
            return
        # Detection is off: the layout stage has already isolated the cell.
        self._ocr = PaddleOCR(use_angle_cls=False, lang=self._lang, show_log=False, use_gpu=False)

    def _recognise(self, image: np.ndarray, field: Field) -> Recognition:
        if self._ocr is None:
            return Recognition(text="", confidence=0.0, engine=self.name)
        results = self._ocr.ocr(_to_rgb(image), det=False, cls=False)
        pairs = [item for group in (results or []) for item in (group or [])]
        if not pairs:
            return Recognition(text="", confidence=0.0, engine=self.name)
        text, confidence = pairs[0][0], float(pairs[0][1])
        return Recognition(text=postprocess(text, field), confidence=confidence, engine=self.name)


def _load_trocr_processor(checkpoint: str):
    """Load a TrOCR processor, working around transformers 5.x tokenizer breakage.

    transformers 5.x cannot convert several older TrOCR tokenizers to the fast
    format (the slow->fast converters were dropped and the checkpoints ship no
    ``tokenizer.json``). For byte-level-BPE checkpoints -- ``*-base-*``, which
    carry ``vocab.json`` + ``merges.txt`` -- we build the fast tokenizer directly
    and skip the broken path. Checkpoints that ship only a SentencePiece model
    (``*-small-*``) cannot be loaded on this stack and raise, so the caller marks
    them unavailable rather than crashing.
    """
    from transformers import TrOCRProcessor

    try:
        return TrOCRProcessor.from_pretrained(checkpoint)
    except ValueError:
        pass  # fast-tokenizer conversion failed; fall back to a manual build

    import tempfile

    from huggingface_hub import hf_hub_download
    from tokenizers import ByteLevelBPETokenizer
    from transformers import AutoImageProcessor, PreTrainedTokenizerFast

    vocab = hf_hub_download(checkpoint, "vocab.json")   # raises if not a BPE checkpoint
    merges = hf_hub_download(checkpoint, "merges.txt")
    tokenizer_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    ByteLevelBPETokenizer(vocab, merges).save(tokenizer_file)
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_file=tokenizer_file,
        bos_token="<s>", eos_token="</s>", unk_token="<unk>",
        pad_token="<pad>", cls_token="<s>", sep_token="</s>", mask_token="<mask>",
    )
    # use_fast=False keeps the image processor off torchvision-only code paths.
    image_processor = AutoImageProcessor.from_pretrained(checkpoint, use_fast=False)
    return TrOCRProcessor(image_processor=image_processor, tokenizer=tokenizer)


class TrOCREngine(Engine):
    """Microsoft TrOCR, the only genuine handwriting model in the bake-off.

    ``trocr-small-handwritten`` is the CPU-realistic option;
    ``trocr-base-handwritten`` is more accurate and several times slower. Both
    are benchmarked so the accuracy-per-millisecond trade-off is a measurement
    rather than a guess.

    TrOCR is a seq2seq model, so the field alphabet cannot be handed to it as an
    allowlist; it is applied to the output instead. Constrained decoding via a
    logits processor is a Phase 2 improvement.
    """

    def __init__(self, checkpoint: str = "microsoft/trocr-small-handwritten", constrain: bool = True):
        super().__init__()
        self.checkpoint = checkpoint
        self.name = f"trocr:{checkpoint.rsplit('/', 1)[-1]}"
        #: Restrict decoding to the field's grammar. Off only for benchmarking
        #: how much the constraint is worth.
        self.constrain = constrain
        self._processor = None
        self._model = None
        self._constraints: dict[Field, object] = {}

    def _load(self) -> None:
        try:
            import torch
            from transformers import VisionEncoderDecoderModel
        except ImportError as exc:  # pragma: no cover - depends on environment
            self.unavailable_reason = f"transformers/torch not installed ({exc})"
            return

        try:
            self._processor = _load_trocr_processor(self.checkpoint)
        except Exception as exc:  # noqa: BLE001 - surface, never crash the caller
            self.unavailable_reason = f"could not load {self.checkpoint} tokenizer ({exc})"
            return

        self._torch = torch
        self._model = VisionEncoderDecoderModel.from_pretrained(self.checkpoint)
        self._model.eval()
        torch.set_num_threads(max(1, (torch.get_num_threads() or 4)))

    def _constraint(self, field: Field):
        """Cached grammar processor for `field`; None when unconstrained."""
        if not self.constrain:
            return None
        if field not in self._constraints:
            from local_ocr.ocr.constrained import build_processor

            tokenizer = self._processor.tokenizer
            self._constraints[field] = build_processor(
                tokenizer,
                field,
                vocab_size=int(self._model.config.decoder.vocab_size),
                eos_token_id=int(tokenizer.eos_token_id or 2),
                torch=self._torch,
            )
        return self._constraints[field]

    def _recognise(self, image: np.ndarray, field: Field) -> Recognition:
        if self._model is None:
            return Recognition(text="", confidence=0.0, engine=self.name)

        pixel_values = self._processor(images=_to_rgb(image), return_tensors="pt").pixel_values
        processor = self._constraint(field)
        with self._torch.no_grad():
            generated = self._model.generate(
                pixel_values,
                max_new_tokens=24,
                num_beams=4,
                output_scores=True,
                return_dict_in_generate=True,
                logits_processor=[processor] if processor is not None else None,
            )
        text = self._processor.batch_decode(generated.sequences, skip_special_tokens=True)[0]

        # `sequences_scores` is a mean log-probability per token; map it into
        # 0..1 so confidences are comparable across engines.
        confidence = 0.0
        scores = getattr(generated, "sequences_scores", None)
        if scores is not None and len(scores):
            confidence = float(self._torch.exp(scores[0]).clamp(0.0, 1.0))

        return Recognition(text=postprocess(text, field), confidence=confidence, engine=self.name)


#: Everything the bake-off knows how to construct, by short name.
REGISTRY: dict[str, callable] = {
    "easyocr": EasyOCREngine,
    "paddleocr": PaddleOCREngine,
    "trocr_small": lambda: TrOCREngine("microsoft/trocr-small-handwritten"),
    "trocr_base": lambda: TrOCREngine("microsoft/trocr-base-handwritten"),
}


def build(name: str) -> Engine:
    if name not in REGISTRY:
        raise KeyError(f"unknown engine {name!r}; known: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[name]()


def available_engines(names: list[str] | None = None) -> list[Engine]:
    """Construct and load the named engines, keeping only the ones that work."""
    engines = []
    for name in names or list(REGISTRY):
        engine = build(name)
        engine.ensure_loaded()
        engines.append(engine)
    return engines

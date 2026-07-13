import json
from pathlib import Path

from django.conf import settings
from google import genai
from google.genai import errors, types
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_incrementing,
)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

CONVERTER_PROMPT = (_PROMPTS_DIR / "converter_prompt.txt").read_text(encoding="utf-8")
VALIDATOR_PROMPT = (_PROMPTS_DIR / "validator_prompt.txt").read_text(encoding="utf-8")
RECONVERT_PROMPT_TEMPLATE = (_PROMPTS_DIR / "reconvert_prompt.txt").read_text(encoding="utf-8")

_client = None

# Retry apenas para erro de limite de requisições (429 / RESOURCE_EXHAUSTED).
# Espera crescente: 30s, 60s, 90s, 120s entre as até 5 tentativas.
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WAIT_START = 30
RATE_LIMIT_WAIT_INCREMENT = 30


def get_client():
    global _client
    if _client is None:
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY não configurada. Defina no arquivo .env.")
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def _is_rate_limit_error(exc):
    return isinstance(exc, errors.APIError) and getattr(exc, "code", None) == 429


def _rate_limit_retry(on_retry):
    def before_sleep(retry_state):
        if on_retry is None:
            return
        wait_seconds = retry_state.next_action.sleep if retry_state.next_action else 0
        on_retry(retry_state.attempt_number, RATE_LIMIT_MAX_ATTEMPTS, round(wait_seconds))

    return retry(
        retry=retry_if_exception(_is_rate_limit_error),
        stop=stop_after_attempt(RATE_LIMIT_MAX_ATTEMPTS),
        wait=wait_incrementing(start=RATE_LIMIT_WAIT_START, increment=RATE_LIMIT_WAIT_INCREMENT),
        before_sleep=before_sleep,
        reraise=True,
    )


def convert_block_to_markdown(pdf_bytes, on_retry=None):
    """Envia um sub-PDF ao Gemini e retorna o Markdown gerado.

    Reexecuta automaticamente em caso de erro 429 (limite de requisições),
    com espera crescente entre tentativas. `on_retry(attempt, max_attempts, wait_seconds)`
    é chamado antes de cada nova tentativa, útil para atualizar a UI.
    """

    @_rate_limit_retry(on_retry)
    def _call():
        client = get_client()
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                CONVERTER_PROMPT,
            ],
        )
        return (response.text or "").strip()

    return _call()


def reconvert_block_with_feedback(pdf_bytes, previous_markdown, issues_text, on_retry=None):
    """Reenvia o sub-PDF pedindo uma nova transcrição que corrija os problemas
    apontados pelo validador na tentativa anterior."""

    prompt = RECONVERT_PROMPT_TEMPLATE.format(
        issues=issues_text,
        previous_markdown=previous_markdown,
    )

    @_rate_limit_retry(on_retry)
    def _call():
        client = get_client()
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                CONVERTER_PROMPT,
                prompt,
            ],
        )
        return (response.text or "").strip()

    return _call()


VALIDATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "aprovado": {"type": "BOOLEAN"},
        "motivo": {"type": "STRING"},
        "trechos_problematicos": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "tipo": {"type": "STRING"},
                    "descricao": {"type": "STRING"},
                    "e_carimbo_rubrica_ou_ilegivel": {"type": "BOOLEAN"},
                },
                "required": ["tipo", "descricao", "e_carimbo_rubrica_ou_ilegivel"],
            },
        },
    },
    "required": ["aprovado", "motivo", "trechos_problematicos"],
}


def _sanitize_validation(validation):
    """Remove da lista de problemas qualquer item que o próprio validador
    classificou como carimbo/rubrica/ilegível, e recalcula `aprovado` com
    base no que sobrar. Isso evita que o modelo reprove um bloco por algo
    que ele mesmo reconhece que deveria ser ignorado."""
    trechos = validation.get("trechos_problematicos", [])
    trechos_relevantes = [t for t in trechos if not t.get("e_carimbo_rubrica_ou_ilegivel")]

    validation["trechos_problematicos"] = trechos_relevantes
    if not trechos_relevantes:
        validation["aprovado"] = True
    return validation


def validate_block(pdf_bytes, markdown_text, on_retry=None):
    """Envia o sub-PDF + Markdown gerado ao Gemini validador.

    Retorna um dict {aprovado, motivo, trechos_problematicos}, já filtrado de
    itens que o próprio validador identificou como carimbo/rubrica/ilegível
    (ver `_sanitize_validation`).
    Mesma política de retry em caso de erro 429 descrita em `convert_block_to_markdown`.
    """

    @_rate_limit_retry(on_retry)
    def _call():
        client = get_client()
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                f"MARKDOWN GERADO PARA VALIDAÇÃO:\n\n{markdown_text}",
                VALIDATOR_PROMPT,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VALIDATION_SCHEMA,
            ),
        )
        return json.loads(response.text)

    return _sanitize_validation(_call())

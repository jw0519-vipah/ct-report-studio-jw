import asyncio
import base64
import json
import os
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_TITLE = "CT Report Studio JW v1 · Web"
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
MAX_FILES = 24
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024

app = FastAPI(title=APP_TITLE)
app.mount("/static", StaticFiles(directory="static"), name="static")


def _secret() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip()


def _headers() -> dict[str, str]:
    h = {"Authorization": f"Bearer {_secret()}", "Content-Type": "application/json"}
    project = os.getenv("OPENAI_PROJECT", "").strip()
    organization = os.getenv("OPENAI_ORG_ID", "").strip()
    if project:
        h["OpenAI-Project"] = project
    if organization:
        h["OpenAI-Organization"] = organization
    return h


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _api_error(status: int, kind: str, title: str, message: str, *, request_id: str | None = None,
               code: str | None = None, retry_after: str | None = None) -> JSONResponse:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {
            "kind": kind,
            "title": title,
            "message": message,
            "http_status": status,
        },
    }
    if request_id:
        payload["error"]["request_id"] = request_id
    if code:
        payload["error"]["code"] = code
    if retry_after:
        payload["error"]["retry_after"] = retry_after
    return JSONResponse(status_code=status, content=payload)


def classify_openai_http_error(resp: httpx.Response) -> JSONResponse:
    body = _safe_json(resp)
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(err.get("code") or err.get("type") or "").strip() or None
    upstream_message = str(err.get("message") or "").strip()
    request_id = resp.headers.get("x-request-id") or resp.headers.get("x-requestid")
    retry_after = resp.headers.get("retry-after")

    if resp.status_code == 401:
        return _api_error(
            401, "authentication_error", "API 키 인증 실패 (401)",
            "서버의 OPENAI_API_KEY Secret이 유효하지 않거나 해당 프로젝트에 접근 권한이 없습니다. Secret을 다시 확인해 주세요.",
            request_id=request_id, code=code,
        )

    if resp.status_code == 429:
        quota_codes = {
            "credit_balance_exhausted",
            "insufficient_quota",
            "organization_usage_limit_exceeded",
            "organization_spend_limit_exceeded",
            "project_spend_limit_exceeded",
        }
        if code in quota_codes or "quota" in (code or "") or "credit" in (code or ""):
            msg = "API 크레딧 또는 사용/지출 한도에 도달했습니다. OpenAI API Billing/Limits를 확인해 주세요."
            kind = "quota_error"
            title = "API 사용 한도/크레딧 오류 (429)"
        else:
            msg = "일시적인 요청 한도 초과입니다. 잠시 후 다시 시도해 주세요."
            kind = "rate_limit_error"
            title = "API Rate limit (429)"
        if upstream_message:
            msg += f" OpenAI: {upstream_message}"
        return _api_error(429, kind, title, msg, request_id=request_id, code=code, retry_after=retry_after)

    if resp.status_code >= 500:
        return _api_error(
            502, "upstream_error", f"OpenAI 서버 오류 ({resp.status_code})",
            "OpenAI 측에서 일시적인 서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            request_id=request_id, code=code,
        )

    message = upstream_message or "OpenAI API 요청을 처리하지 못했습니다."
    return _api_error(resp.status_code, "openai_api_error", f"OpenAI API 오류 ({resp.status_code})", message,
                      request_id=request_id, code=code)


def _timeout_response() -> JSONResponse:
    return _api_error(
        504, "timeout", "연결 시간 초과",
        "OpenAI API가 제한 시간 안에 응답하지 않았습니다. API 키 오류와는 별개입니다. 잠시 후 다시 시도하거나 네트워크/프록시 설정을 확인해 주세요.",
    )


def _network_response(exc: Exception) -> JSONResponse:
    detail = str(exc).strip()
    msg = "서버에서 OpenAI API까지 네트워크 연결을 만들지 못했습니다. DNS, 방화벽, 프록시, TLS 또는 호스팅 네트워크를 확인해 주세요."
    if detail:
        msg += f" ({detail[:220]})"
    return _api_error(502, "network_error", "네트워크 연결 오류", msg)


async def _test_openai_connection() -> JSONResponse | dict[str, Any]:
    if not _secret():
        return _api_error(
            503, "secret_missing", "OpenAI Secret 미설정",
            "서버 환경변수 OPENAI_API_KEY가 설정되어 있지 않습니다. 브라우저에 키를 입력하지 말고 배포 환경의 Secret/Environment Variable에 등록해 주세요.",
        )

    # A lightweight authenticated endpoint is used so connection testing does not need a model generation call.
    timeout = httpx.Timeout(connect=8.0, read=20.0, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(f"{OPENAI_BASE_URL}/v1/models", headers=_headers())
    except httpx.TimeoutException:
        return _timeout_response()
    except (httpx.ConnectError, httpx.NetworkError, httpx.ProxyError, httpx.RemoteProtocolError) as exc:
        return _network_response(exc)
    except httpx.HTTPError as exc:
        return _network_response(exc)

    if resp.is_success:
        body = _safe_json(resp)
        available = {m.get("id") for m in body.get("data", []) if isinstance(m, dict) and m.get("id")}
        model_accessible = DEFAULT_MODEL in available if available else None
        return {
            "ok": True,
            "message": "OpenAI API 연결 및 인증이 정상입니다.",
            "model_default": DEFAULT_MODEL,
            "model_accessible": model_accessible,
            "request_id": resp.headers.get("x-request-id"),
        }
    return classify_openai_http_error(resp)


SYSTEM_PROMPT = r"""
당신은 수의 CT 판독 초안 작성 보조 도구다. 제공된 JPEG CT 스틸 이미지와 사용자의 검토 방향만 근거로 소견서 초안을 작성한다.

반드시 다음 '장진원 스타일' 형식을 지킨다.
CT)_주요 소견
Findings & comments
1. 핵심 관찰 소견
- 부가 설명은 반드시 실제 하이픈(-)으로 시작
- 추가 설명/해부학적 관계/측정/조영 양상/임상적 의미/추적 권고도 같은 번호 아래 - 로 작성

Dx & DDx
- 반드시 영어 의학용어만 사용한다.
- Findings를 다시 설명하지 말고 질환명/진단명 그 자체만 간결하게 작성한다.
- 각 항목은 실제 하이픈(-)으로 시작하는 짧은 diagnosis label이어야 한다.
- 한글, 완전한 설명문, 병변 위치/크기/조영양상 반복, colon(:)을 사용하지 않는다.
- 필요한 경우에만 susp., less likely, metastasis, invasion 정도의 짧은 수식어를 허용한다.
- 예: Prostatic neoplasia / UB neck invasion / Pulmonary metastasis / Regional LN metastasis susp. / UB calculi / Renal cyst / Cholestasis

Impression
사용자가 Impression 포함을 요청한 경우에만 작성한다.

Radiologist : 장진원, DVM, MS

규칙:
- 동그라미 bullet(•, ●)을 사용하지 않는다. 실제 '-' 문자만 사용한다.
- 번호 뒤에 별도의 요약 제목을 붙이지 않고 바로 관찰을 기술한다.
- 한글 문장에 mass, LN, washout, invasion, metastasis, susp., less likely 등의 영상의학 영문 용어를 자연스럽게 혼용한다.
- 보이지 않는 구조를 정상이라고 추정하지 않는다. 제공된 JPEG에서 확인되지 않는 내용을 임의로 추가하지 않는다.
- JPEG 스틸은 전체 DICOM series가 아니므로 확신이 낮은 내용은 '가능성 있음', '배제할 수 없음', '고려됨', 'susp.' 등으로 강도를 조절한다.
- 사용자의 '검토 방향'은 가설이며, 영상에서 실제 확인된 소견과 구분한다.
- 병변의 위치, 크기, attenuation/enhancement, 주변 구조 관계, mass effect/invasion, regional LN 및 metastasis 가능성을 확인 가능한 범위에서 기술한다.
- 정확한 조직학적 진단이 필요하면 FNA/biopsy/조직 검사 또는 추가 영상 검사를 권고할 수 있다.
- Markdown 코드펜스 없이 보고서 본문만 출력한다.
""".strip()


def _extract_output_text(body: dict[str, Any]) -> str:
    if isinstance(body.get("output_text"), str) and body["output_text"].strip():
        return body["output_text"].strip()
    chunks: list[str] = []
    for item in body.get("output", []) if isinstance(body.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(content, dict):
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
    return "\n".join(chunks).strip()


DX_NORMALIZE_PROMPT = r"""
You are editing ONLY the Dx & DDx section of a veterinary CT report in Jang Jin-won style.
Return ONLY the following block, with no code fence and no extra commentary:

Dx & DDx
- diagnosis
- diagnosis

Hard rules:
- English medical terminology only. Absolutely no Korean/Hangul.
- Write disease/diagnosis names themselves, not explanatory sentences and not lesion descriptions.
- Keep each item short and concise, preferably 2-6 words.
- Do not repeat location, size, attenuation, enhancement pattern, or descriptive imaging morphology from Findings.
- Do not use colon (:).
- Use one diagnosis per hyphen line.
- Short qualifiers such as susp., less likely, metastasis, invasion are allowed when clinically necessary.
- Preserve only diagnoses that are supported by the supplied report. Do not invent new findings.
- Prefer terms such as: Prostatic neoplasia, UB neck invasion, Pulmonary metastasis, Regional LN metastasis susp., UB calculi, Renal cyst, Cholestasis.
""".strip()


def _split_dx_section(report: str) -> tuple[str, str, str] | None:
    marker = "Dx & DDx"
    start = report.find(marker)
    if start < 0:
        return None
    body_start = start + len(marker)
    tail_candidates = []
    for tail_marker in ("\nImpression", "\nRadiologist :", "\nRadiologist:"):
        i = report.find(tail_marker, body_start)
        if i >= 0:
            tail_candidates.append(i)
    end = min(tail_candidates) if tail_candidates else len(report)
    return report[:start], report[start:end].strip(), report[end:]


def _dx_needs_normalization(report: str) -> bool:
    parts = _split_dx_section(report)
    if not parts:
        return True
    _, dx_block, _ = parts
    lines = [ln.strip() for ln in dx_block.splitlines()[1:] if ln.strip()]
    if not lines:
        return True
    for line in lines:
        text = line[1:].strip() if line.startswith("-") else line
        if not line.startswith("-"):
            return True
        if any("가" <= ch <= "힣" for ch in text):
            return True
        if ":" in text:
            return True
        # Long lines are usually lesion-description sentences rather than concise diagnosis labels.
        if len(text.split()) > 9:
            return True
        lowered = text.lower()
        verbose_phrases = (
            "adjacent to", "located in", "measuring", "contrast-enhancing",
            "soft tissue mass", "considered", "differential diagnosis",
            "cannot be excluded", "need to differentiate", "arising from",
        )
        if any(p in lowered for p in verbose_phrases):
            return True
    return False


def _replace_dx_section(report: str, normalized_dx: str) -> str:
    parts = _split_dx_section(report)
    if not parts:
        return report
    before, _, after = parts
    normalized_dx = normalized_dx.strip()
    if not normalized_dx.startswith("Dx & DDx"):
        normalized_dx = "Dx & DDx\n" + normalized_dx
    return before + normalized_dx + ("\n" if after and not after.startswith("\n") else "") + after


async def _normalize_dx_ddx(report: str, model: str) -> str:
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": DX_NORMALIZE_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text", "text": report}]},
        ],
        "max_output_tokens": 500,
    }
    timeout = httpx.Timeout(connect=10.0, read=45.0, write=20.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(f"{OPENAI_BASE_URL}/v1/responses", headers=_headers(), json=payload)
    except httpx.HTTPError:
        return report
    if not resp.is_success:
        return report
    normalized = _extract_output_text(_safe_json(resp))
    if not normalized:
        return report
    candidate = _replace_dx_section(report, normalized)
    # Never replace a usable report with a normalization result that still violates the hard rules.
    return candidate if not _dx_needs_normalization(candidate) else report


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "api_key_configured": bool(_secret()),
        "default_model": DEFAULT_MODEL,
        "openai_base_url": OPENAI_BASE_URL,
    }


@app.post("/api/openai/test")
async def openai_test():
    return await _test_openai_connection()


@app.post("/api/report")
async def create_report(
    images: list[UploadFile] = File(...),
    review_direction: str = Form(""),
    model: str = Form(DEFAULT_MODEL),
    include_impression: str = Form("false"),
):
    if not _secret():
        return _api_error(503, "secret_missing", "OpenAI Secret 미설정",
                          "서버 환경변수 OPENAI_API_KEY를 먼저 설정해 주세요.")
    if not images or len(images) > MAX_FILES:
        return _api_error(400, "validation_error", "이미지 개수 오류",
                          f"JPEG 이미지는 1~{MAX_FILES}장까지 선택할 수 있습니다.")

    total = 0
    image_parts: list[dict[str, str]] = []
    for image in images:
        ctype = (image.content_type or "").lower()
        if ctype not in {"image/jpeg", "image/jpg"}:
            return _api_error(400, "validation_error", "파일 형식 오류", "JPEG/JPG 이미지만 업로드할 수 있습니다.")
        raw = await image.read()
        if len(raw) > MAX_FILE_BYTES:
            return _api_error(400, "validation_error", "파일 크기 오류", "이미지 한 장은 4MB 이하여야 합니다.")
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            return _api_error(400, "validation_error", "전체 파일 크기 오류", "선택한 이미지의 합계는 10MB 이하여야 합니다.")
        encoded = base64.b64encode(raw).decode("ascii")
        image_parts.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}"})

    include = include_impression.lower() in {"1", "true", "yes", "on"}
    user_text = (
        "제공된 JPEG CT 스틸을 바탕으로 장진원 스타일 CT 소견서 초안을 작성해 주세요.\n"
        f"Impression 포함: {'예' if include else '아니오'}\n"
        f"사용자 검토 방향: {review_direction.strip() or '없음'}\n"
        "스틸에서 확인되지 않는 구조는 정상이라고 추정하지 마세요."
    )
    content: list[dict[str, str]] = [{"type": "input_text", "text": user_text}, *image_parts]
    payload = {
        "model": (model or DEFAULT_MODEL).strip(),
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
            {"role": "user", "content": content},
        ],
        "max_output_tokens": 5000,
    }

    # Vision/report generation can legitimately take longer than a key-validation call.
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(f"{OPENAI_BASE_URL}/v1/responses", headers=_headers(), json=payload)
    except httpx.TimeoutException:
        return _api_error(504, "timeout", "판독 요청 시간 초과",
                          "OpenAI 응답이 180초 안에 완료되지 않았습니다. 이미지 수를 줄이거나 잠시 후 다시 시도해 주세요.")
    except (httpx.ConnectError, httpx.NetworkError, httpx.ProxyError, httpx.RemoteProtocolError) as exc:
        return _network_response(exc)
    except httpx.HTTPError as exc:
        return _network_response(exc)

    if not resp.is_success:
        return classify_openai_http_error(resp)

    body = _safe_json(resp)
    report = _extract_output_text(body)
    if not report:
        return _api_error(502, "empty_response", "빈 응답",
                          "OpenAI 연결은 성공했지만 소견서 텍스트를 받지 못했습니다. 모델 ID와 Responses API 지원 여부를 확인해 주세요.",
                          request_id=resp.headers.get("x-request-id"))

    # Safety layer for Jang Jin-won style: Dx & DDx must be concise English diagnosis labels only.
    if _dx_needs_normalization(report):
        report = await _normalize_dx_ddx(report, payload["model"])

    return {
        "ok": True,
        "report": report,
        "request_id": resp.headers.get("x-request-id"),
        "model": payload["model"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

# CT Report Studio JW v1 — Web/PWA

맥북/Windows/iPhone의 브라우저에서 동일한 URL로 사용하는 웹 버전입니다. iPhone Safari의 **공유 → 홈 화면에 추가**로 앱처럼 실행할 수 있습니다.

## 주요 기능
- JPEG CT 스틸 최대 24장 업로드
- 장진원 스타일 `Findings & comments → Dx & DDx → Impression(선택)` 생성
- 부가 설명은 실제 `-` bar 사용
- 보고서 직접 편집 / 전체 복사 / TXT 저장
- API 키를 브라우저에 노출하지 않고 서버 `OPENAI_API_KEY` Secret만 사용
- 화면 진입 시 OpenAI 연결/인증 자동 검사
- 401 인증 오류, 429 크레딧/사용한도, 429 rate limit, 네트워크, timeout, OpenAI 5xx를 분리 표시
- PWA manifest 및 service worker 포함

## Render 배포
1. 이 폴더 전체를 GitHub repository 루트에 업로드합니다.
2. Render에서 New → Web Service → GitHub repository 연결 → Docker runtime으로 배포합니다.
3. Render Web Service → Environment에서 다음 Secret/변수를 등록합니다.
   - `OPENAI_API_KEY` : OpenAI API secret key
   - `OPENAI_MODEL` : 기본값 `gpt-6-luna`
   - 필요 시 `OPENAI_PROJECT`, `OPENAI_ORG_ID`
4. Deploy 후 웹주소를 열면 OpenAI 연결 상태가 자동 검사됩니다.

## 모델
기본 모델은 `gpt-6-luna`입니다. 서버 환경변수 `OPENAI_MODEL`로 바꿀 수 있습니다. 최신 OpenAI 주력 모델은 image input과 Responses API를 지원합니다.

## 로컬 실행
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export OPENAI_API_KEY='sk-...'
uvicorn app:app --host 127.0.0.1 --port 8000
```

## 보안
- API key를 HTML/JavaScript/GitHub에 넣지 마세요.
- 반드시 Render 등의 서버 Secret/Environment Variable에 저장하세요.
- 업로드된 JPEG와 보고서는 이 앱 자체에서 영구 저장하지 않습니다.

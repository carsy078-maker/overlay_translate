# 번역 프록시 (Cloudflare Workers)

exe를 남에게 배포하면서 **내 Gemini 키를 노출하지 않는** 방법.

클라이언트(exe)에 심은 비밀은 원리적으로 추출된다(디스어셈블·네트워크 캡처).
그래서 키는 exe가 아니라 **이 서버에만** 두고, exe는 서버 주소만 호출한다.

```
exe ──POST text──▶ Cloudflare Worker(키 보관) ──▶ Gemini ──▶ 번역문 ──▶ exe
```

## 배포 (5분, 무료)

Cloudflare 무료 플랜으로 충분하다(하루 10만 요청). 방법 두 가지 중 하나.

### A. 대시보드로 (설치 없이)

1. [dash.cloudflare.com](https://dash.cloudflare.com) → **Workers & Pages** → **Create** → **Worker**
2. 이름 정하고 **Deploy** → **Edit code** → `worker.js` 내용 붙여넣고 **Deploy**
3. **Settings > Variables and Secrets** 에서 추가:
   - `GEMINI_API_KEY` = 발급받은 Gemini 키 (**Encrypt** 체크)
   - `PROXY_TOKEN` = 아무 긴 랜덤 문자열 (남용 방지용, **Encrypt** 체크)
   - (선택) `GEMINI_MODEL` = `gemini-flash-latest`
4. 워커 주소 확인: `https://<이름>.<계정>.workers.dev`

### B. wrangler CLI로

```bash
npm i -g wrangler
wrangler login
cd proxy
wrangler deploy                       # worker.js 배포
wrangler secret put GEMINI_API_KEY    # 프롬프트에 키 입력
wrangler secret put PROXY_TOKEN       # 랜덤 토큰 입력
```

## 동작 확인

```bash
curl -X POST https://<이름>.<계정>.workers.dev \
  -H "X-Proxy-Token: <토큰>" \
  -H "Content-Type: application/json" \
  -d '{"text":"u on global or garena?","target":"ko"}'
# → {"translated":"너 글섭이야 아니면 가레나야?"}
```

## exe 연결

배포하는 쪽에서 `translator.ini`에 아래를 넣고 exe를 빌드/배포한다
(**Gemini 키는 넣지 않는다** — 프록시 주소와 토큰만):

```ini
[translator]
translation_provider = proxy
proxy_url = https://<이름>.<계정>.workers.dev
proxy_token = <토큰>
```

받는 사람은 키 없이 그냥 실행하면 된다.

## 주의

- **모든 사용자의 번역이 내 Gemini 키 하나로 나간다.** 무료 티어 한도를 여럿이
  나눠 쓰므로, 많이 쓰면 한도에 걸리거나(429) 초과분이 과금될 수 있다.
- `proxy_token`은 exe에서 추출될 수 있다(키는 아님). 남용이 보이면 워커의
  `PROXY_TOKEN`을 새 값으로 바꾸고 exe를 다시 배포하면 된다 — **Gemini 키는 그대로.**
- 더 강한 남용 방지가 필요하면 Cloudflare **Rate Limiting** 규칙을 추가한다.

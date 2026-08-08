/**
 * Discord 오버레이 번역기 — 번역 프록시 (Cloudflare Workers)
 *
 * 목적: 배포되는 exe에 Gemini API 키를 넣지 않기 위해, 키를 이 서버에만 비밀로
 * 두고 exe는 이 서버 주소만 호출하게 한다. exe를 뜯어봐도 키는 나오지 않는다.
 *
 * 서버 비밀(대시보드 Settings > Variables, 또는 `wrangler secret put`):
 *   GEMINI_API_KEY  (필수)  Google AI Studio 키
 *   PROXY_TOKEN     (선택)  남용 방지용 공유 토큰. 설정하면 exe도 같은 값을 보내야 함.
 *   GEMINI_MODEL    (선택)  기본 gemini-flash-latest
 *
 * 요청:  POST /  {"text": "...", "target": "ko"}   헤더 X-Proxy-Token: <토큰>
 * 응답:  {"translated": "..."}  또는  {"error": "..."}
 */

export default {
  async fetch(request, env) {
    // 점검용: GET 으로 열면 시크릿이 바인딩됐는지(값은 노출 안 하고 존재/길이만) 확인.
    if (request.method === "GET") {
      const k = (env.GEMINI_API_KEY || "").trim();
      return json({
        ok: true,
        hasKey: !!k,
        keyLen: k.length,
        hasToken: !!(env.PROXY_TOKEN || "").trim(),
        model: (env.GEMINI_MODEL || "gemini-flash-latest").trim(),
      });
    }
    if (request.method !== "POST") {
      return json({ error: "POST only" }, 405);
    }

    // 남용 방지 토큰(설정한 경우에만 검사). 키가 아니라서 언제든 재발급 가능.
    if (env.PROXY_TOKEN) {
      if (request.headers.get("X-Proxy-Token") !== env.PROXY_TOKEN) {
        return json({ error: "unauthorized" }, 401);
      }
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "bad json" }, 400);
    }
    const text = (body.text || "").toString().slice(0, 2000);
    const target = (body.target || "ko").toString();
    if (!text.trim()) return json({ error: "no text" }, 400);

    // 시크릿에 붙여넣을 때 딸려온 공백/줄바꿈 제거(400의 흔한 원인).
    const apiKey = (env.GEMINI_API_KEY || "").trim();
    const model = (env.GEMINI_MODEL || "gemini-flash-latest").trim();
    const prompt =
      `다음은 디스코드 채팅 메시지야. 자연스러운 ${target} 구어체로 번역해줘. ` +
      `게임 용어·슬랭·줄임말은 맥락에 맞게 자연스럽게 옮기고, 설명이나 따옴표 없이 ` +
      `번역문만 한 줄로 출력해. 이미 ${target}이면 그대로 출력해.\n\n메시지: ${text}`;

    const url =
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent` +
      `?key=${encodeURIComponent(apiKey)}`;

    let r;
    try {
      r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [{ parts: [{ text: prompt }] }],
          generationConfig: { temperature: 0.3 },
        }),
      });
    } catch {
      return json({ error: "upstream fetch failed" }, 502);
    }

    if (!r.ok) {
      // 429는 그대로 전달해 클라이언트가 재시도하도록. 그 외 오류는 원인 파악을
      // 위해 업스트림 메시지를 함께 반환한다(키 값 자체는 노출하지 않는다).
      let detail = "";
      try {
        detail = (await r.text()).slice(0, 300);
      } catch {}
      return json({ error: "upstream " + r.status, detail },
                  r.status === 429 ? 429 : 502);
    }

    let out = "";
    try {
      const data = await r.json();
      out = (data.candidates[0].content.parts[0].text || "").trim();
    } catch {
      out = "";
    }
    return json({ translated: out });
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

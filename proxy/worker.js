/**
 * Discord 오버레이 번역기 — 번역 프록시 (Cloudflare Workers)
 *
 * 목적: 배포되는 exe에 API 키를 넣지 않기 위해, 키를 이 서버에만 비밀로 두고
 * exe는 이 서버 주소만 호출하게 한다. exe를 뜯어봐도 키는 나오지 않는다.
 *
 * 서버 비밀(대시보드 Settings > Variables and Secrets, 또는 `wrangler secret put`):
 *   GROQ_API_KEY    Groq 키 (있으면 Groq 사용 — 무료 한도 넉넉, 권장)
 *   GEMINI_API_KEY  Google AI Studio 키 (GROQ 키 없을 때 사용)
 *   PROXY_TOKEN     (선택) 남용 방지 공유 토큰. 설정하면 exe도 같은 값을 보내야 함.
 *   GROQ_MODEL      (선택) 기본 llama-3.3-70b-versatile
 *   GEMINI_MODEL    (선택) 기본 gemini-flash-latest
 *
 * 요청:  POST /  {"texts": ["..."], "target": "ko"}   (또는 {"text": "..."})
 *        헤더 X-Proxy-Token: <토큰>
 * 응답:  {"translations": ["..."]}  (또는 {"translated": "..."})  /  {"error": "..."}
 */

export default {
  async fetch(request, env) {
    const groqKey = (env.GROQ_API_KEY || "").trim();
    const geminiKey = (env.GEMINI_API_KEY || "").trim();
    const provider = groqKey ? "groq" : geminiKey ? "gemini" : "none";

    // 점검용: GET 으로 시크릿 바인딩 여부(값 노출 없이)와 프로바이더 확인.
    if (request.method === "GET") {
      return json({
        ok: true,
        provider,
        hasKey: provider !== "none",
        hasToken: !!(env.PROXY_TOKEN || "").trim(),
        model:
          provider === "groq"
            ? (env.GROQ_MODEL || "llama-3.3-70b-versatile").trim()
            : (env.GEMINI_MODEL || "gemini-flash-latest").trim(),
      });
    }
    if (request.method !== "POST") return json({ error: "POST only" }, 405);

    if (env.PROXY_TOKEN) {
      if (request.headers.get("X-Proxy-Token") !== env.PROXY_TOKEN) {
        return json({ error: "unauthorized" }, 401);
      }
    }
    if (provider === "none") return json({ error: "no API key on server" }, 500);

    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "bad json" }, 400);
    }
    const target = (body.target || "ko").toString();

    // 배치: texts 배열이면 여러 개를 한 요청으로(무료 한도 절약).
    const texts = Array.isArray(body.texts)
      ? body.texts.map((t) =>
          (t || "").toString().replace(/\n/g, " ").slice(0, 2000))
      : null;
    const single = texts ? null : (body.text || "").toString().slice(0, 2000);
    if (!texts && !single.trim()) return json({ error: "no text" }, 400);
    if (texts && texts.length === 0) return json({ translations: [] });

    const onlyLang =
      `반드시 ${target} 한 가지 언어로만 출력하고, 다른 언어의 문자(한자·` +
      `히라가나·키릴 등)를 절대 섞지 마. `;
    const prompt = texts
      ? `아래 번호 매겨진 메시지들을 각각 자연스러운 ${target} 구어체로 번역해. ` +
        `게임 용어·슬랭·줄임말은 맥락에 맞게. ${onlyLang}반드시 "번호. 번역문" ` +
        `형식으로, 입력과 같은 개수/번호만 출력해. 설명 금지. 이미 ${target}이면 그대로.\n\n` +
        texts.map((t, i) => `${i + 1}. ${t}`).join("\n")
      : `다음은 디스코드 채팅 메시지야. 자연스러운 ${target} 구어체로 번역해줘. ` +
        `게임 용어·슬랭·줄임말은 맥락에 맞게 자연스럽게 옮기고, ${onlyLang}설명이나 ` +
        `따옴표 없이 번역문만 한 줄로 출력해. 이미 ${target}이면 그대로 출력해.\n\n메시지: ${single}`;

    // 프로바이더별 업스트림 호출.
    let r, out = "";
    try {
      if (provider === "groq") {
        const model = (env.GROQ_MODEL || "llama-3.3-70b-versatile").trim();
        r = await fetch("https://api.groq.com/openai/v1/chat/completions", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${groqKey}`,
          },
          body: JSON.stringify({
            model,
            messages: [{ role: "user", content: prompt }],
            temperature: 0.3,
          }),
        });
      } else {
        const model = (env.GEMINI_MODEL || "gemini-flash-latest").trim();
        r = await fetch(
          `https://generativelanguage.googleapis.com/v1beta/models/${model}` +
            `:generateContent?key=${encodeURIComponent(geminiKey)}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              contents: [{ parts: [{ text: prompt }] }],
              generationConfig: { temperature: 0.3 },
            }),
          }
        );
      }
    } catch {
      return json({ error: "upstream fetch failed" }, 502);
    }

    if (!r.ok) {
      let detail = "";
      try {
        detail = (await r.text()).slice(0, 300);
      } catch {}
      return json({ error: "upstream " + r.status, detail },
                  r.status === 429 ? 429 : 502);
    }

    try {
      const data = await r.json();
      out =
        provider === "groq"
          ? (data.choices[0].message.content || "").trim()
          : (data.candidates[0].content.parts[0].text || "").trim();
    } catch {
      out = "";
    }

    if (texts) {
      // "1. 번역\n2. 번역" 응답을 입력 순서대로 되돌린다.
      const map = {};
      for (const line of out.split("\n")) {
        const m = line.trim().match(/^(\d+)[.)]\s*(.*)$/);
        if (m) map[+m[1] - 1] = m[2].trim();
      }
      return json({ translations: texts.map((_t, i) => map[i] || "") });
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

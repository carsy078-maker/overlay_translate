"""
Discord 실시간 화면 오버레이 번역기 (Windows 전용) — v2

v1 대비 바뀐 핵심(실측 기반):
  1. 스캔 범위 축소:  창 전체(≈700ms, 407요소)를 훑지 않고, 채팅 메시지 목록
     컨테이너만 스캔한다. 메시지는 전부 automationId가 "chat-messages-"로
     시작하는 ListItem 안에 들어 있다(언어/버전 무관 안정적 앵커).
  2. 메시지 단위 처리:  디스코드는 메시지 한 줄을 링크/멘션/이모지/문장부호마다
     별개의 Text 조각으로 쪼갠다. 조각을 따로 번역하면 "(", " in " 같은 쓰레기가
     번역되고 문맥이 깨진다. 그래서 한 메시지(ListItem)의 Text 조각을 모아 하나로
     합친 뒤, 통째로 한 번 번역하고, 그 위에 박스 하나를 덮는다.
  3. 유저명/타임스탬프/편집표시/반응숫자 제거:  본문만 남긴다.
  4. 번역 비동기화:  좌표 추적(스캔)과 번역(HTTP)을 분리. 스캔 스레드는 캐시만
     읽고, 캐시에 없는 문장은 번역 워커 스레드 큐에 넣는다. 번역이 도착하면
     다음 프레임에 오버레이가 채워진다 → 스크롤/좌표 추적이 안 끊긴다.
  5. 라벨 diff:  메시지ID를 key로 라벨을 재사용. 매 프레임 전부 지웠다 다시
     만들지 않아 깜빡임이 준다.

번역 백엔드: 무료 비공식 Google 엔드포인트(translate_a/single). API 키 불필요.
  대량 호출 시 일시적으로 차단될 수 있으나 개인용에는 충분.

사전 준비:
  1. (최신 디스코드는 수동 토글이 없음) 접근성 트리가 비어 있으면
     Win+Ctrl+Enter 로 내레이터를 켜서 트리를 활성화한다.
  2. pip install -r requirements.txt
  3. python discord_screen_overlay.py
"""

import ctypes
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from queue import Queue, Empty
from typing import Optional

import requests
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from pywinauto import Desktop

from config import app_dir, load_config
from text_filter import (
    classify_run as _classify_run,
    group_runs_into_lines as _group_runs_into_lines,
    is_visible_rect as _is_visible_rect,
    looks_like_korean as _looks_like_korean,
    parse_numbered_lines as _parse_numbered_lines,
    select_body_runs as _select_body_runs,
)

_LOG_PATH = os.path.join(app_dir(), "translator.log")
_LOG_LOCK = threading.Lock()


def _log(*args):
    """콘솔 없는 --windowed exe는 sys.stdout이 None이라 print가 터진다.
    그래서 콘솔 출력은 안전하게 감싸고, 항상 로그 파일에도 남긴다.
    (windowed exe에서 무슨 일이 일어나는지 볼 수 있는 유일한 창구)

    스캐너/번역워커 등 여러 스레드가 동시에 부른다. 락 없이 같은 파일을 동시에
    열면 Windows 파일 공유 위반으로 줄이 유실되므로 락으로 직렬화한다."""
    msg = " ".join(str(a) for a in args)
    try:
        print(msg)
    except Exception:
        pass
    with _LOG_LOCK:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

# ── 설정 ──────────────────────────────────────────────
# 값은 translator.ini / 환경변수(DTL_*) / 기본값 순으로 결정된다 (config.py 참고).
CONFIG = load_config()

DISCORD_TITLE_RE = ".*Discord.*"
MESSAGE_AUTOMATION_PREFIX = "chat-messages-"

TARGET_LANG = CONFIG.target_lang
PROVIDER = CONFIG.translation_provider
GEMINI_API_KEY = CONFIG.gemini_api_key
GEMINI_MODEL = CONFIG.gemini_model
PROXY_URL = CONFIG.proxy_url
PROXY_TOKEN = CONFIG.proxy_token
SCAN_INTERVAL_SEC = CONFIG.scan_interval_sec
MIN_TEXT_LEN = CONFIG.min_text_len
MAX_TEXT_LEN = CONFIG.max_text_len
TRANSLATE_WORKERS = CONFIG.translate_workers
TRANSLATE_MAX_RETRIES = CONFIG.translate_max_retries
TRANSLATE_RETRY_DELAY = CONFIG.translate_retry_delay
# 한 요청에 최대 몇 문장을 묶을지. LLM(proxy/gemini)은 무료 티어 분당 요청수가
# 적어서, 한 화면(십수 개)을 개별 호출하면 대부분 429로 막힌다. 묶어 보내면
# 화면당 1~2요청으로 줄어 한도에 안 걸린다.
TRANSLATE_BATCH_MAX = 20
FAIL_COOLDOWN = 20            # 일시 실패(429 등) 후 재시도까지 대기(초)
SINGLE_LINE_MAX_H = CONFIG.single_line_max_h
FONT_SIZE = CONFIG.font_size

# 고해상도(배율 조정된) 모니터에서 좌표가 어긋나지 않도록 DPI 인식을 켠다.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass


# ── 번역 ──────────────────────────────────────────────
# 요청 실패(네트워크/레이트리밋)를 '번역할 필요 없음(None)'과 구분하기 위한 표식.
# 이 둘을 뭉뚱그리면 실패한 문장이 영구히 캐시돼서 다시는 번역되지 않는다.
_FAILED = object()


def _google_free_translate(text: str, session=None):
    """무료 비공식 Google 엔드포인트.

    반환: 번역문(str) / None(한국어라 번역 불필요) / _FAILED(요청 실패 → 재시도 대상)
    """
    http = session or requests
    try:
        res = http.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": TARGET_LANG,
                "dt": "t",
                "q": text,
            },
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        res.raise_for_status()
        data = res.json()
    except Exception:
        return _FAILED          # 네트워크 오류/429 등 → 나중에 다시 시도

    try:
        detected = data[2] if len(data) > 2 else None
        if detected == TARGET_LANG:
            return None
        segments = data[0]
        translated = "".join(seg[0] for seg in segments if seg and seg[0])
        translated = translated.strip()
        if not translated or translated == text.strip():
            return None
        return translated
    except Exception:
        return _FAILED          # 응답 형식이 깨짐 → 역시 재시도


# ── Gemini (Google AI Studio, LLM) ─────────────────────
# 구글 기계번역보다 자연스럽다(슬랭·구어체·문맥 반영). 무료 키는 카드 없이 발급.
_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "{model}:generateContent")
_GEMINI_PROMPT = (
    "다음은 디스코드 채팅 메시지야. 자연스러운 한국어 구어체로 번역해줘. "
    "게임 용어·슬랭·줄임말은 맥락에 맞게 자연스럽게 옮기고, 설명이나 따옴표 없이 "
    "번역문만 한 줄로 출력해. 이미 한국어면 그대로 출력해.\n\n메시지: {text}"
)


def _gemini_translate(text: str, session=None):
    """Gemini(LLM)로 자연스럽게 번역. 반환: 번역문 / None / _FAILED(재시도)."""
    http = session or requests
    try:
        res = http.post(
            _GEMINI_URL.format(model=GEMINI_MODEL),
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": _GEMINI_PROMPT.format(text=text)}]}],
                "generationConfig": {"temperature": 0.3},
            },
            timeout=10,
        )
        res.raise_for_status()
        data = res.json()
    except Exception:
        return _FAILED
    try:
        out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if not out or out == text.strip():
            return None
        return out
    except Exception:
        return _FAILED


# ── Proxy (배포용: 키를 서버에 숨김) ───────────────────
# exe엔 키가 안 들어가고, 우리가 띄운 프록시 서버가 키를 쥐고 Gemini로 대신 호출.
def _proxy_translate(text: str, session=None):
    """프록시 서버로 번역 위임. 반환: 번역문 / None / _FAILED(재시도)."""
    http = session or requests
    headers = {}
    if PROXY_TOKEN:
        headers["X-Proxy-Token"] = PROXY_TOKEN
    try:
        res = http.post(PROXY_URL, headers=headers,
                        json={"text": text, "target": TARGET_LANG}, timeout=10)
        if res.status_code == 429:
            return _FAILED                 # 서버측 레이트리밋 → 재시도
        res.raise_for_status()
        data = res.json()
    except Exception:
        return _FAILED
    out = (data.get("translated") or "").strip()
    if not out or out == text.strip():
        return None
    return out


# ── 프로바이더 디스패처 ─────────────────────────────────
_GEMINI_WARNED = False
_PROXY_WARNED = False


def _translate(text: str, session=None):
    """설정된 프로바이더로 번역. 키/주소가 없으면 구글 무료로 폴백."""
    global _GEMINI_WARNED, _PROXY_WARNED
    if PROVIDER == "proxy":
        if PROXY_URL:
            return _proxy_translate(text, session)
        if not _PROXY_WARNED:
            _PROXY_WARNED = True
            _log("[translate] provider=proxy 인데 proxy_url 이 없음 → 구글 무료로 폴백.")
    elif PROVIDER == "gemini":
        if GEMINI_API_KEY:
            return _gemini_translate(text, session)
        if not _GEMINI_WARNED:
            _GEMINI_WARNED = True
            _log("[translate] provider=gemini 인데 키가 없음 → 구글 무료로 폴백. "
                 "translator.ini 의 gemini_api_key 를 채우세요.")
    return _google_free_translate(text, session)


# ── 배치 번역 (LLM 무료 티어 RPM 회피) ──────────────────
_BATCH_PROMPT = (
    "아래 번호 매겨진 메시지들을 각각 자연스러운 {target} 구어체로 번역해. "
    "게임 용어·슬랭·줄임말은 맥락에 맞게. 반드시 {target} 한 가지 언어로만 출력하고 "
    "다른 언어의 문자(한자·히라가나·키릴 등)를 절대 섞지 마. 반드시 '번호. 번역문' "
    "형식으로, 입력과 같은 개수/번호만 출력해. 설명 금지. 이미 {target}이면 그대로.\n\n{body}"
)


def _finalize_batch(texts, parsed):
    """파싱된 번역 리스트를 원문과 비교해 정리(빈 값/동일 → None)."""
    out = []
    for src, tr in zip(texts, parsed):
        tr = (tr or "").strip()
        out.append(None if (not tr or tr == src.strip()) else tr)
    return out


def _proxy_translate_batch(texts, session=None):
    """여러 문장을 프록시 한 번으로 번역. texts와 같은 길이 리스트(번역문/None/_FAILED)."""
    http = session or requests
    headers = {}
    if PROXY_TOKEN:
        headers["X-Proxy-Token"] = PROXY_TOKEN
    try:
        res = http.post(PROXY_URL, headers=headers,
                        json={"texts": texts, "target": TARGET_LANG}, timeout=20)
        if res.status_code == 429:
            return [_FAILED] * len(texts)
        res.raise_for_status()
        arr = res.json().get("translations")
    except Exception:
        return [_FAILED] * len(texts)
    if not isinstance(arr, list) or len(arr) != len(texts):
        return [_FAILED] * len(texts)
    return _finalize_batch(texts, arr)


def _gemini_translate_batch(texts, session=None):
    """여러 문장을 Gemini 한 번으로 번역(번호 목록 프롬프트)."""
    http = session or requests
    body = "\n".join(f"{i+1}. {t.replace(chr(10), ' ')}" for i, t in enumerate(texts))
    prompt = _BATCH_PROMPT.format(target=TARGET_LANG, body=body)
    try:
        res = http.post(_GEMINI_URL.format(model=GEMINI_MODEL),
                        params={"key": GEMINI_API_KEY},
                        json={"contents": [{"parts": [{"text": prompt}]}],
                              "generationConfig": {"temperature": 0.3}},
                        timeout=20)
        if res.status_code == 429:
            return [_FAILED] * len(texts)
        res.raise_for_status()
        out = res.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return [_FAILED] * len(texts)
    return _finalize_batch(texts, _parse_numbered_lines(out, len(texts)))


def _translate_batch(texts, session=None):
    """설정된 프로바이더로 여러 문장을 한 번에 번역."""
    if PROVIDER == "proxy" and PROXY_URL:
        return _proxy_translate_batch(texts, session)
    if PROVIDER == "gemini" and GEMINI_API_KEY:
        return _gemini_translate_batch(texts, session)
    # 구글 무료는 배치 API가 없고 RPM 여유가 있어 개별 번역.
    return [_translate(t, session) for t in texts]


class TranslationManager:
    """번역 캐시 + 백그라운드 워커 풀.

    get(text): 캐시에 있으면 즉시 반환(번역문 or None), 없으면 큐에 넣고 None.
    None은 '아직 없음 / 한국어 / 실패' 모두를 뜻한다. 워커가 채우면 다음 프레임에 뜬다.

    한 화면에 번역할 문장이 십수 개 나오는데, LLM 무료 티어는 분당 요청 수가
    적다. 그래서 워커가 큐를 '한 번에 여러 개' 꺼내(batch) 한 요청으로 묶어
    번역한다 → 화면당 요청이 1~2개로 줄어 429(레이트리밋)를 피한다.
    """

    def __init__(self, num_workers: int = TRANSLATE_WORKERS):
        self._cache: dict[str, Optional[str]] = {}
        self._pending: set[str] = set()
        self._retry_after: dict[str, float] = {}   # 일시 실패(429 등) 재시도 예약 시각
        self._queue: "Queue[tuple]" = Queue()
        self._lock = threading.Lock()
        for _ in range(max(1, num_workers)):
            threading.Thread(target=self._worker, daemon=True).start()

    def get(self, text: str) -> Optional[str]:
        with self._lock:
            if text in self._cache:
                return self._cache[text]
            ra = self._retry_after.get(text)
            if ra is not None:
                if time.time() < ra:
                    return None                    # 쿨다운 중 — 아직 재요청 안 함
                del self._retry_after[text]        # 쿨다운 끝 → 다시 시도 허용
            if text not in self._pending:
                self._pending.add(text)
                self._queue.put((text, 0))
        return None

    def _worker(self):
        session = requests.Session()
        while True:
            try:
                first = self._queue.get(timeout=1)
            except Empty:
                continue
            # 큐에 쌓인 것을 최대 BATCH_MAX개까지 한꺼번에 꺼낸다.
            batch = [first]
            while len(batch) < TRANSLATE_BATCH_MAX:
                try:
                    batch.append(self._queue.get_nowait())
                except Empty:
                    break

            # 한국어는 API를 안 부르고 바로 None. 나머지만 배치 번역.
            results = [None] * len(batch)
            todo = [(i, t) for i, (t, _a) in enumerate(batch)
                    if not _looks_like_korean(t)]
            if todo:
                trs = _translate_batch([t for _i, t in todo], session)
                for (i, _t), tr in zip(todo, trs):
                    results[i] = tr

            requeue = []
            with self._lock:
                for (text, attempt), res in zip(batch, results):
                    if res is _FAILED:
                        # 실패는 캐시하지 않는다(영구 원문 방지). 재시도 대상으로.
                        if attempt < TRANSLATE_MAX_RETRIES:
                            requeue.append((text, attempt + 1))
                        else:
                            # 일시 실패(429/네트워크)는 영구 캐시하지 않는다. 쿨다운 뒤
                            # 다시 시도하도록 예약 → 한도가 회복되면 자동으로 재번역.
                            _log(f"[translate] 재시도 예약(일시 실패): {text[:40]!r}")
                            self._retry_after[text] = time.time() + FAIL_COOLDOWN
                            self._pending.discard(text)
                    else:
                        self._cache[text] = res
                        self._pending.discard(text)

            if requeue:
                time.sleep(TRANSLATE_RETRY_DELAY)   # 잠깐 쉬고 통째로 재시도
                for item in requeue:
                    self._queue.put(item)


# ── 스캔 (메시지 목록만) ────────────────────────────────
@dataclass
class MessageBlock:
    msg_id: str
    text: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom (스크린 좌표)


class _ListCache:
    def __init__(self):
        self.msg_list = None


def _rect_tuple(el) -> Optional[tuple[int, int, int, int]]:
    try:
        r = el.rectangle()
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


_find_win_logged = False
# Discord 본체 창의 클래스. 우리 조작 창(Qt)이나 Discord 숨은 보조 창을 걸러낸다.
_DISCORD_WIN_CLASS = "Chrome_WidgetWin_1"


def find_discord_window():
    """제목에 Discord가 든 창이 여러 개(우리 창 포함, Discord 보조 창 포함)여도
    진짜 본체 하나를 고른다. 단일 매칭을 가정하면 ElementAmbiguousError로 죽는다."""
    global _find_win_logged
    try:
        wins = Desktop(backend="uia").windows(
            title_re=DISCORD_TITLE_RE, top_level_only=True
        )
    except Exception:
        if not _find_win_logged:      # 첫 실패만 남긴다 (0.35초마다 도배 방지)
            _find_win_logged = True
            _log("[find] Discord 창 탐색 실패:\n" + traceback.format_exc())
        return None

    candidates = []
    for w in wins:
        try:
            if not w.is_visible():
                continue
            cls = w.element_info.class_name or ""
            r = w.rectangle()
            area = (r.right - r.left) * (r.bottom - r.top)
            if area <= 0:
                continue
            candidates.append((cls, area, w))
        except Exception:
            continue

    # Discord 본체(Chrome_WidgetWin_1) 우선, 그중 가장 큰 창.
    chrome = [c for c in candidates if c[0] == _DISCORD_WIN_CLASS]
    pool = chrome or candidates
    if not pool:
        return None
    pool.sort(key=lambda c: c[1], reverse=True)
    return pool[0][2]


def _has_message_items(lst) -> bool:
    """이 List가 실제 채팅 메시지(chat-messages-*)를 담고 있는가."""
    try:
        kids = lst.children(control_type="ListItem")
    except Exception:
        return False
    for k in kids:
        try:
            auto = k.element_info.automation_id or ""
        except Exception:
            continue
        if auto.startswith(MESSAGE_AUTOMATION_PREFIX):
            return True
    return False


def find_message_list(discord_win):
    """자식 ListItem의 automationId가 chat-messages-* 인 List를 찾는다.

    창 전체에서 List 컨트롤(수십 개 수준)만 훑으므로 Text 전체 스캔보다 훨씬 싸다.
    한 번 찾으면 캐시해서 재사용한다.
    """
    try:
        lists = discord_win.descendants(control_type="List")
    except Exception:
        return None
    for lst in lists:
        if _has_message_items(lst):
            return lst
    return None


def _extract_message_segments(item, win_rect):
    """메시지 ListItem 하나에서 (본문줄 텍스트, rect) 세그먼트 목록을 뽑는다.

    유저명 처리가 핵심이다. 디스코드는 같은 사람이 연속으로 쓴 메시지를 '묶어서'
    첫 메시지에만 [유저명][타임스탬프] 헤더를 붙이고, 나머지 묶인 메시지는 헤더가
    없다. 그래서 '타임스탬프 뒤 = 본문'으로 단정하면, 헤더 없는 묶인 메시지(대화의
    대부분)의 본문을 통째로 유저명으로 오인해 버린다.

    해결: 타임스탬프 앞/뒤 본문을 나눠 담고,
      - 타임스탬프가 있으면(헤더 메시지) 앞쪽(=유저명)은 버리고 뒤쪽만 본문으로,
      - 타임스탬프가 없으면(묶인 메시지) 앞쪽이 곧 본문이므로 그대로 쓴다.
    """
    try:
        runs = item.descendants(control_type="Text")
    except Exception:
        return []

    raw = []
    for run in runs:
        try:
            t = run.window_text()
        except Exception:
            continue
        raw.append((t, _rect_tuple(run)))

    # 본문만 추림(유저명/타임스탬프/묶인 메시지 처리) → 화면에 렌더된 조각만 남김
    body = _select_body_runs(raw)
    collected = [(t, r) for t, r in body if r and _is_visible_rect(r, win_rect)]
    if not collected:
        return []

    segments = _group_runs_into_lines(collected)
    return [
        (text, rect)
        for text, rect in segments
        if MIN_TEXT_LEN <= len(text) <= MAX_TEXT_LEN
    ]


def _viewport_rect(msg_list, win_rect):
    """메시지가 실제로 보이는 영역(채널 헤더 아래 ~ 입력창 위).

    메시지 List 자체의 rect는 스크롤된 위/아래 콘텐츠까지 포함(뷰포트가 아님)이라,
    그걸로 가시성을 판정하면 헤더 뒤로 스크롤된 메시지까지 번역돼 헤더 위에 박스가
    뜬다. List의 부모 컨테이너가 실제 스크롤 뷰포트라 그 rect를 창과 교집합해 쓴다.
    못 구하면 창 전체로 폴백.
    """
    try:
        pr = _rect_tuple(msg_list.parent())
    except Exception:
        pr = None
    if not pr:
        return win_rect
    l, t = max(pr[0], win_rect[0]), max(pr[1], win_rect[1])
    r, b = min(pr[2], win_rect[2]), min(pr[3], win_rect[3])
    return (l, t, r, b) if (r > l and b > t) else win_rect


class Scanner(QObject):
    blocksReady = pyqtSignal(list)          # list[(MessageBlock, translated)]
    windowRectReady = pyqtSignal(tuple)
    statusReady = pyqtSignal(bool, int, int)  # (디스코드 감지됨?, 보이는 메시지, 번역표시)

    def __init__(self, translator: TranslationManager):
        super().__init__()
        self._running = True
        self._translator = translator
        self._cache = _ListCache()

    def stop(self):
        self._running = False

    def _get_message_list(self, discord_win):
        """캐시된 List 핸들을 재사용하되, 채널을 전환하면 다시 찾는다.

        주의: 채널을 바꾸면 옛 List 핸들이 예외를 던지지 않고 '빈 자식 목록'을
        돌려준다. 그래서 살아있는지(예외 여부)가 아니라, chat-messages 자식이
        실제로 있는지로 검증해야 한다.
        """
        if self._cache.msg_list is not None:
            if _has_message_items(self._cache.msg_list):
                return self._cache.msg_list
            self._cache.msg_list = None      # 채널 바뀜 → 다시 찾는다

        lst = find_message_list(discord_win)
        self._cache.msg_list = lst
        return lst

    def run(self):
        try:
            self._run_loop()
        except Exception:
            # 스레드에서 예외가 터지면 조용히 죽어서 '대기 중'에 멈춘다. 반드시 남긴다.
            _log("[scanner] 스레드 예외로 중단:\n" + traceback.format_exc())

    def _log_state(self, state: str):
        # 상태가 바뀔 때만 로그 (매 0.35초 도배 방지, 어디서 막혔는지 추적용)
        if state != self._last_state:
            self._last_state = state
            _log(f"[scanner] 상태: {state}")

    def _run_loop(self):
        self._last_state = ""
        _log("[scanner] 시작. 디스코드 창을 찾는 중...")
        while self._running:
            discord_win = find_discord_window()
            if discord_win is None or not discord_win.is_visible():
                self._log_state("디스코드 창 못 찾음")
                self.statusReady.emit(False, 0, 0)
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            win_rect = _rect_tuple(discord_win)
            if win_rect is None:
                self._log_state("디스코드 창 좌표 못 읽음")
                self.statusReady.emit(False, 0, 0)
                time.sleep(SCAN_INTERVAL_SEC)
                continue
            self.windowRectReady.emit(win_rect)

            msg_list = self._get_message_list(discord_win)
            if msg_list is None:
                self._log_state("메시지 목록 못 찾음 (내레이터 필요할 수 있음)")
                self.statusReady.emit(True, 0, 0)
                time.sleep(SCAN_INTERVAL_SEC)
                continue
            self._log_state("정상 스캔 중")

            # 실제로 보이는 메시지 영역(헤더 아래~입력창 위). 창 전체가 아니라 이걸로
            # 가시성을 판정해야 스크롤로 가려진 메시지가 헤더 위에 안 뜬다.
            clip = _viewport_rect(msg_list, win_rect)

            t0 = time.perf_counter()
            try:
                items = msg_list.children(control_type="ListItem")
            except Exception:
                self._cache.msg_list = None
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            results = []
            scanned = 0
            for item in items:
                try:
                    auto = item.element_info.automation_id or ""
                except Exception:
                    continue
                if not auto.startswith(MESSAGE_AUTOMATION_PREFIX):
                    continue
                item_rect = _rect_tuple(item)
                if item_rect is None or not _is_visible_rect(item_rect, clip):
                    continue
                scanned += 1
                segments = _extract_message_segments(item, clip)
                for i, (text, rect) in enumerate(segments):
                    translated = self._translator.get(text)
                    if translated:
                        key = f"{auto}#{i}"
                        results.append(
                            (MessageBlock(msg_id=key, text=text, rect=rect),
                             translated)
                        )

            dt = (time.perf_counter() - t0) * 1000
            _log(f"[scanner] 보이는 메시지 {scanned}개, 번역표시 {len(results)}개, "
                  f"스캔 {dt:.0f}ms")
            self.statusReady.emit(True, scanned, len(results))
            self.blocksReady.emit(results)
            time.sleep(SCAN_INTERVAL_SEC)


# ── 오버레이 ──────────────────────────────────────────
class OverlayWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # key(msg_id) -> (QLabel, last_text)
        self.labels: dict[str, tuple[QLabel, str]] = {}
        self.origin = (0, 0, 0, 0)

    def set_window_rect(self, rect):
        self.origin = rect
        left, top, right, bottom = rect
        self.setGeometry(left, top, right - left, bottom - top)

    def _fit_label(self, lbl: QLabel, rect):
        """번역문이 원문보다 길어도 깨지지 않게 라벨 크기를 잡는다.

        원문 rect에 억지로 우겨넣으면(고정 크기 + wordWrap) 한 줄 높이 박스에
        두 줄이 들어가 글자가 잘리고 겹친다. 그래서 위치(좌상단)만 원문에 맞추고,
        크기는 '원문을 덮을 만큼(최소) + 번역문이 들어갈 만큼(필요시 확장)'으로 준다.
        """
        ox, oy, _, _ = self.origin
        l, t, r, b = rect
        x, y = l - ox, t - oy
        ow, oh = max(r - l, 1), max(b - t, 1)

        # 오른쪽으로 늘어나도 화면(오버레이 창) 밖으로는 못 나가게 하는 최대 폭.
        # 너무 넓으면 읽기 나쁘므로 편안한 줄폭(600px)과 창 안쪽 중 작은 값으로 캡.
        cap = min(self.width() - x - 8, 600)
        max_w = max(ow, cap)

        if oh <= SINGLE_LINE_MAX_H:
            # 원문이 한 줄. 번역이 짧으면 한 줄로 가로 확장, 길면 줄바꿈해 아래로.
            lbl.setWordWrap(False)
            hint_w = lbl.sizeHint().width()
            if hint_w <= max_w:
                lbl.setWordWrap(False)
                w = max(ow, hint_w)
                h = max(oh, lbl.sizeHint().height())
            else:
                # 한 줄로는 화면 밖까지 넘침 → 최대 폭으로 줄바꿈, 아래로 확장
                lbl.setWordWrap(True)
                w = max_w
                h = max(oh, lbl.heightForWidth(w))
        else:
            # 원문이 여러 줄인 문단 → 폭을 유지(단, 화면 밖 방지)하고 아래로 확장
            lbl.setWordWrap(True)
            w = min(ow, max_w)
            h = max(oh, lbl.heightForWidth(w))

        lbl.setGeometry(x, y, w, h)

    def update_blocks(self, results):
        current_keys = set()

        for block, translated in results:
            key = block.msg_id
            current_keys.add(key)

            existing = self.labels.get(key)
            if existing is None:
                lbl = QLabel(translated, self)
                lbl.setStyleSheet(
                    "background-color: rgba(30,30,30,255);"
                    f"color: white; font-size: {FONT_SIZE}px; padding: 1px 4px;"
                    "border-radius: 3px;"
                )
                lbl.show()
                self.labels[key] = (lbl, translated)
            else:
                lbl, last_text = existing
                if translated != last_text:
                    lbl.setText(translated)
                    self.labels[key] = (lbl, translated)

            self._fit_label(lbl, block.rect)   # 스크롤 따라 위치/크기 갱신

        # 화면에서 사라진 메시지의 라벨 제거
        for key in list(self.labels.keys()):
            if key not in current_keys:
                lbl, _ = self.labels.pop(key)
                lbl.deleteLater()


class ControlWindow(QWidget):
    """실행 상태를 보여주고, 번역 토글/종료를 할 수 있는 작은 조작 창."""

    def __init__(self, overlay: OverlayWindow):
        super().__init__()
        self.overlay = overlay
        # 제목에 'Discord'를 넣으면 find_discord_window의 검색에 우리 창이 걸려 충돌한다.
        self.setWindowTitle("실시간 오버레이 번역기")
        self.setFixedWidth(300)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        self.status = QLabel("● 시작하는 중…")
        self.status.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.detail = QLabel("디스코드 창을 찾는 중입니다.")
        self.detail.setStyleSheet("color: #666; font-size: 12px;")
        self.detail.setWordWrap(True)

        self.toggle_btn = QPushButton("번역 숨기기")
        self.toggle_btn.clicked.connect(self._toggle_overlay)
        quit_btn = QPushButton("종료")
        quit_btn.clicked.connect(self._quit)

        buttons = QHBoxLayout()
        buttons.addWidget(self.toggle_btn)
        buttons.addWidget(quit_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.detail)
        layout.addLayout(buttons)

    def _toggle_overlay(self):
        show = not self.overlay.isVisible()
        self.overlay.setVisible(show)
        self.toggle_btn.setText("번역 숨기기" if show else "번역 보이기")

    def _quit(self):
        QApplication.instance().quit()

    def closeEvent(self, event):
        # 창의 X 버튼 = 프로그램 종료
        QApplication.instance().quit()
        event.accept()

    def update_status(self, found: bool, scanned: int, translated: int):
        if not found:
            self.status.setText("● 대기 중")
            self.status.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #b0902b;")
            self.detail.setText("디스코드 창을 찾는 중입니다. 디스코드를 켜 두세요.")
        elif scanned == 0:
            self.status.setText("● 실행 중")
            self.status.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #2b8a3e;")
            self.detail.setText(
                "디스코드는 찾았지만 메시지를 못 읽고 있습니다.\n"
                "번역이 안 뜨면 Win+Ctrl+Enter 로 내레이터를 켜 보세요.")
        else:
            self.status.setText("● 번역 중")
            self.status.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #2b8a3e;")
            self.detail.setText(
                f"보이는 메시지 {scanned}개 · 번역 표시 {translated}개")


def main():
    app = QApplication(sys.argv)
    # 오버레이는 투명한 도구 창이라 '마지막 창'으로 잡혀 앱이 꺼질 수 있다.
    # 종료는 조작 창(ControlWindow)에서만 하도록 자동 종료를 끈다.
    app.setQuitOnLastWindowClosed(False)

    overlay = OverlayWindow()
    overlay.show()

    control = ControlWindow(overlay)
    control.show()

    translator = TranslationManager()
    scanner = Scanner(translator)
    scanner.windowRectReady.connect(overlay.set_window_rect)
    scanner.blocksReady.connect(overlay.update_blocks)
    scanner.statusReady.connect(control.update_status)

    thread = threading.Thread(target=scanner.run, daemon=True)
    thread.start()

    _log("=" * 60)
    _log("오버레이 번역기 실행 중. 조작 창에서 상태 확인/종료 가능.")
    _log("=" * 60)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

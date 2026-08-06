"""
설정 외부화.

우선순위: 환경변수(DTL_*)  >  translator.ini  >  코드 기본값

- translator.ini 를 exe(또는 스크립트) 옆에 두면 값을 바꿀 수 있다. 없으면 기본값.
- 일회성으로 바꿀 땐 환경변수가 편하다. 예) DTL_TARGET_LANG=en python discord_screen_overlay.py

의존성 없음(stdlib만). PyQt6/pywinauto 없이 import된다.
"""

import configparser
import dataclasses
import os
import sys
from dataclasses import dataclass

CONFIG_FILENAME = "translator.ini"
CONFIG_SECTION = "translator"
ENV_PREFIX = "DTL_"


def app_dir() -> str:
    """실행 파일(frozen exe) 또는 스크립트가 있는 폴더. 설정/로그의 기준 경로."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


@dataclass
class Config:
    # 번역
    target_lang: str = "ko"           # 목표 언어 (ISO 코드: ko, en, ja ...)
    translation_provider: str = "google"  # google | gemini | proxy
    gemini_api_key: str = ""          # Google AI Studio 무료 키(카드 불필요)
    gemini_model: str = "gemini-flash-latest"  # 최신 flash 별칭(무료 티어)
    # proxy: 배포용. 서버(프록시)가 키를 쥐고 있어 exe엔 키가 안 들어간다.
    proxy_url: str = ""               # 예: https://xxx.workers.dev
    proxy_token: str = ""             # 남용 방지용 공유 토큰(키 아님, 재발급 가능)
    translate_workers: int = 4        # 동시 번역 워커 수 (많을수록 빠르나 레이트리밋↑)
    translate_max_retries: int = 3    # 번역 실패 시 재시도 횟수
    translate_retry_delay: float = 0.7  # 재시도 간격(초), 점증
    # 스캔
    scan_interval_sec: float = 0.35   # 화면 스캔 주기(초). 작을수록 반응 빠르나 CPU↑
    min_text_len: int = 2             # 이 글자 수 미만은 번역 안 함
    max_text_len: int = 2000          # 이 글자 수 초과는 번역 안 함
    # 오버레이
    font_size: int = 13               # 번역 박스 글자 크기(px)
    single_line_max_h: int = 40       # 원문 높이가 이하면 한 줄로 보고 가로로 늘림


def _coerce(current, raw: str):
    """raw(문자열)를 current 값의 타입으로 변환. 실패하면 current 유지."""
    try:
        if isinstance(current, bool):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return type(current)(raw)
    except (ValueError, TypeError):
        return current


def load_config(directory: str = None) -> Config:
    directory = directory or app_dir()
    cfg = Config()

    # inline_comment_prefixes: "key = ko  ; 주석" 에서 주석을 값에 포함하지 않도록.
    parser = configparser.ConfigParser(inline_comment_prefixes=(";",))
    path = os.path.join(directory, CONFIG_FILENAME)
    if os.path.exists(path):
        try:
            parser.read(path, encoding="utf-8")
        except Exception:
            pass

    for field in dataclasses.fields(cfg):
        current = getattr(cfg, field.name)
        raw = None
        if parser.has_option(CONFIG_SECTION, field.name):
            raw = parser.get(CONFIG_SECTION, field.name)
        env = os.environ.get(ENV_PREFIX + field.name.upper())
        if env is not None:                 # 환경변수가 최우선
            raw = env
        if raw is not None:
            setattr(cfg, field.name, _coerce(current, raw))

    return cfg

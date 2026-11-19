"""D-30: повтори відправки в Telegram без дублів. Без мережі: requests.post підмінено.

Залежності бота (feedparser, psycopg2, google-genai, cloudscraper, tenacity), якщо їх немає,
підмінено заглушками — тестуємо лише логіку відправки.
"""
import importlib
import os
import sys
import types

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _stub_missing_modules():
    def ensure(name, module):
        try:
            importlib.import_module(name)
        except ImportError:
            sys.modules[name] = module

    ensure('feedparser', types.SimpleNamespace(USER_AGENT=''))
    ensure('psycopg2', types.ModuleType('psycopg2'))
    ensure('cloudscraper', types.SimpleNamespace(create_scraper=lambda **k: None))
    ensure('dotenv', types.SimpleNamespace(load_dotenv=lambda *a, **k: None))

    try:
        importlib.import_module('google.genai')
    except ImportError:
        google = sys.modules.setdefault('google', types.ModuleType('google'))
        genai = types.ModuleType('google.genai')
        genai.Client = lambda **k: None
        class _AnyAttr(types.ModuleType):
            def __getattr__(self, name):
                return lambda *a, **k: None
        gtypes = _AnyAttr('google.genai.types')
        genai.types = gtypes
        google.genai = genai
        sys.modules['google.genai'] = genai
        sys.modules['google.genai.types'] = gtypes

    try:
        importlib.import_module('tenacity')
    except ImportError:
        t = types.ModuleType('tenacity')
        t.stop_after_attempt = lambda n: n
        t.wait_exponential = lambda **k: None
        t.retry_if_exception_type = lambda exc: (lambda e: isinstance(e, exc))
        t.retry_if_not_exception_type = lambda exc: (lambda e: not isinstance(e, exc))

        def retry(stop=1, wait=None, retry=None, reraise=True):
            def deco(fn):
                def wrapper(*a, **k):
                    for attempt in range(stop):
                        try:
                            return fn(*a, **k)
                        except Exception as e:
                            if attempt == stop - 1 or not retry(e):
                                raise
                return wrapper
            return deco
        t.retry = retry
        sys.modules['tenacity'] = t


_stub_missing_modules()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'TESTTOKEN')
os.environ.setdefault('TELEGRAM_CHANNEL_ID', '@test')
import main


class FakeResp:
    def __init__(self, status, text='{}'):
        self.status_code = status
        self.text = text

    def json(self):
        return {}


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(main.time, 'sleep', lambda s: None)
    monkeypatch.setattr(main, 'DRY_RUN', False)

    def install(responses):
        it = iter(responses)

        def fake_post(url, data=None, timeout=None):
            log.append(url.rsplit('/', 1)[-1])
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return r
        monkeypatch.setattr(main.requests, 'post', fake_post)
        return log
    return install


def test_502_photo_no_retry_no_text_fallback(calls):
    log = calls([FakeResp(502, 'Bad Gateway')])
    assert main.send_to_telegram('Текст', 'https://x', 'Src', ['https://img']) == main.SEND_UNCERTAIN
    assert log == ['sendPhoto']


def test_read_timeout_photo_no_retry(calls):
    log = calls([requests.ReadTimeout('Read timed out')])
    assert main.send_to_telegram('Текст', 'https://x', 'Src', ['https://img']) == main.SEND_UNCERTAIN
    assert log == ['sendPhoto']


def test_502_text_only_no_retry(calls):
    log = calls([FakeResp(504)])
    assert main.send_to_telegram('Текст', 'https://x', 'Src', []) == main.SEND_UNCERTAIN
    assert log == ['sendMessage']


def test_connection_aborted_is_uncertain(calls):
    log = calls([requests.ConnectionError("('Connection aborted.', RemoteDisconnected())")])
    assert main.send_to_telegram('Текст', 'https://x', 'Src', []) == main.SEND_UNCERTAIN
    assert log == ['sendMessage']


def test_no_connection_is_retried(calls):
    log = calls([requests.ConnectTimeout('connect timeout'),
                 requests.ConnectionError('Failed to establish a new connection'),
                 FakeResp(200)])
    assert main.send_to_telegram('Текст', 'https://x', 'Src', []) is True
    assert log == ['sendMessage'] * 3


def test_429_is_retried(calls):
    log = calls([FakeResp(429), FakeResp(200)])
    assert main.send_to_telegram('Текст', 'https://x', 'Src', []) is True
    assert log == ['sendMessage', 'sendMessage']


def test_bad_photo_4xx_falls_back_to_text(calls):
    log = calls([FakeResp(400, 'wrong file identifier'), FakeResp(200)])
    assert main.send_to_telegram('Текст', 'https://x', 'Src', ['https://img']) is True
    assert log == ['sendPhoto', 'sendMessage']


def test_text_fallback_502_is_uncertain(calls):
    log = calls([FakeResp(400, 'wrong file identifier'), FakeResp(502)])
    assert main.send_to_telegram('Текст', 'https://x', 'Src', ['https://img']) == main.SEND_UNCERTAIN
    assert log == ['sendPhoto', 'sendMessage']


def test_admin_message_502_not_retried(calls, monkeypatch):
    monkeypatch.setattr(main, 'ADMIN_CHAT_ID', '1')
    log = calls([FakeResp(502)])
    assert main.send_admin_html('звіт') is False
    assert log == ['sendMessage']


def test_report_shows_uncertain_and_deferred():
    stats = {
        'execution_time': 1, 'errors_count': 1, 'gemini_retries': 0, 'tokens_used': 0,
        'funnel': {'scraped_total': 0, 'already_seen': 0, 'rejected_by_keywords': 0, 'duplicates_removed_by_ai': 0},
        'backlog': {'total_size': 0, 'sources': {}},
        'published': {'total': 0, 'with_images': 0, 'text_only': 0, 'ukraine_priority': 0, 'sources': {}, 'uncertain': 1},
        'send_deferred': 2,
    }
    text = main.build_analytics_message(stats, None)
    assert 'не підтвердив відправку: 1' in text
    assert 'Ліміт часу запуску: 2' in text

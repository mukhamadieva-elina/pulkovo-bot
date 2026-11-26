"""Согласие в сообщении бота с необязательной ссылкой на полный документ."""

from urllib.parse import urlparse
import re
import unicodedata
from .dialogue_rules import without_opening_greeting, SESSION_INTRO


INTRODUCTION = SESSION_INTRO
DEFAULT_CONSENT_TEXT = (
    'Для ответа на ваше обращение будут обрабатываться ваши сообщения и фотографии, '
    'включая содержащиеся в них персональные данные. '
    'Переписка сохраняется и доступна сотрудникам поддержки.\n\n'
    'Не присылайте паспортные и банковские данные или коды бронирования. '
    'Отозвать согласие: /отозвать.'
)
CONSENT_INSTRUCTION = (
    'Если согласны с обработкой, нажмите «Согласен» или напишите «Согласен» / «Согласна».\n'
    'Можно также обратиться к сотруднику.'
)


def is_typed_consent(text):
    if len(text) > 50:
        return False
    value = unicodedata.normalize('NFKC', text).strip().casefold()
    return bool(re.fullmatch(r'соглас(?:ен|на)[.!\s]*', value))


def valid_consent_url(value):
    if not value:
        return True
    try:
        parsed = urlparse(value)
        return parsed.scheme == "https" and bool(parsed.hostname)
    except ValueError:
        return False


def consent_message(settings, *, include_intro=False):
    intro = settings.get('session_intro', INTRODUCTION).strip() if include_intro else ''
    text = without_opening_greeting(settings.get("consent_text", "").strip())
    url = settings.get("consent_url", "").strip()
    return ((intro + '\n\n' if intro else '') + text
            + ("\n\nПодробнее об условиях: " + url if url else "")
            + '\n\n' + CONSENT_INSTRUCTION)


def consent_configured(settings):
    return (
        settings.get("consent_ready") == "true"
        and bool(settings.get("consent_version", "").strip())
        and bool(settings.get("consent_text", "").strip())
        and valid_consent_url(settings.get("consent_url", "").strip())
        and len(consent_message(settings, include_intro=True)) <= 3500
    )

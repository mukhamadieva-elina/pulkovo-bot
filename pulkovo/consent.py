"""Согласие в сообщении бота с необязательной ссылкой на полный документ."""

from urllib.parse import urlparse
import re
import unicodedata
from .dialogue_rules import without_opening_greeting


INTRODUCTION = (
    '✈ Я бот Пулково.\n\n'
    'Помогу с вопросами о багаже, регистрации, парковке и услугах аэропорта. '
    'Можно написать вопрос или прислать фотографию: если информации будет мало, я уточню детали. '
    'Если потребуется помощь сотрудника, передам ему обращение.\n\n'
    'Также проверю рейс по онлайн-табло Пулково. Напишите номер с кодом авиакомпании '
    'и дату по расписанию в Пулково, например: SU 25 сегодня.'
)
CONSENT_INSTRUCTION = (
    'Нажмите «Согласен» или отправьте отдельным сообщением «Согласен» либо «Согласна». '
    'Такое сообщение также означает согласие с условиями выше.\n\n'
    'Для общения без ИИ выберите «Без ИИ — к сотруднику».'
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


def consent_message(settings):
    text = without_opening_greeting(settings.get("consent_text", "").strip())
    url = settings.get("consent_url", "").strip()
    return (INTRODUCTION + '\n\nУсловия обработки данных\n\n' + text
            + ("\n\nПодробнее об условиях: " + url if url else "")
            + '\n\n' + CONSENT_INSTRUCTION)


def consent_configured(settings):
    return (
        settings.get("consent_ready") == "true"
        and bool(settings.get("consent_version", "").strip())
        and bool(settings.get("consent_text", "").strip())
        and valid_consent_url(settings.get("consent_url", "").strip())
        and len(consent_message(settings)) <= 3500
    )

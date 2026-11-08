"""Согласие в сообщении бота с необязательной ссылкой на полный документ."""

from urllib.parse import urlparse


def valid_consent_url(value):
    if not value:
        return True
    try:
        parsed = urlparse(value)
        return parsed.scheme == "https" and bool(parsed.hostname)
    except ValueError:
        return False


def consent_message(settings):
    text = settings.get("consent_text", "").strip()
    url = settings.get("consent_url", "").strip()
    return text + ("\n\nПодробнее об условиях: " + url if url else "")


def consent_configured(settings):
    return (
        settings.get("consent_ready") == "true"
        and bool(settings.get("consent_version", "").strip())
        and bool(settings.get("consent_text", "").strip())
        and valid_consent_url(settings.get("consent_url", "").strip())
        and len(consent_message(settings)) <= 3500
    )

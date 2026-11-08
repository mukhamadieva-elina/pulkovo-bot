"""Проверка авторизации и списка моделей без отправки сообщений в ИИ."""

import base64
import binascii
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

import requests
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
MODELS_URL = "https://api.giga.chat/v1/models"
SCOPES = {"GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP"}


def main():
    load_dotenv(PROJECT_DIR / ".env")
    key = os.getenv("GIGACHAT_AUTH_TOKEN", "").strip()
    scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
    if not key:
        print("Заполните GIGACHAT_AUTH_TOKEN в .env.", file=sys.stderr)
        return 1
    try:
        if b":" not in base64.b64decode(key, validate=True):
            raise ValueError
    except (binascii.Error, ValueError):
        print("Нужен ключ авторизации GigaChat в формате Base64, без префикса Basic.", file=sys.stderr)
        return 1
    if scope not in SCOPES:
        print("Неизвестное значение GIGACHAT_SCOPE.", file=sys.stderr)
        return 1

    ca_file = os.getenv("GIGACHAT_CA_BUNDLE_FILE", "").strip()
    default_ca = PROJECT_DIR / ".certs" / "gigachat.pem"
    verify = True
    if ca_file or default_ca.is_file():
        ca_path = Path(ca_file).expanduser() if ca_file else default_ca
        if not ca_path.is_absolute():
            ca_path = PROJECT_DIR / ca_path
        if not ca_path.is_file():
            print("Не найден файл сертификатов GIGACHAT_CA_BUNDLE_FILE.", file=sys.stderr)
            return 1
        verify = str(ca_path)

    print(f"Проверка GigaChat, scope={scope}. Значения ключей скрыты.", flush=True)
    stage = "авторизация"
    try:
        with requests.Session() as session:
            response = session.post(
                AUTH_URL,
                headers={
                    "Authorization": f"Basic {key}",
                    "RqUID": str(uuid4()),
                    "Accept": "application/json",
                },
                data={"scope": scope},
                timeout=(10, 20),
                verify=verify,
                allow_redirects=False,
            )
            if response.status_code != 200:
                print(f"Авторизация: HTTP {response.status_code}. Проверьте ключ и тип аккаунта (scope).", file=sys.stderr)
                return 1
            payload = response.json()
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                print("В ответе авторизации нет токена доступа.", file=sys.stderr)
                return 1
            print("Авторизация успешна.", flush=True)

            stage = "получение списка моделей"
            response = session.get(
                MODELS_URL,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=(10, 20),
                verify=verify,
                allow_redirects=False,
            )
            if response.status_code != 200:
                print(f"Список моделей: HTTP {response.status_code}.", file=sys.stderr)
                return 1
            payload = response.json()
            models = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                print("Неожиданный формат списка моделей.", file=sys.stderr)
                return 1
            names = [item.get("id") for item in models if isinstance(item, dict)]
            names = [name for name in names if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,100}", name)]
            print("Доступные модели: " + (", ".join(names) or "список пуст"))
            print("Проверка завершена. Тексты и изображения в модель не отправлялись.")
            return 0
    except requests.exceptions.SSLError:
        print(f"Ошибка TLS ({stage}). Укажите доверенные сертификаты в GIGACHAT_CA_BUNDLE_FILE.", file=sys.stderr)
    except requests.exceptions.Timeout:
        print(f"Истекло время ожидания: {stage}.", file=sys.stderr)
    except requests.exceptions.RequestException:
        print(f"Ошибка сети: {stage}. Проверьте доступ к серверам GigaChat.", file=sys.stderr)
    except (ValueError, OSError):
        print(f"Не удалось прочитать ответ или файл сертификатов: {stage}.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

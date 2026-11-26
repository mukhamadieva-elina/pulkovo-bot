"""Короткие реплики, для которых не нужен внешний ИИ."""

import re
import unicodedata


SESSION_INTRO = 'Здравствуйте! ✈\nБот Пулково на связи, я постараюсь помочь с вашим вопросом!'
GREETING_PROMPT = (
    'Напишите вопрос или отправьте фотографию. '
    'Для поиска рейса: «SU 25 сегодня» или «Ближайший рейс в Красноярск».'
)
GREETING_TEXT = SESSION_INTRO + '\n\n' + GREETING_PROMPT
GREETING_IDLE_SECONDS = 24 * 60 * 60
THANKS_TEXT = 'Пожалуйста! 🙂'
FAREWELL_TEXT = 'До свидания! 👋'
FEEDBACK_THANKS_TEXT = 'Спасибо за оценку!\n\nЕсли появится новый вопрос, напишите сюда.'


def without_opening_greeting(text):
    """Remove only a standalone opening salutation; preserve the answer body."""
    result = re.sub(
        r'^\s*(?:здравствуй(?:те)?|привет|добрый\s+(?:день|вечер)|доброе\s+утро)'
        r'(?:[!.…]+\s*|\s*\n\s*)', '', text, count=1, flags=re.IGNORECASE)
    return result if result.strip() else text


def is_greeting(text):
    """Only a standalone greeting; never swallow a question after it."""
    if not text or len(text) > 160:
        return False
    value = unicodedata.normalize('NFKC', text).strip().casefold().replace('ё', 'е')
    if re.fullmatch(r'👋[\U0001f3fb-\U0001f3ff\ufe0f\s]*', value):
        return True
    value = re.sub(r'[.,!?…:;\-\s👋😊🙂☺\ufe0f\U0001f3fb-\U0001f3ff]+', ' ', value).strip()
    return bool(re.fullmatch(
        r'привет|приветик|приветствую(?: вас)?|здравствуй(?:те)?|здрасте|'
        r'доброе утро|добрый (?:день|вечер)|доброго (?:утра|дня|вечера|времени суток)|hello|hi', value))


def is_thanks(text):
    value=text.strip().casefold().replace('ё','е')
    if re.fullmatch(r'[🙏\ufe0f\s]+',value):
        return True
    value=re.sub(r'[.,!…:;\-\s🙏👍❤💙😊🙂☺\ufe0f🤝🙌👌🧡💚💛👏]+',' ',value).strip()
    return bool(re.fullmatch(
        r'(?:(?:ок|окей|хорошо|понятно|ясно) )?'
        r'(?:(?:(?:большое|огромное) )?спасибо(?: (?:большое|огромное|вам|тебе)){0,2}|благодарю(?: (?:вас|тебя))?|thanks|thank you)'
        r'(?: за (?:помощь|ответ|информацию|подсказку|разъяснение|пояснение))?'
        r'(?: (?:все понятно|теперь все понятно|все ясно|все получилось|разобрался|разобралась))?',value))


def is_farewell(text):
    """Match a complete farewell, not 'пока' inside an unfinished question."""
    if not text or len(text) > 160:
        return False
    value = unicodedata.normalize('NFKC', text).strip().casefold().replace('ё', 'е')
    value = re.sub(r'[.,!…:;\-\s👋🙏👍❤💙😊🙂☺\ufe0f🤝🙌👌🧡💚💛👏\U0001f3fb-\U0001f3ff]+', ' ', value).strip()
    farewell = r'(?:пока(?: пока)?|до свидания|до встречи|до скорого|всего (?:доброго|хорошего)|хорошего (?:дня|вечера)|доброй ночи|bye|goodbye|good bye)'
    thanks = r'(?:спасибо(?: (?:большое|вам)){0,2}|благодарю(?: вас)?)(?: за (?:помощь|ответ))?'
    return bool(re.fullmatch(rf'(?:(?:ну|все) )?(?:{thanks} )?{farewell}(?: (?:{farewell}|{thanks}))?', value))

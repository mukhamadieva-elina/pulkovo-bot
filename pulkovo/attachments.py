"""Единое представление вложений VK для обработки и переписки."""

from urllib.parse import urlparse


IMAGE_DOMAINS=('userapi.com','vkuserphoto.ru','vk-cdn.net','vk.com','vk.ru')
ATTACHMENT_LABELS={
    'photo':'Фотография','sticker':'Стикер','doc':'Документ',
    'audio_message':'Голосовое сообщение','audio':'Аудиозапись','video':'Видео',
    'link':'Ссылка','wall':'Запись на стене','gift':'Подарок','poll':'Опрос',
}


def safe_image_url(url):
    if not isinstance(url,str):
        return ''
    try:
        parsed=urlparse(url)
        host=(parsed.hostname or '').lower()
        if parsed.scheme=='https' and not parsed.username and not parsed.password and parsed.port in (None,443) and any(host==domain or host.endswith('.'+domain) for domain in IMAGE_DOMAINS):
            return url
    except ValueError:
        pass
    return ''


def normalize_attachments(items):
    attachments=[]
    for item in items[:20]:
        kind=item.get('type','attachment')
        attachment={'type':kind}
        data=item.get(kind,{})
        images=[]
        if kind=='photo':
            images=data.get('sizes',[])
        elif kind=='sticker':
            images=data.get('images') or data.get('images_with_background',[])
            if isinstance(data.get('sticker_id'),int):
                attachment['sticker_id']=data['sticker_id']
        images=[image for image in images if safe_image_url(image.get('url'))]
        if images:
            # Для стикера достаточно компактного статического изображения.
            suitable=[image for image in images if image.get('width',0)>=256] if kind=='sticker' else []
            image=min(suitable,key=lambda p:p.get('width',0)*p.get('height',0)) if suitable else max(images,key=lambda p:p.get('width',0)*p.get('height',0))
            attachment['url']=image['url']
        attachments.append(attachment)
    return attachments

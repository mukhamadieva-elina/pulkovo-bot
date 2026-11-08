const historyPanel = document.getElementById('dialogue-history');
const latestButton = document.getElementById('latest-messages');
const scrollStateKey = 'dialogue-scroll:' + window.location.pathname + window.location.search;
let loadingDialogue = false;
let followLatest = true;
let unseenMessages = false;

function nearLatest() {
  return historyPanel.scrollHeight - historyPanel.scrollTop - historyPanel.clientHeight < 48;
}

function lastMessageId() {
  return historyPanel.querySelector('[data-message-id]:last-child')?.dataset.messageId || '';
}

function updateNavigation() {
  if (followLatest) unseenMessages = false;
  latestButton.hidden = followLatest;
  latestButton.textContent = unseenMessages ? 'Новые сообщения ↓' : 'К последним сообщениям ↓';
}

function showLatest() {
  followLatest = true;
  historyPanel.scrollTop = historyPanel.scrollHeight;
  updateNavigation();
}

function historyPosition() {
  const top = historyPanel.getBoundingClientRect().top;
  const anchor = [...historyPanel.querySelectorAll('[data-message-id]')].find(el => el.getBoundingClientRect().bottom > top);
  return {bottom:nearLatest(),top:historyPanel.scrollTop,anchor:anchor?.dataset.messageId,offset:anchor ? anchor.getBoundingClientRect().top - top : 0,last:lastMessageId()};
}

function restoreHistory(position) {
  followLatest = position.bottom;
  if (followLatest) {
    showLatest();
    return;
  }
  const anchor = [...historyPanel.querySelectorAll('[data-message-id]')].find(el => el.dataset.messageId === position.anchor);
  if (anchor) {
    historyPanel.scrollTop += anchor.getBoundingClientRect().top - historyPanel.getBoundingClientRect().top - position.offset;
  } else {
    historyPanel.scrollTop = position.top;
  }
  unseenMessages = position.last !== lastMessageId();
  updateNavigation();
}

if (historyPanel && latestButton) {
  let saved;
  try {
    saved = JSON.parse(sessionStorage.getItem(scrollStateKey) || 'null');
    sessionStorage.removeItem(scrollStateKey);
  } catch (_) {}
  if (saved && Date.now() - saved.time < 15000) {
    restoreHistory(saved.history);
    window.scrollTo(saved.x, saved.y);
  } else {
    showLatest();
    (document.getElementById('reply-form') || historyPanel).scrollIntoView({block:'nearest'});
  }
  historyPanel.addEventListener('scroll', () => {
    followLatest = nearLatest();
    updateNavigation();
  });
  historyPanel.addEventListener('load', () => { if (followLatest) showLatest(); }, true);
  new ResizeObserver(() => { if (followLatest) showLatest(); }).observe(historyPanel);
  latestButton.addEventListener('click', showLatest);
}

async function refreshDialogue() {
  if (!historyPanel || loadingDialogue || document.visibilityState !== 'visible') return;
  loadingDialogue = true;
  try {
    const updatesUrl = new URL(historyPanel.dataset.updates, window.location.href);
    updatesUrl.searchParams.set('revision', historyPanel.dataset.revision);
    const response = await fetch(updatesUrl, {cache:'no-store'});
    if (!response.ok || response.redirected) return;
    const update = await response.json();
    if (update.revision === historyPanel.dataset.revision || !update.html) return;
    // Не уничтожаем выделенный текст во время чтения или копирования истории.
    if (window.getSelection()?.isCollapsed === false) return;
    const position = historyPosition();
    const dirty = [...document.querySelectorAll('textarea, input:not([type=hidden])')].some(el => el.value !== el.defaultValue);
    const editing = document.activeElement?.matches('textarea, input:not([type=hidden]), select');
    if (!dirty && !editing) {
      try {
        // Обновляем также карточку обращения, сохраняя место чтения переписки.
        sessionStorage.setItem(scrollStateKey, JSON.stringify({time:Date.now(),history:position,x:window.scrollX,y:window.scrollY}));
        window.location.reload();
        return;
      } catch (_) {
        // При недоступном sessionStorage обновляем историю без перезагрузки.
      }
    }
    // Ответ и внутренняя заметка остаются в своих полях при обновлении истории.
    historyPanel.innerHTML = update.html;
    historyPanel.dataset.revision = update.revision;
    restoreHistory(position);
    document.getElementById('dialogue-changed').hidden = false;
    document.querySelectorAll('[data-active-status]').forEach(el => { el.textContent = update.status; });
  } catch (_) {
    // Сохраняем введённый ответ при временной недоступности сервера.
  } finally { loadingDialogue = false; }
}
setInterval(refreshDialogue, 4000);
document.addEventListener('visibilitychange', refreshDialogue);

(() => {
  const modal = document.getElementById('dialogue-window');
  const content = document.getElementById('dialogue-window-content');
  const warning = document.getElementById('dialogue-close-warning');
  const dock = document.getElementById('dialogue-dock');
  const controllers = new Set();
  let opening = 0;
  let opener;
  let activePath = '';
  let minimized = false;
  let minimizedPosition;
  let pendingConversation;
  let unseenUpdate = false;

  const windowUrl = path => {
    const url = new URL(path, location.href);
    url.searchParams.set('window', '1');
    return url;
  };
  async function fragment(response) {
    if (!response.ok || response.headers.get('X-Dialogue-Fragment') !== '1') {
      throw new Error(response.redirected && new URL(response.url).pathname === '/login'
        ? 'Сессия завершилась. Войдите в админку в другой вкладке, затем повторите действие. Текст сохранен в этом окне.'
        : 'Не удалось обновить диалог. Проверьте соединение и повторите. Введенный текст сохранен.');
    }
    return response.text();
  }

  class Dialogue {
    constructor(root, position) {
      this.root = root;
      this.history = root.querySelector('#dialogue-history');
      this.latest = root.querySelector('#latest-messages');
      this.follow = true;
      this.busy = false;
      this.alive = true;
      this.history.addEventListener('scroll', () => { this.follow = this.nearBottom(); this.navigation(); });
      this.history.addEventListener('load', () => { if (this.follow) this.bottom(); }, true);
      this.latest.addEventListener('click', () => this.bottom());
      this.observer = new ResizeObserver(() => { if (this.follow) this.bottom(); });
      this.observer.observe(this.history);
      root.addEventListener('submit', event => this.submit(event));
      root.querySelector('[data-refresh-dialogue]').addEventListener('click', () => this.reload(true));
      this.timer = setInterval(() => this.refresh(), 4000);
      controllers.add(this);
      if (position) this.restore(position); else this.bottom();
      if (modal?.contains(root)) {
        modal.querySelector('[data-minimize-dialogue]').disabled = false;
        updateDock();
      }
    }
    nearBottom() { return this.history.scrollHeight-this.history.scrollTop-this.history.clientHeight < 48; }
    navigation() { this.latest.hidden = this.follow; }
    bottom() { if (modal?.contains(this.root) && minimized) return; this.follow = true; this.history.scrollTop = this.history.scrollHeight; this.navigation(); }
    position() {
      if (modal?.contains(this.root) && minimized && minimizedPosition) return minimizedPosition;
      const top = this.history.getBoundingClientRect().top;
      const anchor = [...this.history.querySelectorAll('[data-message-id]')].find(el => el.getBoundingClientRect().bottom > top);
      return {bottom:this.nearBottom(),top:this.history.scrollTop,id:anchor?.dataset.messageId,offset:anchor ? anchor.getBoundingClientRect().top-top : 0};
    }
    restore(position) {
      if (position.bottom) return this.bottom();
      this.follow = false;
      const anchor = [...this.history.querySelectorAll('[data-message-id]')].find(el => el.dataset.messageId === position.id);
      this.history.scrollTop = position.top;
      if (anchor) this.history.scrollTop += anchor.getBoundingClientRect().top-this.history.getBoundingClientRect().top-position.offset;
      this.navigation();
    }
    drafts(exclude) {
      const drafts = [];
      for (const form of this.root.querySelectorAll('form')) {
        if (form === exclude) continue;
        const action = form.elements.action?.value;
        for (const field of form.querySelectorAll('textarea, input:not([type=hidden])')) {
          if (field.value !== field.defaultValue || (field.name === 'reply' && field.value)) {
            drafts.push({action,name:field.name,value:field.value});
          }
        }
      }
      return drafts;
    }
    dirty() { return this.drafts().length > 0; }
    error(message) { const el = this.root.querySelector('[data-dialogue-error]'); el.textContent = message; el.hidden = false; }
    dispose() { this.alive = false; clearInterval(this.timer); this.observer.disconnect(); controllers.delete(this); }
    replace(html, drafts = [], position = this.position()) {
      if (!this.alive) return;
      const template = document.createElement('template');
      template.innerHTML = html;
      const next = template.content.querySelector('[data-dialogue-root]');
      if (!next) throw new Error('Не удалось получить диалог. Попробуйте обновить окно.');
      const opened = [...this.root.querySelectorAll('details[open]')].map(el => el.querySelector('summary')?.textContent);
      for (const draft of drafts) {
        const form = [...next.querySelectorAll('form')].find(f => f.elements.action?.value === draft.action);
        const field = form?.elements.namedItem(draft.name);
        if (field && field.type !== 'hidden') field.value = draft.value;
      }
      for (const details of next.querySelectorAll('details')) {
        if (opened.includes(details.querySelector('summary')?.textContent)) details.open = true;
      }
      this.dispose();
      this.root.replaceWith(next);
      return new Dialogue(next, position);
    }
    async reload(preserve = false) {
      if (this.busy || !this.alive) return;
      this.busy = true;
      try {
        const html = await fragment(await fetch(windowUrl(this.root.dataset.url), {cache:'no-store'}));
        this.replace(html, preserve ? this.drafts() : []);
      } catch (error) { if (this.alive) this.error(error.message); }
      finally { this.busy = false; }
    }
    async submit(event) {
      event.preventDefault();
      if (this.busy) return;
      const form = event.target;
      const data = new FormData(form);
      if (event.submitter?.name) data.set(event.submitter.name, event.submitter.value);
      this.busy = true;
      this.submitting = true;
      const buttons = [...this.root.querySelectorAll('button')].map(button => [button,button.disabled]);
      buttons.forEach(([button]) => { button.disabled = true; });
      try {
        const html = await fragment(await fetch(windowUrl(this.root.dataset.url), {method:'POST',body:data}));
        this.replace(html, this.drafts(form));
      } catch (error) { this.error(error.message); }
      finally { this.busy = false; this.submitting = false; buttons.forEach(([button,disabled]) => { button.disabled = disabled; }); }
    }
    async refresh() {
      if (!this.alive || this.busy || document.visibilityState !== 'visible' || (modal?.open && !modal.contains(this.root))) return;
      this.busy = true;
      try {
        const url = new URL(this.history.dataset.updates, location.href);
        url.searchParams.set('revision', this.history.dataset.revision);
        const response = await fetch(url, {cache:'no-store'});
        if (!response.ok || response.redirected || !this.alive) return;
        const update = await response.json();
        if (update.revision === this.history.dataset.revision || !update.html || window.getSelection()?.isCollapsed === false) return;
        if (modal?.contains(this.root) && minimized) {
          // Keep the DOM, draft, and reading position untouched while hidden.
          unseenUpdate = true;
          updateDock();
          return;
        }
        const position = this.position();
        const editing = this.root.contains(document.activeElement) && document.activeElement.matches('textarea,input,select,button');
        if (!this.dirty() && !editing) {
          const html = await fragment(await fetch(windowUrl(this.root.dataset.url), {cache:'no-store'}));
          // The user may start typing while the fragment is being fetched.
          this.replace(html, this.drafts(), position);
          return;
        }
        this.history.innerHTML = update.html;
        this.history.dataset.revision = update.revision;
        this.restore(position);
        this.root.querySelector('#dialogue-changed').hidden = false;
        this.root.querySelectorAll('[data-active-status]').forEach(el => { el.textContent = update.status; });
        this.root.querySelectorAll('[data-mode-action]').forEach(el => { el.disabled = true; });
      } catch (_) { /* Keep drafts and retry after a transient failure. */ }
      finally { this.busy = false; }
    }
  }

  const modalController = () => [...controllers].find(c => modal.contains(c.root));
  function updateDock() {
    if (!dock) return;
    const current = modalController();
    const name = current?.root.querySelector('.dialogue-heading h2')?.textContent || 'Чат с пассажиром';
    const detail = current?.dirty() ? 'Черновик сохранен в этом окне' : 'Нажмите, чтобы открыть';
    dock.querySelector('[data-dock-name]').textContent = name;
    dock.querySelector('[data-dock-detail]').textContent = detail;
    dock.querySelector('[data-dock-update]').hidden = !unseenUpdate;
    dock.setAttribute('aria-label', 'Открыть чат: ' + name + '. ' + detail);
  }
  function restoreWindow() {
    if (!activePath) return;
    minimized = false;
    dock.hidden = true;
    if (!modal.open) modal.showModal();
    document.body.classList.add('dialogue-window-open');
    const current = modalController();
    if (minimizedPosition) current?.restore(minimizedPosition);
    minimizedPosition = null;
    unseenUpdate = false;
    current?.refresh();
  }
  function minimize() {
    const current = modalController();
    if (!current) return;
    if (current.busy) { current.error('Дождитесь завершения обновления или отправки.'); return; }
    minimizedPosition = current.position();
    minimized = true;
    pendingConversation = null;
    warning.hidden = true;
    modal.close();
    document.body.classList.remove('dialogue-window-open');
    updateDock();
    dock.hidden = false;
    dock.focus();
  }
  function close(force = false) {
    const current = modalController();
    if (current?.busy) { current.error('Дождитесь завершения обновления или отправки.'); return; }
    if (!force && current?.dirty()) {
      restoreWindow();
      warning.querySelector('p').textContent = 'В диалоге есть несохраненный текст. Можно свернуть окно и вернуться к нему позже или закрыть без сохранения.';
      warning.hidden = false; warning.querySelector('button').focus(); return;
    }
    const next = pendingConversation;
    pendingConversation = null;
    opening++;
    current?.dispose();
    modal.close();
    modal.dataset.active = 'false';
    activePath = '';
    minimized = false;
    minimizedPosition = null;
    unseenUpdate = false;
    dock.hidden = true;
    modal.querySelector('[data-minimize-dialogue]').disabled = true;
    content.replaceChildren();
    warning.hidden = true;
    document.body.classList.remove('dialogue-window-open');
    opener?.focus();
    if (next) openConversation(next.url, next.link);
    else document.dispatchEvent(new Event('dialogueclosed'));
  }
  async function openConversation(url, link) {
    if (activePath === url.pathname) { restoreWindow(); return; }
    const current = modalController();
    if (current?.busy) { restoreWindow(); current.error('Дождитесь завершения обновления или отправки, затем откройте другой диалог.'); return; }
    if (current?.dirty()) {
      pendingConversation = {url,link};
      restoreWindow();
      warning.querySelector('p').textContent = 'В открытом чате есть несохраненный текст. Закрыть его без сохранения и перейти к другому пассажиру?';
      warning.hidden = false;
      warning.querySelector('button').focus();
      return;
    }
    current?.dispose();
    const request = ++opening;
    activePath = url.pathname;
    minimized = false;
    minimizedPosition = null;
    unseenUpdate = false;
    modal.dataset.active = 'true';
    modal.querySelector('[data-minimize-dialogue]').disabled = true;
    dock.hidden = true;
    warning.hidden = true;
    opener = link;
    content.textContent = 'Загружаем переписку…';
    if (!modal.open) modal.showModal();
    document.body.classList.add('dialogue-window-open');
    try {
      const html = await fragment(await fetch(windowUrl(url), {cache:'no-store'}));
      if (opening !== request || !modal.open) return;
      content.innerHTML = html;
      new Dialogue(content.querySelector('[data-dialogue-root]'));
    } catch (error) {
      if (opening !== request || !modal.open) return;
      content.textContent = error.message + ' ';
      const retry = document.createElement('a'); retry.href = url.href; retry.textContent = 'Открыть отдельной страницей';
      // Do not intercept the fallback link a second time.
      retry.target = '_blank'; retry.rel = 'noopener'; content.append(retry);
    }
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link || !modal || link.target === '_blank' || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || !/^\/dialogue\/\d+$/.test(url.pathname) || url.searchParams.has('ticket')) return;
    event.preventDefault();
    openConversation(url, link);
  });
  dock?.addEventListener('click', restoreWindow);
  modal?.querySelector('[data-minimize-dialogue]').addEventListener('click', minimize);
  modal?.querySelector('[data-close-dialogue]').addEventListener('click', () => close());
  modal?.querySelector('[data-discard-dialogue]').addEventListener('click', () => close(true));
  modal?.querySelector('[data-keep-dialogue]').addEventListener('click', () => { warning.hidden = true; pendingConversation = null; modalController()?.root.querySelector('[name=reply]').focus(); });
  modal?.addEventListener('cancel', event => { event.preventDefault(); close(); });
  document.querySelectorAll('[data-dialogue-root]').forEach(root => new Dialogue(root));
  document.addEventListener('visibilitychange', () => controllers.forEach(c => c.refresh()));
  window.addEventListener('beforeunload', event => {
    if ([...controllers].some(c => c.dirty() || c.submitting)) { event.preventDefault(); event.returnValue = ''; }
  });
})();

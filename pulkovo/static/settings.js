(() => {
  const form = document.querySelector('[data-settings-form]');
  if (!form) return;
  const output = form.querySelector('[data-consent-preview]');
  const intro = form.querySelector('[data-session-intro-preview]');
  const status = form.querySelector('[data-consent-preview-status]');
  const fields = ['session_intro', 'consent_text', 'consent_url'];
  let timer;
  let active;
  let revision = 0;

  function notice(text) {
    status.textContent = text;
    status.hidden = !text;
  }

  async function update(version) {
    const controller = new AbortController();
    active = controller;
    const data = new FormData();
    for (const key of ['csrf', ...fields]) data.set(key, form.elements.namedItem(key).value);
    try {
      const response = await fetch(form.dataset.consentPreviewUrl, {
        method: 'POST', body: data, signal: controller.signal, cache: 'no-store'
      });
      if (!response.ok || response.redirected) throw new Error('preview unavailable');
      const result = await response.json();
      if (version !== revision) return;
      output.textContent = result.text;
      intro.textContent = result.session_intro;
      notice(result.warning || '');
    } catch (error) {
      if (error.name !== 'AbortError' && version === revision) {
        notice('Не удалось обновить пример. Проверьте соединение и измените текст, чтобы повторить.');
      }
    }
  }

  function schedule() {
    const version = ++revision;
    clearTimeout(timer);
    active?.abort();
    notice('Обновляем пример…');
    timer = setTimeout(() => update(version), 200);
  }

  for (const key of fields) form.elements.namedItem(key).addEventListener('input', schedule);
  if (!output.textContent.trim()) schedule();
})();

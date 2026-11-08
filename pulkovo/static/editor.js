(() => {
  const answer = document.getElementById('answer-text');
  const editor = document.getElementById('answer-editor');
  const choice = document.getElementById('resource-choice');
  const insert = document.getElementById('insert-resource');
  if (!answer || !editor) return;
  const form = document.getElementById('answer-form');
  const resources = new Map([...choice?.options || []].filter(o => o.value).map(o => [o.value, {key:o.value,value:o.dataset.value,label:o.dataset.label}]));
  let bookmark;

  function fieldNode(resource) {
    const field = document.createElement('span');
    field.className = 'answer-field';
    field.contentEditable = 'false';
    field.dataset.resourceKey = resource.key;
    field.tabIndex = 0;
    field.title = resource.label;
    field.setAttribute('aria-label', resource.label + ': ' + resource.value);
    field.textContent = resource.value;
    return field;
  }

  function serialize(root, visible = false) {
    let text = '';
    for (const node of root.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) {
        text += node.textContent.replace(/\u00a0/g, ' ');
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        const resource = node.matches('.answer-field') && resources.get(node.dataset.resourceKey);
        if (resource) text += visible ? resource.value : '{' + resource.key + '}';
        else if (node.tagName === 'BR') text += '\n';
        else {
          if (/^(DIV|P|LI)$/.test(node.tagName) && text && !text.endsWith('\n')) text += '\n';
          text += serialize(node, visible);
        }
      }
    }
    return text;
  }

  function sync() {
    answer.value = serialize(editor);
  }

  function rememberSelection() {
    const selection = window.getSelection();
    if (!selection.rangeCount) return;
    const range = selection.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)) return;
    bookmark = range.cloneRange();
    const element = range.startContainer.nodeType === Node.ELEMENT_NODE ? range.startContainer : range.startContainer.parentElement;
    const field = element.closest('.answer-field');
    if (field && editor.contains(field)) {
      bookmark.setStartAfter(field);
      bookmark.collapse(true);
    }
  }

  function restoreSelection() {
    editor.focus({preventScroll:true});
    const selection = window.getSelection();
    if (!bookmark || !editor.contains(bookmark.commonAncestorContainer)) {
      bookmark = document.createRange();
      bookmark.selectNodeContents(editor);
      bookmark.collapse(false);
    }
    selection.removeAllRanges();
    selection.addRange(bookmark);
  }

  function removeField(field) {
    if (!editor.contains(field)) return;
    bookmark = document.createRange();
    bookmark.selectNode(field);
    restoreSelection();
    document.execCommand('delete');
    sync();
  }

  function templateHTML(template) {
    const wrapper = document.createElement('div');
    let start = 0;
    for (const match of template.matchAll(/\{([a-z][a-z0-9_]*)\}/g)) {
      const resource = resources.get(match[1]);
      if (!resource) continue;
      wrapper.append(document.createTextNode(template.slice(start, match.index)), fieldNode(resource));
      start = match.index + match[0].length;
    }
    wrapper.append(document.createTextNode(template.slice(start)));
    return wrapper.innerHTML;
  }

  // Скрытое поле отправляет шаблон со стабильными ссылками на справочник.
  // В тексте видны текущие значения, названия доступны при наведении.
  answer.hidden = true;
  editor.hidden = false;
  document.getElementById('answer-label').removeAttribute('for');
  document.getElementById('answer-hint').hidden = false;
  sync();
  document.addEventListener('selectionchange', rememberSelection);
  editor.addEventListener('input', sync);
  editor.addEventListener('keydown', event => {
    if (!event.target.matches('.answer-field')) return;
    if (event.key === 'Backspace' || event.key === 'Delete') {
      event.preventDefault(); removeField(event.target);
    }
  });
  editor.addEventListener('beforeinput', event => {
    if (event.inputType.startsWith('format')) event.preventDefault();
  });
  editor.addEventListener('paste', event => {
    event.preventDefault();
    let template;
    try {
      const data = JSON.parse(event.clipboardData.getData('application/x-pulkovo-answer'));
      if (data.origin === window.location.origin && typeof data.text === 'string') template = data.text;
    } catch (_) {}
    // Только собственные вставки или обычный текст, без HTML из буфера.
    if (template !== undefined) document.execCommand('insertHTML', false, templateHTML(template));
    else document.execCommand('insertText', false, event.clipboardData.getData('text/plain'));
    sync();
  });
  for (const type of ['copy','cut']) editor.addEventListener(type, event => {
    const selection = window.getSelection();
    if (!selection.rangeCount || selection.isCollapsed) return;
    const range = selection.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)) return;
    const fragment = range.cloneContents();
    event.preventDefault();
    event.clipboardData.setData('text/plain', serialize(fragment, true));
    event.clipboardData.setData('application/x-pulkovo-answer', JSON.stringify({origin:window.location.origin,text:serialize(fragment)}));
    if (type === 'cut') { document.execCommand('delete'); sync(); }
  });
  editor.addEventListener('drop', event => event.preventDefault());
  form.addEventListener('submit', sync);

  choice?.addEventListener('change', () => {
    const resource = resources.get(choice.value);
    document.getElementById('resource-preview').textContent = resource?.value || '';
    insert.disabled = !resource;
  });
  insert?.addEventListener('click', () => {
    const resource = resources.get(choice.value);
    if (!resource) return;
    restoreSelection();
    // insertHTML сохраняет вставку в стандартной истории отмены редактора.
    document.execCommand('insertHTML', false, fieldNode(resource).outerHTML + '&nbsp;');
    rememberSelection();
    sync();
  });
})();

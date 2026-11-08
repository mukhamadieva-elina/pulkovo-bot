// Обновляем только очередь и только при изменении её данных.
const queue = document.getElementById('queue-updates');
let checkingQueue = false;
async function checkQueue() {
  if (!queue || checkingQueue || document.visibilityState !== 'visible') return;
  checkingQueue = true;
  try {
    const response = await fetch('/queue/state?state=' + encodeURIComponent(queue.dataset.state), {cache: 'no-store'});
    if (!response.ok || response.redirected) return;
    const state = await response.json();
    if (state.revision !== queue.dataset.revision && window.getSelection()?.isCollapsed !== false) {
      window.location.reload();
    }
  } catch (_) {
    // При временном обрыве связи сохраняем список и повторяем проверку позже.
  } finally {
    checkingQueue = false;
  }
}
setInterval(checkQueue, 5000);
document.addEventListener('visibilitychange', checkQueue);

// Общие помощники для кнопок и опроса фоновых задач

function setBusy(btn) {
  btn.dataset.label = btn.innerHTML;
  btn.innerHTML = '<span class="spin">⏳</span> Выполняется…';
  btn.disabled = true;
}

function resetBtn(btn) {
  if (btn && btn.dataset.label) {
    btn.innerHTML = btn.dataset.label;
    btn.disabled = false;
  }
}

// Опрашивает /job/{id} до завершения; onSuccess() вызывается при done,
// при ошибке показывает текст в элементе statusEl.
function pollJob(jobId, statusEl, onSuccess) {
  const timer = setInterval(async () => {
    try {
      const r = await fetch('/job/' + jobId);
      const j = await r.json();
      if (j.status === 'running') return;
      clearInterval(timer);
      if (j.status === 'done') {
        if (onSuccess) onSuccess(j.result);
        else if (statusEl) statusEl.textContent = '✅ Готово.';
      } else {
        if (statusEl) statusEl.textContent = '❌ ' + (j.error || 'Задача не найдена');
        document.querySelectorAll('button.btn').forEach(b => resetBtn(b));
      }
    } catch (e) {
      clearInterval(timer);
      if (statusEl) statusEl.textContent = '❌ Ошибка связи: ' + e;
      document.querySelectorAll('button.btn').forEach(b => resetBtn(b));
    }
  }, 1500);
}

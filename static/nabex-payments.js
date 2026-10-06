/* Shared checkout chooser. Availability is enforced again by the backend. */
(function () {
  'use strict';
  let selectionOpen = false;

  async function choose(loadMethods) {
    let response;
    try {
      response = await loadMethods();
    } catch (error) {
      // apiFetch throws on non-2xx; direct fetch returns the same HTTP status.
      if ([401, 403, 404].includes(Number(error && error.status))) return '';
      throw error;
    }
    // Supports frontend-first rollout and the existing sign-in redirect.
    if ([401, 403, 404].includes(response.status)) return '';
    if (!response.ok) throw new Error('Не удалось загрузить способы оплаты. Попробуйте ещё раз.');
    const data = await response.json();
    if (!data || !data.ok || !Array.isArray(data.methods)) {
      throw new Error('Не удалось загрузить способы оплаты.');
    }
    const methods = data.methods.filter(item => item && ['legacy', 'new'].includes(item.id));
    if (!methods.length) return ''; // Existing Telegram Stars fallback.
    if (methods.length === 1) return methods[0].id;
    if (selectionOpen) throw new Error('Сначала завершите выбор способа оплаты.');
    selectionOpen = true;

    return new Promise(resolve => {
      const previousFocus = document.activeElement;
      const dialog = document.createElement('dialog');
      dialog.setAttribute('aria-label', 'Способ оплаты');
      dialog.style.cssText = 'width:min(380px,calc(100vw - 40px));box-sizing:border-box;padding:24px;border:1px solid #475569;border-radius:20px;background:#111827;color:#f8fafc;box-shadow:0 20px 90px #0009;font:16px/1.5 system-ui,sans-serif;z-index:2147483647;';
      const title = document.createElement('h2');
      title.textContent = 'Способ оплаты';
      title.style.cssText = 'margin:0 0 10px;font-size:22px;color:inherit;';
      const hint = document.createElement('p');
      hint.textContent = 'Выберите, как перейти к оплате';
      hint.style.cssText = 'margin:0 0 18px;color:#cbd5e1;font-size:14px;';
      dialog.append(title, hint);

      let done = false;
      function finish(value) {
        if (done) return;
        done = true;
        if (typeof dialog.close === 'function' && dialog.open) dialog.close();
        dialog.remove();
        selectionOpen = false;
        if (previousFocus && typeof previousFocus.focus === 'function') previousFocus.focus();
        resolve(value);
      }
      for (const method of methods) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = method.label;
        button.style.cssText = 'display:block;width:100%;margin:10px 0;padding:14px 12px;border:1px solid #64748b;border-radius:12px;background:#1e293b;color:#fff;font:inherit;cursor:pointer;';
        if (method.id === data.default) button.style.borderColor = '#818cf8';
        button.addEventListener('click', () => finish(method.id));
        dialog.append(button);
      }
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.textContent = 'Отмена';
      cancel.style.cssText = 'display:block;width:100%;padding:10px;margin-top:8px;border:0;background:transparent;color:#cbd5e1;font:inherit;cursor:pointer;';
      cancel.addEventListener('click', () => finish(null));
      dialog.append(cancel);
      dialog.addEventListener('cancel', event => { event.preventDefault(); finish(null); });
      document.body.append(dialog);
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else {
        dialog.setAttribute('open', '');
        dialog.style.position = 'fixed';
        dialog.style.top = '20vh';
        dialog.style.left = '50%';
        dialog.style.margin = '0';
        dialog.style.transform = 'translateX(-50%)';
      }
      const first = dialog.querySelector('button');
      if (first) first.focus();
    });
  }

  window.NabexPayments = Object.freeze({ choose });
})();

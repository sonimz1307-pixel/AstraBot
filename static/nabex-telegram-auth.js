/* Telegram cabinet launch compatibility. Server signature validation remains mandatory. */
(function () {
  'use strict';
  function ensureSession(tg) {
    // Only a server can validate this value. Here we detect the documented
    // reply-keyboard launch with no initData and ask the bot for an inline
    // launch. Never promote a query-string uid to authenticated identity.
    if (tg && typeof tg.initData === 'string' && tg.initData.trim()) return true;
    const platform = String(tg && tg.platform || '').toLowerCase();
    if (platform && platform !== 'unknown' && typeof tg.sendData === 'function') {
      try {
        tg.sendData(JSON.stringify({ action: 'open_account' }));
        return false;
      } catch (_) {}
    }
    const message = 'Для оплаты отправьте /cabinet в личный чат с ботом и нажмите «Открыть кабинет».';
    if (tg && typeof tg.showAlert === 'function') tg.showAlert(message);
    else if (typeof window.alert === 'function') window.alert(message);
    return false;
  }

  window.NabexTelegramAuth = Object.freeze({ ensureSession });
})();

// ==UserScript==
// @name         Fable印・クッキー自動化 v2
// @namespace    gamehub
// @version      2.0
// @description  自動クリック+回収期間ベース購入+Game Hubへの進捗報告/セーブ退避
// @match        https://orteil.dashnet.org/cookieclicker/*
// @grant        GM_xmlhttpRequest
// @connect      lostworldproject
// @updateURL    https://raw.githubusercontent.com/Kanako-12/cookie-automation/main/cookie.user.js
// @downloadURL  https://raw.githubusercontent.com/Kanako-12/cookie-automation/main/cookie.user.js
// ==/UserScript==

(function () {
  'use strict';

  const HUB = 'http://lostworldproject:8090/report/cookieclicker';

  function post(payload) {
    GM_xmlhttpRequest({
      method: 'POST',
      url: HUB,
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify(payload),
      onerror: () => console.warn('[gamehub] report failed'),
    });
  }

  // --- 100ms: クリック系 ---
  setInterval(() => {
    if (typeof Game === 'undefined' || !Game.ready) return;
    Game.ClickCookie();
    // ゴールデンのみ回収、ラースは温存(黙示録は観測対象)
    Game.shimmers
      .filter(s => s.type === 'golden' && !s.wrath)
      .forEach(s => s.pop());
  }, 100);

  // --- 1s: 購入系(回収期間ベースの貪欲法) ---
  setInterval(() => {
    if (typeof Game === 'undefined' || !Game.ready) return;
    // tech(黙示録研究)とtoggle(シーズン切替等)は自動購入しない
    Game.UpgradesInStore
      .filter(u => u.canBuy() && u.pool !== 'tech' && u.pool !== 'toggle')
      .forEach(u => u.buy(1));
    const best = Object.values(Game.Objects)
      .filter(o => o.price <= Game.cookies)
      .map(o => ({ o, pp: o.price / Math.max(o.storedCps * Game.globalCpsMult, 1e-9) }))
      .sort((a, b) => a.pp - b.pp)[0];
    if (best) best.o.buy(1);
  }, 1000);

  // --- 60s: ハブへ進捗報告 ---
  setInterval(() => {
    if (typeof Game === 'undefined' || !Game.ready) return;
    post({
      cookies: Math.round(Game.cookies),
      cps: Math.round(Game.cookiesPs),
      buildings: Object.values(Game.Objects)
        .filter(o => o.amount > 0)
        .map(o => `${o.name}:${o.amount}`),
      elderWrath: Game.elderWrath,
      upgrades: Game.UpgradesOwned,
    });
  }, 60000);

  // --- 300s: セーブ退避 ---
  setInterval(() => {
    if (typeof Game === 'undefined' || !Game.ready) return;
    post({
      cookies: Math.round(Game.cookies),
      save: Game.WriteSave(1),
    });
  }, 300000);
})();

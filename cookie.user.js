// ==UserScript==
// @name         Fable印・クッキー自動化 v2
// @namespace    gamehub
// @version      2.3.0
// @description  自動クリック+回収期間ベース購入+Game Hubへの進捗報告/セーブ退避/スクショ送信
// @match        https://orteil.dashnet.org/cookieclicker/*
// @grant        GM_xmlhttpRequest
// @connect      lostworldproject
// @noframes
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
      timeout: 10000,
      // 401/500等はonerrorでなくonloadに来るため、ステータスも失敗判定する
      onload: (res) => {
        if (res.status < 200 || res.status >= 300) {
          console.warn('[gamehub] report rejected: HTTP ' + res.status);
        }
      },
      onerror: () => console.warn('[gamehub] report failed'),
      ontimeout: () => console.warn('[gamehub] report timed out'),
    });
  }

  // Game準備チェックと例外処理を共通化(1tickの失敗で以降が壊れないように)
  function every(ms, fn) {
    setInterval(() => {
      if (typeof Game === 'undefined' || !Game.ready) return;
      try { fn(); } catch (e) { console.warn('[gamehub]', e); }
    }, ms);
  }

  // Game.buyModeが売却側(-1)だとbuy()は建物を売ってしまうため、必ず購入モードで1個買う
  function buyOne(building) {
    const { buyMode, buyBulk } = Game;
    Game.buyMode = 1;
    Game.buyBulk = 1;
    try {
      building.buy(1);
    } finally {
      Game.buyMode = buyMode;
      Game.buyBulk = buyBulk;
    }
  }

  // --- 100ms: クリック系 ---
  every(100, () => {
    Game.ClickCookie();
    // ゴールデン+トナカイのみ回収、ラースは温存(黙示録は観測対象)
    // pop()はGame.shimmersから要素を抜くので、コピーしてから走査する
    for (const s of [...Game.shimmers]) {
      if ((s.type === 'golden' && !s.wrath) || s.type === 'reindeer') s.pop();
    }
    // ニュース欄のフォーチュンも回収
    if (Game.TickerEffect && Game.TickerEffect.type === 'fortune') Game.tickerL.click();
  });

  // --- 1s: 購入系(待ち時間込み回収期間ベースの貪欲法) ---
  every(1000, () => {
    // tech(黙示録研究)とtoggle(シーズン切替等)は自動購入しない
    // buy(1)のbypassがスキップするのは確認ダイアログ(clickFunction)のみで、
    // 支払いはbuy()内のcanBuy+Game.Spendで必ず発生する。ランプ払いだけは
    // bypassで無償取得になるが、該当するSugar frenzyはtoggleなので除外済み
    Game.UpgradesInStore
      .filter(u => u.canBuy() && u.pool !== 'tech' && u.pool !== 'toggle')
      .sort((a, b) => a.getPrice() - b.getPrice())
      .forEach(u => u.buy(1));

    // 「貯まるまでの待ち時間+回収期間」が最短の建物を狙う。
    // 最良候補にまだ手が届かない場合は安物を買わずに貯金する
    const cps = Math.max(Game.cookiesPs, 1e-9);
    const best = Object.values(Game.Objects)
      .map(o => {
        const price = o.getPrice();
        const gain = Math.max(o.storedCps * Game.globalCpsMult, 1e-9);
        const wait = Math.max(price - Game.cookies, 0) / cps;
        return { o, price, score: wait + price / gain };
      })
      .sort((a, b) => a.score - b.score)[0];
    if (best && best.price <= Game.cookies) buyOne(best.o);
  });

  // --- 60s: ハブへ進捗報告 ---
  every(60000, () => {
    post({
      type: 'report',
      cookies: Math.round(Game.cookies),
      cps: Math.round(Game.cookiesPs),
      // Frenzy等のバフを除いたCpS(グラフ用。スパイクで暴れないベースライン)
      baseCps: Math.round(Game.unbuffedCps),
      buildings: Object.values(Game.Objects)
        .filter(o => o.amount > 0)
        .map(o => `${o.name}:${o.amount}`),
      elderWrath: Game.elderWrath,
      wrinklers: Game.wrinklers.filter(w => w.phase > 0).length,
      upgrades: Game.UpgradesOwned,
      prestige: Game.prestige,
    });
  });

  // --- 300s: セーブ退避 ---
  every(300000, () => {
    post({
      type: 'save',
      cookies: Math.round(Game.cookies),
      save: Game.WriteSave(1),
    });
  });

  // --- 300s: スクショ送信 ---
  // 左パネルのcanvas(大クッキー・ミルク・ワームの描画領域)を画像化して送る。
  // ゴールデンクッキー等のDOM要素は写らない。canvas汚染等のtoDataURL例外は
  // every()のtry/catchで握る
  every(300000, () => {
    const canvas = Game.LeftBackground && Game.LeftBackground.canvas;
    if (!canvas || !canvas.width || !canvas.height) return;
    post({
      type: 'shot',
      shot: canvas.toDataURL('image/jpeg', 0.7),
    });
  });
})();

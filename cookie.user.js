// ==UserScript==
// @name         Fable印・クッキー自動化 v2
// @namespace    gamehub
// @version      2.4.0
// @description  自動クリック+購入+砂糖玉/黙示録/昇天の自動運用+Game Hubへの進捗報告/セーブ退避/スクショ送信
// @match        https://orteil.dashnet.org/cookieclicker/*
// @grant        GM_xmlhttpRequest
// @connect      lostworldproject
// @noframes
// @updateURL    https://raw.githubusercontent.com/Kanako-12/cookie-automation/main/cookie.user.js
// @downloadURL  https://raw.githubusercontent.com/Kanako-12/cookie-automation/main/cookie.user.js
// ==/UserScript==

(function () {
  'use strict';

  const BASE = 'http://lostworldproject:8090';
  const GAME = 'cookieclicker';
  const HUB = BASE + '/report/' + GAME;

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

  // Hubのダッシュボードから切り替える設定。取得できるまでは安全側(昇天しない)
  let hubConfig = { autoAscend: false };
  function fetchConfig() {
    GM_xmlhttpRequest({
      method: 'GET',
      url: BASE + '/config/' + GAME,
      timeout: 10000,
      onload: (res) => {
        if (res.status !== 200) return;
        try {
          const c = JSON.parse(res.responseText);
          if (c && typeof c.autoAscend === 'boolean') hubConfig = c;
        } catch (e) { /* 不正応答は無視して前回値を維持 */ }
      },
    });
  }
  fetchConfig();
  setInterval(fetchConfig, 60000);

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
    // ゴールデン+トナカイのみ回収、ラースは放置(クリックしなければデバフもない)
    // pop()はGame.shimmersから要素を抜くので、コピーしてから走査する
    for (const s of [...Game.shimmers]) {
      if ((s.type === 'golden' && !s.wrath) || s.type === 'reindeer') s.pop();
    }
    // ニュース欄のフォーチュンも回収
    if (Game.TickerEffect && Game.TickerEffect.type === 'fortune') Game.tickerL.click();
  });

  // --- 1s: 砂糖玉が完熟(ripe)したら即収穫 ---
  // mature段階の収穫は50%で0個の博打なのでせず、確定になった瞬間に摘む。
  // 放置落下と収量は同じだが、次の玉の成長がその分早く始まる
  every(1000, () => {
    if (Game.canLumps() && Date.now() - Game.lumpT >= Game.lumpRipeAge) Game.clickLump();
  });

  // --- 1s: 購入系(待ち時間込み回収期間ベースの貪欲法) ---
  every(1000, () => {
    // toggle(シーズン切替・Elder Pledge/Covenant等)は自動購入しない。
    // tech(黙示録研究)は購入対象:リンクラー放牧のため黙示録を自動進行させる。
    // buy(1)のbypassがスキップするのは確認ダイアログ(clickFunction)のみで、
    // 支払いはbuy()内のcanBuy+Game.Spendで必ず発生する。ランプ払いだけは
    // bypassで無償取得になるが、該当するSugar frenzyはtoggleなので除外済み
    Game.UpgradesInStore
      .filter(u => u.canBuy() && u.pool !== 'toggle')
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

  // --- 60s: 砂糖玉の消費(建物レベル上げ) ---
  // ミニゲーム解禁(農場/寺院/魔法塔/銀行をLv1)を最優先、以後は最低Lvの建物から。
  // Sugar baking(未使用玉1個につき+1%CpS、100個まで)を持っていたら100個は温存
  function lumpLevelUp(target) {
    const ask = Game.prefs.askLumps;
    Game.prefs.askLumps = 0; // spendLumpの確認ダイアログで止まらないよう一時的に外す
    try { target.levelUp(); } finally { Game.prefs.askLumps = ask; }
  }
  every(60000, () => {
    if (!Game.canLumps()) return;
    const reserve = Game.Has('Sugar baking') ? 100 : 0;
    const target =
      ['Farm', 'Temple', 'Wizard tower', 'Bank']
        .map(n => Game.Objects[n])
        .find(o => o && o.amount > 0 && o.level < 1) ||
      Object.values(Game.Objects)
        .filter(o => o.amount > 0)
        .sort((a, b) => a.level - b.level)[0];
    // レベルアップ費用は現Lv+1個。温存分を割り込むなら見送り
    if (target && Game.lumps - (target.level + 1) >= reserve) lumpLevelUp(target);
  });

  // --- 60s: リンクラー回収 ---
  // 満員(通常10匹)まで貯めてから1時間ごとに一括回収(吸われた分が1.1倍で戻る)。
  // 回収後の再湧き待ちの取りこぼしを減らすため高頻度では潰さない。
  // レアなshinyリンクラー(type===1)は温存する
  let wrinklersPoppedAt = Date.now();
  every(60000, () => {
    const active = Game.wrinklers.filter(w => w.phase > 0);
    if (active.length < Game.getWrinklersMax()) return;
    if (Date.now() - wrinklersPoppedAt < 3600e3) return;
    for (const w of active) if (w.type === 0) w.hp = 0;
    wrinklersPoppedAt = Date.now();
  });

  // リンクラーを今潰した場合の見込み還元額。main.jsのUpdateWrinklersの
  // pop係数(1.1×Sacrilegious corruption×Dragon Gutsオーラ×shiny3倍×
  // Wrinklerspawn)と同じ。Pantheonの神ボーナスのみ省略=控えめな見積もり
  function wrinklerPayout() {
    let total = 0;
    for (const w of Game.wrinklers) {
      if (w.phase > 0 && w.sucked > 0) {
        let mult = 1.1;
        if (Game.Has('Sacrilegious corruption')) mult *= 1.05;
        mult *= 1 + Game.auraMult('Dragon Guts') * 0.2;
        if (w.type === 1) mult *= 3;
        if (Game.Has('Wrinklerspawn')) mult *= 1.05;
        total += w.sucked * mult;
      }
    }
    return total;
  }

  // --- 60s: 自動昇天(Hubのトグルがオンのときだけ) ---
  // 「今昇天したら得られるプレステージ」が現在値と同量以上(=2倍化)かつ
  // 100以上になったら実行。100の下限は意図的な床で、prestige 0〜99の帯で
  // 数レベルの獲得ごとに昇天を連打しないようにするため(2倍化条件だけだと
  // 低prestige帯では数分おきの昇天ループになり得る)。
  // リンクラーの未回収分も判定に含め、
  // 条件を満たしたらまず全回収(shinyも。リセットで消えるため)して
  // cookiesEarnedに実額を反映させ、次tickで昇天する。回収は一度だけ:
  // 回収後60秒の間に新しいリンクラーが吸い始めても(黙示録中は数十秒で
  // 再湧きする)再回収でループせず、微少な新規分は諦めて昇天を優先する
  let ascendCollected = false;
  every(60000, () => {
    // オフにしたら回収状態も破棄し、再有効化は新しい昇天試行として扱う
    // (回収直後にオフ→後日オンで、溜まった分を回収せず即昇天しないように)
    if (!hubConfig.autoAscend) { ascendCollected = false; return; }
    if (Game.OnAscend || Game.AscendTimer > 0 || Game.ReincarnateTimer > 0) return;
    const potential = Math.floor(Game.HowMuchPrestige(
      Game.cookiesReset + Game.cookiesEarned + wrinklerPayout()));
    const gained = potential - Game.prestige;
    if (gained < Math.max(Game.prestige, 100)) { ascendCollected = false; return; }
    if (!ascendCollected && Game.wrinklers.some(w => w.phase > 0 && w.sucked > 0)) {
      ascendCollected = true;
      Game.CollectWrinklers();
      return;
    }
    ascendCollected = false;
    Game.Ascend(1);
  });

  // --- 10s: 昇天画面での買い物と転生(自動昇天オン時のみ) ---
  every(10000, () => {
    if (!hubConfig.autoAscend || !Game.OnAscend) return;
    // ツリーの解禁条件を満たす天国アップグレードを安い順に買い切る。
    // prestige poolのbuy()は解禁条件を再検証しないため、本体のBuildAscendTree
    // と同じ条件を自前で確認する:全親の所持(AND)+showIf(Lucky digit系の
    // 「prestigeに7を含む」等の出現条件)
    for (;;) {
      const next = Object.values(Game.Upgrades)
        .filter(u => u.pool === 'prestige' && !u.bought &&
          u.getPrice() <= Game.heavenlyChips &&
          u.parents.every(p => p === -1 || p.bought) &&
          (!u.showIf || u.showIf()))
        .sort((a, b) => a.getPrice() - b.getPrice())[0];
      if (!next) break;
      next.buy();
    }
    Game.Reincarnate(1);
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
      lumps: Math.max(Game.lumps, 0), // 未解禁時は-1なので0に丸める
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

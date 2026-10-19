/* 面板上的两处积分视图：网关积分卡的「今日消耗」与用量明细按模型的「积分」列。
 *
 * 这两处都只错在"数字落在哪里"：整张表里出现过某个数字不算数，它必须出现在
 * 描述它的那一行/那张卡里；今日消耗还必须与全历史数字分开，否则读者会把
 * 999.50 当成今天用掉的。这类回归在浏览器里只表现为看错数，Python 测试看不见，
 * 所以这里驱动 dashboard.html 里真实的 refreshInner / renderDim。
 *
 * 另外钉住两件容错：today 窗口取不到时整轮刷新照常画（卡片只是没有今日那行），
 * 以及载荷里没有 credit 字段时模型行显示 — 而不是少一格导致整行错位。
 *
 * Run with Node: node tests/_test_dashboard_credit_views.js
 */
'use strict';
const assert = require('assert');
const {dashboardScript} = require('./_dashboard_source.js');
const dom = require('./_dom_stub.js');

const script = dashboardScript();

function okJson(payload){
  return {status: 200, ok: true,
          json: () => Promise.resolve(payload),
          text: () => Promise.resolve(JSON.stringify(payload))};
}

// 数据看板整段包在 IIFE 里（(function(){ ... })();），对外只挂了
// window.loadAnalytics / wbaLoad，renderDim 与 state 都是 IIFE 内部变量。
// 这里在 IIFE 出口锚点后面补一行导出再整体求值；锚点一旦改名，套件大声失败，
// 而不是静默地测不到渲染逻辑。
const EXPORT_ANCHOR = 'window.loadAnalytics = wbaTick;';
assert.ok(script.includes(EXPORT_ANCHOR),
  'dashboard.html 的 analytics IIFE 出口变了（找不到 "' + EXPORT_ANCHOR + '"），请同步更新本测试的导出补丁');
const patchedScript = script.replace(EXPORT_ANCHOR,
  EXPORT_ANCHOR + '\nwindow.__creditViewsTest = { renderDim: renderDim, state: state };');

// 每个用例一份新的全局环境：按 URL 前缀路由 fetch（长前缀优先）。没命中的
// 一律答 {authenticated:false}，面板停在门控页，不会自己起定时器。
function boot(routes){
  const calls = [];
  const table = Object.keys(routes || {}).sort((a, b) => b.length - a.length);
  const installed = dom.installDom({
    fetch: (url, init) => {
      const u = String(url);
      calls.push(u);
      for (const prefix of table){
        if (u.startsWith(prefix)) return routes[prefix](u, init);
      }
      return Promise.resolve(okJson({authenticated: false}));
    },
  });
  const top = new Function(patchedScript + '\nreturn {refresh};')();
  const views = installed.window.__creditViewsTest;
  const api = {refresh: top.refresh, renderDim: views.renderDim, state: views.state};
  return {api, calls, dom: installed};
}

const stat = (o) => Object.assign({
  requests: 0, errors: 0, prompt_tokens: 0, completion_tokens: 0, reasoning_tokens: 0,
  cached_tokens: 0, total_tokens: 0, credit: 0, cost_cny: 0, cache_hit_pct: 0,
  speed_avg: 0,
}, o);

// 卡片是 innerHTML 里一串 <div class="card">，按标题取回它自己那一块，
// 这样"今日数字在积分卡上"读的是那张卡，而不是整段 HTML。
function cardsOf(html){
  return html.split('<div class="card').slice(1);
}
function cardTitled(html, title){
  return cardsOf(html).find(b => b.includes(title)) || '';
}

let pass = 0, fail = 0;
const check = (label, cond, extra) => {
  if (cond) { pass++; console.log('  [PASS] ' + label); }
  else { fail++; console.log('  [FAIL] ' + label + (extra !== undefined ? '  ' + extra : '')); }
};

const thsOf = (html) =>
  [...html.matchAll(/<th[^>]*>([\s\S]*?)<\/th>/g)].map(m => m[1]);
const trsOf = (html) =>
  [...html.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/g)].map(m => m[1]);
const tdsOf = (row) =>
  [...row.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(m => m[1]);

(async () => {
  /* 1) 网关页「积分消耗」卡：今日消耗显示在副行，全历史数字仍在主值。 */
  {
    const {api, calls} = boot({
      '/usage?realm=intl&range=today': () => Promise.resolve(okJson({credit: 12.5, requests: 3, realm: 'intl'})),
      '/usage/recent': () => Promise.resolve(okJson({total: 0, page: 1, total_pages: 1, rows: []})),
      '/usage/perf': () => Promise.resolve(okJson({errors: 0, success_rate_pct: 100})),
      '/usage?realm=intl': () => Promise.resolve(okJson({requests: 100, credit: 999.5, cost_cny: 8,
                                                           usd_cny: 7.2, realm: 'intl', log_file: 'usage/usage.jsonl'})),
    });
    await api.refresh();
    const cards = document.getElementById('cards').innerHTML;
    const costCard = cardTitled(cards, '积分消耗');
    check('积分消耗卡已渲染', !!costCard);
    check('今日消耗出现在积分消耗卡的副行上',
          costCard.includes('今日 12.50 积分'), costCard);
    check('全历史积分仍在主值上', costCard.includes('999.50'), costCard);
    check('今日消耗只出现在这一张卡上',
          cardsOf(cards).filter(b => b.includes('今日 12.50 积分')).length === 1,
          cardsOf(cards).length + ' 张卡');
    check('今日窗口的请求确实发给了服务端（range=today）',
          calls.some(u => u.startsWith('/usage?realm=intl&range=today')),
          calls.join(' | '));
    check('估算价值与今日消耗并排在同一副行',
          costCard.includes('估算价值'), costCard);
  }

  /* 1b) today 窗口失败：整轮刷新照常画，卡片只是没有今日那行。 */
  {
    const {api} = boot({
      '/usage?realm=intl&range=today': () => Promise.reject(new Error('today window down')),
      '/usage/recent': () => Promise.resolve(okJson({total: 0, page: 1, total_pages: 1, rows: []})),
      '/usage/perf': () => Promise.resolve(okJson({errors: 0, success_rate_pct: 100})),
      '/usage?realm=intl': () => Promise.resolve(okJson({requests: 5, credit: 3.5, cost_cny: 0.5,
                                                          usd_cny: 7.2, realm: 'intl'})),
    });
    await api.refresh();
    const cards = document.getElementById('cards').innerHTML;
    const costCard = cardTitled(cards, '积分消耗');
    check('今日窗口取不到时整轮刷新不受影响', !!costCard, cards.slice(0, 120));
    check('今日取不到就不显示今日那行', !costCard.includes('今日'), costCard);
    check('估算价值仍在副行上', costCard.includes('估算价值'), costCard);
  }

  /* 2) 用量明细 → 按模型：每个模型行自带一格「积分」。 */
  {
    const {api} = boot({});
    const st = api.state;
    st.dim = 'model';
    st.analytics = {
      models: [
        {model: 'model-big', window: stat({requests: 10, total_tokens: 5000, credit: 1234.5,
                                            cost_cny: 1.23, cache_hit_pct: 80, speed_avg: 42})},
        {model: 'model-small', window: stat({requests: 2, total_tokens: 100, credit: 0.25,
                                              cost_cny: 0, cache_hit_pct: 50, speed_avg: 20})},
      ],
      accounts: [], keys: [],
      summary: {window: stat({}), all_time: stat({})},
    };
    api.renderDim();
    const headHtml = document.getElementById('dimHead').innerHTML;
    const heads = thsOf(headHtml);
    check('按模型表头有「积分」列', heads.includes('积分'), heads.join(' / '));
    check('积分列夹在缓存命中与花费之间',
          heads.indexOf('缓存命中') + 1 === heads.indexOf('积分')
          && heads.indexOf('积分') + 1 === heads.indexOf('花费'), heads.join(' / '));

    const rows = trsOf(document.getElementById('dimBody').innerHTML);
    const rowFor = (name) => rows.find(r => r.includes(name)) || '';
    const big = rowFor('model-big');
    const small = rowFor('model-small');
    const bigCells = tdsOf(big);
    check('model-big 行存在', !!big);
    check('model-big 每行列数与表头一致', bigCells.length === heads.length,
          bigCells.length + ' vs ' + heads.length);
    check('model-big 的积分格是它自己的 1,234.5', bigCells[5] === '1,234.5',
          JSON.stringify(bigCells[5]));
    const smallCells = tdsOf(small);
    check('model-small 的积分格是它自己的 0.25', smallCells[5] === '0.25',
          JSON.stringify(smallCells[5]));
    check('花费列仍在最后一格', bigCells[bigCells.length - 1] === '￥1.23',
          JSON.stringify(bigCells[bigCells.length - 1]));

    /* 载荷缺 credit（旧缓存/降级响应）时显示 —，而不是少一格把整行错位。 */
    st.analytics.models = [{model: 'legacy-model', window: {requests: 1, total_tokens: 10}}];
    api.renderDim();
    const legacyCells = tdsOf(trsOf(document.getElementById('dimBody').innerHTML)[0] || '');
    check('缺 credit 时该格显示 —', legacyCells[5] === '—', JSON.stringify(legacyCells[5]));
    check('缺 credit 时列数仍然对齐', legacyCells.length === heads.length,
          legacyCells.length + ' vs ' + heads.length);

    /* 空区间用表头长度做 colspan，加了列也不能停在旧值。 */
    st.analytics.models = [];
    api.renderDim();
    check('空区间的占位行 colspan 跟随表头长度',
          document.getElementById('dimBody').innerHTML.includes(`colspan="${heads.length}"`),
          document.getElementById('dimBody').innerHTML);
  }

  /* 2b) 按账号维度保持原样：仍只有「剩余积分」，不多出消耗列。 */
  {
    const {api} = boot({});
    const st = api.state;
    st.dim = 'account';
    st.analytics = {
      models: [],
      accounts: [{uid: 'u1', nickname: '甲', realm: 'intl',
                  window: stat({requests: 5, total_tokens: 300, credit: 7.5}),
                  credits: {remain: 20}}],
      keys: [],
      summary: {window: stat({}), all_time: stat({})},
    };
    api.renderDim();
    const heads = thsOf(document.getElementById('dimHead').innerHTML);
    check('按账号表头仍是 剩余积分 / 花费 两列',
          heads[5] === '剩余积分' && heads[6] === '花费', heads.join(' / '));
    check('按账号没有多出消耗积分列', !heads.includes('积分'), heads.join(' / '));
    const cells = tdsOf(trsOf(document.getElementById('dimBody').innerHTML)[0] || '');
    check('按账号每行列数与表头一致', cells.length === heads.length,
          cells.length + ' vs ' + heads.length);
    check('剩余积分格读的是账号自己的余额',
          cells[5].replace(/<[^>]*>/g, '') === '20.0', JSON.stringify(cells[5]));
  }

  console.log();
  console.log('PASS=' + pass + ' FAIL=' + fail);
  process.exit(fail ? 1 : 0);
})().catch(error => { console.error(error); process.exit(1); });

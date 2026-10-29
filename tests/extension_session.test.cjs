// Critical export provenance/identity UX only; no browser account or network.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {join} = require('node:path');

async function exportSession({stores, payload = {status: 'needs_reauth', error: 'TikTok did not confirm an authenticated account'}} = {}) {
  const elements = {
    '#send': {addEventListener(type, callback) {this.click = callback;}},
    '#result': {}, '#server': {value: 'https://admin.legendary-trio.me'}, '#code': {value: 'x'.repeat(32)},
  };
  const calls = [];
  const chrome = {
    tabs: {query: async () => [{id: 7, url: 'https://www.tiktok.com/tiktokstudio/upload'}]},
    permissions: {request: async () => true},
    cookies: {
      getAllCookieStores: async () => stores ?? [{id: 'regular', tabIds: [3]}, {id: 'private', tabIds: [7]}],
      getAll: async details => {
        calls.push(details);
        return [{domain: '.tiktok.com', path: '/', name: 'sessionid', value: 'test-only-session', storeId: details.storeId}];
      },
    },
  };
  let sent;
  runInNewContext(readFileSync(join(__dirname, '../extension/popup.js'), 'utf8'), {
    document: {querySelector: selector => elements[selector]}, chrome,
    navigator: {userAgent: 'test browser'}, URL, AbortController, setTimeout, clearTimeout,
    fetch: async (url, options) => {sent = JSON.parse(options.body); return {ok: true, json: async () => payload};},
  });
  await elements['#send'].click();
  return {calls, sent, text: elements['#result'].textContent, code: elements['#code'].value};
}

test('exports only active tab store and says authentication was refused', async () => {
  const result = await exportSession();
  assert.equal(result.calls[0].storeId, 'private');
  assert.equal(result.sent.cookies[0].storeId, 'private');
  assert.match(result.text, /not connected/i);
  assert.match(result.text, /TikTok did not confirm an authenticated account/);
  assert.equal(result.code, '');
});

test('missing active tab store fails closed before reading cookies or sending', async () => {
  const result = await exportSession({stores: [{id: 'regular', tabIds: [3]}]});
  assert.equal(result.calls.length, 0);
  assert.equal(result.sent, undefined);
  assert.match(result.text, /cookie store/i);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {mkdtemp, mkdir, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const text = result => result.content.filter(block => block.type === 'text').map(block => block.text).join('\n');
const ref = (snapshot, label) => {
  const line = snapshot.split('\n').find(line => line.includes('"' + label + '"'));
  assert.ok(line, 'Missing observed element ' + label + '\n' + snapshot);
  return line.match(/\[ref=([^\]]+)\]/)[1];
};

test('official Playwright MCP: isolated login, form, actual POST and recovery', {timeout: 60000}, async t => {
  const workspace = await mkdtemp(path.join(tmpdir(), 'yuki-browser-'));
  const orders = [], log = [];
  const page = createServer(async (request, response) => {
    let body = '';
    for await (const part of request) body += part;
    if (request.url === '/login' && request.method === 'POST') {
      const form = new URLSearchParams(body);
      assert.equal(form.get('username'), 'test-user');
      assert.equal(form.get('password'), 'test-only-password');
      response.writeHead(303, {'Set-Cookie': 'session=test-session; Path=/; HttpOnly', Location: '/'});
      response.end(); return;
    }
    if (request.url === '/orders' && request.method === 'POST') {
      assert.match(request.headers.cookie || '', /session=test-session/);
      orders.push(Object.fromEntries(new URLSearchParams(body)));
      response.writeHead(303, {Location: '/'}); response.end(); return;
    }
    response.setHeader('Content-Type', 'text/html; charset=utf-8');
    response.end(request.headers.cookie?.includes('session=test-session')
      ? '<h1>Workshop orders</h1><form method="post" action="/orders"><label>Order title<input name="title"></label><button>Create order</button></form><p>Saved orders: ' + orders.length + '</p>'
      : '<h1>Test login</h1><form method="post" action="/login"><label>Username<input name="username"></label><label>Password<input name="password" type="password"></label><button>Sign in</button></form>');
  });
  await new Promise(resolve => page.listen(0, '127.0.0.1', resolve));
  const child = spawn(process.execPath, [path.join(root, 'adapter.mjs')], {detached: true, stdio: ['pipe', 'pipe', 'pipe']});
  let id = 0, errors = '';
  const pending = new Map(), lines = createInterface({input: child.stdout});
  lines.on('line', line => {
    const message = JSON.parse(line);
    if (message.id !== undefined) pending.get(message.id)?.(message);
  });
  child.stderr.on('data', data => errors += data);
  const send = message => new Promise((resolve, reject) => {
    const current = ++id;
    const timer = setTimeout(() => reject(new Error('No MCP reply: ' + errors)), 30000);
    pending.set(current, response => {clearTimeout(timer); pending.delete(current); log.push({request: message, response}); resolve(response);});
    child.stdin.write(JSON.stringify({id: current, ...message}) + '\n');
  });
  t.after(async () => {
    try {process.kill(-child.pid, 'SIGKILL');} catch {}
    lines.close(); page.closeAllConnections();
    await new Promise(resolve => page.close(resolve));
    await rm(workspace, {recursive: true, force: true});
  });
  const connected = await send({method: 'connect', workspace, server: {command: process.execPath,
    args: [path.join(root, 'node_modules/@playwright/mcp/cli.js'), '--config', path.join(root, 'browser.config.json'), '--timeout-action', '500']}});
  assert.ok(connected.result, JSON.stringify(connected));
  const call = async (name, args) => {
    const response = await send({method: 'call', name, arguments: args});
    assert.ok(response.result, JSON.stringify(response));
    assert.notEqual(response.result.isError, true, text(response.result));
    return response.result;
  };
  await call('browser_navigate', {url: 'http://127.0.0.1:' + page.address().port});
  let snapshot = text(await call('browser_snapshot', {}));
  await call('browser_fill_form', {fields: [
    {target: ref(snapshot, 'Username'), name: 'Username', type: 'textbox', value: 'test-user'},
    {target: ref(snapshot, 'Password'), name: 'Password', type: 'textbox', value: 'test-only-password'}
  ]});
  snapshot = text(await call('browser_snapshot', {}));
  await call('browser_click', {target: ref(snapshot, 'Sign in')});
  snapshot = text(await call('browser_snapshot', {}));
  assert.match(snapshot, /Workshop orders/);
  await call('browser_fill_form', {fields: [{target: ref(snapshot, 'Order title'), name: 'Order title', type: 'textbox', value: 'MCP production proof'}]});
  snapshot = text(await call('browser_snapshot', {}));
  await call('browser_click', {target: ref(snapshot, 'Create order')});
  snapshot = text(await call('browser_snapshot', {}));
  assert.match(snapshot, /Saved orders: 1/);
  assert.deepEqual(orders, [{title: 'MCP production proof'}]);
  const failed = await send({method: 'call', name: 'browser_click', arguments: {target: 'this-selector-does-not-exist'}});
  assert.equal(failed.result.isError, true);
  snapshot = text(await call('browser_snapshot', {}));
  assert.match(snapshot, /Saved orders: 1/);
  await call('browser_close', {});
  await call('browser_navigate', {url: 'http://127.0.0.1:' + page.address().port});
  snapshot = text(await call('browser_snapshot', {}));
  assert.match(snapshot, /Test login/);
  assert.doesNotMatch(snapshot, /Workshop orders/);
  await send({method: 'close'});
  const evidence = path.join(root, 'evidence');
  await mkdir(evidence, {recursive: true});
  await writeFile(path.join(evidence, 'browser-integration.json'), JSON.stringify({
    checkedAt: new Date().toISOString(), serverInfo: connected.result.serverInfo,
    transport: 'stdio', isolatedProfile: true, testOnlyCredentials: true,
    checks: {discovery: true, navigation: true, login: true, cookieRetained: true, fillForm: true,
      actualPost: true, snapshot: true, badSelectorIsError: true, recovery: true, cookieNotInheritedAfterClose: true, close: true},
    actualOrders: orders, calls: log
  }, null, 2) + '\n');
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

const adapter = fileURLToPath(new URL('../adapter.mjs', import.meta.url));
const server = fileURLToPath(new URL('./fixture-server.mjs', import.meta.url));
const alive = pid => {try {process.kill(pid, 0); return true;} catch {return false;}};

async function fixture(t, prefix = '') {
  const workspace = await mkdtemp(path.join(tmpdir(), 'yuki-mcp-'));
  const process = spawn(globalThis.process.execPath, [adapter], {detached: true, stdio: ['pipe', 'pipe', 'pipe']});
  let sequence = 0, errors = '';
  const pending = new Map(), events = [];
  const lines = createInterface({input: process.stdout});
  lines.on('line', line => {
    const message = JSON.parse(line);
    if (message.event) events.push(message);
    else pending.get(message.id)?.(message);
  });
  process.stderr.on('data', data => errors += data);
  const request = message => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => reject(new Error('adapter response missing: ' + errors)), 5000);
    pending.set(id, result => {clearTimeout(timer); pending.delete(id); resolve(result);});
    process.stdin.write(JSON.stringify({id, ...message}) + '\n');
  });
  t.after(async () => {
    try {globalThis.process.kill(-process.pid, 'SIGKILL');} catch {}
    lines.close();
    await rm(workspace, {recursive: true, force: true});
  });
  return {process, workspace, request, events, connect: () => request({method: 'connect', workspace, server: {command: globalThis.process.execPath, args: [server, prefix]}, timeoutMs: 2000})};
}

test('real stdio discovery aggregates pages and preserves tool session state', async t => {
  const f = await fixture(t), connection = await f.connect();
  assert.equal(connection.result.serverInfo.name, 'yuki-integration-fixture');
  assert.equal(connection.result.tools.length, 8);
  assert.equal(alive(connection.result.serverPid), true);
  await f.request({method: 'call', name: 'state', arguments: {value: '雪站'}});
  const result = await f.request({method: 'call', name: 'state', arguments: {}});
  assert.deepEqual(result.result, {content: [{type: 'text', text: '雪站'}], structuredContent: {value: '雪站'}});
});

test('roots/list returns task workspace and a tool actually writes there', async t => {
  const f = await fixture(t); await f.connect();
  const roots = await f.request({method: 'call', name: 'roots', arguments: {}});
  assert.equal(JSON.parse(roots.result.content[0].text).roots[0].uri, pathToFileURL(await import('node:fs/promises').then(fs => fs.realpath(f.workspace))).href);
  await f.request({method: 'call', name: 'write', arguments: {value: 'actual MCP output'}});
  assert.equal(await readFile(path.join(f.workspace, 'output.txt'), 'utf8'), 'actual MCP output');
});

test('tool business errors and protocol errors remain distinct and recoverable', async t => {
  const f = await fixture(t); await f.connect();
  assert.equal((await f.request({method: 'call', name: 'fail', arguments: {}})).result.isError, true);
  assert.equal((await f.request({method: 'call', name: 'state', arguments: {value: 123}})).result.isError, true);
  assert.match((await f.request({method: 'call', name: 'missing', arguments: {}})).error.message, /Unknown tool/);
  assert.equal((await f.request({method: 'call', name: 'state', arguments: {value: 'recovered'}})).result.structuredContent.value, 'recovered');
});

test('tool list changes are forwarded and explicit list refresh sees the addition', async t => {
  const f = await fixture(t); await f.connect();
  await f.request({method: 'call', name: 'add_tool', arguments: {}});
  assert.ok(f.events.some(event => event.event === 'tools_changed'));
  const result = await f.request({method: 'list'});
  assert.ok(result.result.tools.some(tool => tool.name === 'added'));
});

test('per-call timeout returns an error and next tool call works', async t => {
  const f = await fixture(t); await f.connect();
  const start = Date.now();
  const result = await f.request({method: 'call', name: 'slow', arguments: {}, timeoutMs: 50});
  assert.match(result.error.message, /timed out/i);
  assert.ok(Date.now() - start < 1000);
  assert.ok((await f.request({method: 'call', name: 'state', arguments: {}})).result);
});

test('server exit returns a transport error instead of a successful tool result', async t => {
  const f = await fixture(t); await f.connect();
  const result = await f.request({method: 'call', name: 'crash', arguments: {}});
  assert.ok(result.error);
  assert.equal(result.result, undefined);
});

test('normal close reaps the MCP server', async t => {
  const f = await fixture(t), connection = await f.connect();
  assert.deepEqual((await f.request({method: 'close'})).result, {closed: true});
  assert.equal(alive(connection.result.serverPid), false);
});

test('host cancellation of the dedicated process group stops adapter, server and child', async t => {
  const f = await fixture(t), connection = await f.connect();
  const child = Number((await f.request({method: 'call', name: 'child', arguments: {}})).result.content[0].text);
  const exited = new Promise(resolve => f.process.once('exit', resolve));
  globalThis.process.kill(-f.process.pid, 'SIGTERM');
  await exited;
  for (let attempt = 0; attempt < 30 && (alive(child) || alive(connection.result.serverPid)); attempt++) await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(alive(child), false);
  assert.equal(alive(connection.result.serverPid), false);
});

test('invalid input returns the matching request id without executing a tool', async t => {
  const f = await fixture(t);
  const missing = await f.request({method: 'call', name: 'state', arguments: {}});
  assert.match(missing.error.message, /not connected/);
  const malformed = await f.request({method: 'call', name: 'state', arguments: null});
  assert.equal(malformed.id, 2);
  assert.match(malformed.error.message, /arguments object/);
  const unknown = await f.request({method: 'http'});
  assert.match(unknown.error.message, /Unsupported/);
  const bad = await f.request({method: 'connect', workspace: f.workspace, server: {command: 'node', args: 'invalid'}});
  assert.match(bad.error.message, /configuration/);
});

test('server-defined prefixed names are discovered and invoked without an adapter mapping', async t => {
  const f = await fixture(t, 'new_role_'), connected = await f.connect();
  const tool = connected.result.tools.find(tool => tool.description.startsWith('Write value'));
  assert.equal(tool.name, 'new_role_write');
  await f.request({method: 'call', name: tool.name, arguments: {value: 'discovered dynamically'}});
  assert.equal(await readFile(path.join(f.workspace, 'output.txt'), 'utf8'), 'discovered dynamically');
});

test('missing server executable fails connect and allows a corrected connection', async t => {
  const f = await fixture(t);
  const failed = await f.request({method: 'connect', workspace: f.workspace,
    server: {command: path.join(f.workspace, 'missing-server'), args: []}});
  assert.ok(failed.error);
  assert.ok((await f.connect()).result.tools.length > 0);
});

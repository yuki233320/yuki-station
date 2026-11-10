import {Client} from '@modelcontextprotocol/client';
import {StdioClientTransport} from '@modelcontextprotocol/client/stdio';
import {createInterface} from 'node:readline';
import {realpath, stat} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';

const send = value => process.stdout.write(JSON.stringify(value) + '\n');
let client, transport, timeoutMs = 60000;

async function dispatch(request) {
  if (request.method === 'connect') return connect(request);
  if (request.method === 'close') {
    await close();
    return {closed: true};
  }
  if (!client) throw new Error('MCP server is not connected');
  if (request.method === 'list') return client.listTools({}, {timeout: timeoutMs, cache: 'refresh'});
  return client.callTool({name: request.name, arguments: request.arguments}, {timeout: request.timeoutMs ?? timeoutMs});
}

async function connect(request) {
  if (client) throw new Error('This task already has an MCP session');
  const workspace = await realpath(request.workspace);
  if (!(await stat(workspace)).isDirectory()) throw new Error('Workspace must be a directory');
  timeoutMs = request.timeoutMs ?? 60000;
  transport = new StdioClientTransport({...request.server, cwd: request.server.cwd ?? workspace, stderr: 'pipe'});
  // Diagnostics stay on stderr; stdout is exclusively the host's JSONL channel.
  transport.stderr.on('data', chunk => process.stderr.write(chunk));
  client = new Client({name: 'yuki-mcp-bridge', version: '0.1.0'}, {capabilities: {roots: {listChanged: false}}});
  client.setRequestHandler('roots/list', async () => ({roots: [{uri: pathToFileURL(workspace).href, name: 'task-workspace'}]}));
  client.setNotificationHandler('notifications/tools/list_changed', async () => send({event: 'tools_changed'}));
  client.onerror = error => process.stderr.write(String(error.message) + '\n');
  try {
    await client.connect(transport, {timeout: timeoutMs});
    const listing = await client.listTools({}, {timeout: timeoutMs, cache: 'refresh'});
    return {serverInfo: client.getServerVersion(), capabilities: client.getServerCapabilities(),
      serverPid: transport.pid, tools: listing.tools, transport: 'stdio', protocolMode: 'legacy'};
  } catch (error) {
    await close();
    throw error;
  }
}

async function close() {
  const current = client;
  client = undefined;
  if (current) await current.close();
  else if (transport) await transport.close();
  transport = undefined;
}

function validate(request) {
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  if (!object(request) || !['string', 'number'].includes(typeof request.id)) throw new Error('Request id is required');
  if (!['connect', 'list', 'call', 'close'].includes(request.method)) throw new Error('Unsupported adapter method');
  if (request.timeoutMs !== undefined && (!Number.isInteger(request.timeoutMs) || request.timeoutMs < 1 || request.timeoutMs > 3600000)) throw new Error('Invalid request timeout');
  if (request.method === 'connect') {
    const server = request.server;
    if (!object(server) || typeof server.command !== 'string' || !server.command.trim()
        || !Array.isArray(server.args) || server.args.some(v => typeof v !== 'string')
        || typeof request.workspace !== 'string' || !request.workspace
        || server.cwd !== undefined && typeof server.cwd !== 'string'
        || server.env !== undefined && (!object(server.env) || Object.values(server.env).some(v => typeof v !== 'string'))) {
      throw new Error('Invalid MCP stdio connection configuration');
    }
  }
  if (request.method === 'call' && (typeof request.name !== 'string' || !request.name || !object(request.arguments))) {
    throw new Error('Tool call requires a name and an arguments object');
  }
  return request;
}

const input = createInterface({input: process.stdin, crlfDelay: Infinity});
try {
  for await (const line of input) {
    let request;
    try {
      request = JSON.parse(line);
      validate(request);
      send({id: request.id, result: await dispatch(request)});
      if (request.method === 'close') break;
    } catch (error) {
      send({id: request?.id ?? null, error: {message: String(error.message), code: error.code ?? null}});
    }
  }
} finally {
  input.close();
  await close();
}

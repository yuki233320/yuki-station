import {createInterface} from 'node:readline';
import {writeFile} from 'node:fs/promises';
import {spawn} from 'node:child_process';

const send = value => process.stdout.write(JSON.stringify({jsonrpc: '2.0', ...value}) + '\n');
const prefix = process.argv[2] || '';
const tools = ['state', 'write', 'roots', 'fail', 'slow', 'add_tool', 'crash', 'child'].map(name => ({
  name: prefix + name, description: name === 'write' ? 'Write value to output.txt in the task workspace.' : name,
  inputSchema: {type: 'object', properties: {value: {type: 'string'}}}
}));
const pending = new Map();
let value = '', child;

async function handle(request) {
  const {id, method, params = {}} = request;
  if (method === 'initialize') return {protocolVersion: '2025-11-25', capabilities: {tools: {listChanged: true}}, serverInfo: {name: 'yuki-integration-fixture', version: '1.0.0'}};
  if (method === 'tools/list') return params.cursor ? {tools: tools.slice(2)} : {tools: tools.slice(0, 2), nextCursor: 'page-2'};
  if (method !== 'tools/call') throw new Error('Unknown method');
  const args = params.arguments;
  if (!tools.some(tool => tool.name === params.name)) throw new Error('Unknown tool');
  const name = params.name.slice(prefix.length);
  if (name === 'crash') process.exit(7);
  if (name === 'slow') await new Promise(resolve => setTimeout(resolve, 2000));
  if (name === 'state') {
    if (args.value !== undefined && typeof args.value !== 'string') return {isError: true, content: [{type: 'text', text: 'value must be a string'}]};
    if (args.value !== undefined) value = args.value;
    return {content: [{type: 'text', text: value}], structuredContent: {value}};
  }
  if (name === 'write') {
    await writeFile('output.txt', args.value);
    return {content: [{type: 'text', text: 'saved output.txt'}]};
  }
  if (name === 'roots') {
    const result = await new Promise(resolve => {pending.set('roots-' + id, resolve); send({id: 'roots-' + id, method: 'roots/list'});});
    return {content: [{type: 'text', text: JSON.stringify(result)}]};
  }
  if (name === 'fail') return {isError: true, content: [{type: 'text', text: 'fixture operation failed'}]};
  if (name === 'add_tool') {
    tools.push({name: prefix + 'added', inputSchema: {type: 'object'}});
    send({method: 'notifications/tools/list_changed'});
  }
  if (name === 'child') {
    child = spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], {stdio: 'ignore'});
    return {content: [{type: 'text', text: String(child.pid)}]};
  }
  return {content: [{type: 'text', text: params.name}]};
}

const lines = createInterface({input: process.stdin});
lines.on('line', line => {
  const message = JSON.parse(line);
  if (!message.method) {pending.get(message.id)?.(message.result); pending.delete(message.id); return;}
  if (message.id === undefined) return;
  handle(message).then(result => send({id: message.id, result})).catch(error => send({id: message.id, error: {code: -32602, message: error.message}}));
});
lines.on('close', () => {child?.kill(); process.exit(0);});

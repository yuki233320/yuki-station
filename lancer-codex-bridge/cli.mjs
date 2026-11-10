import {readFile,mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import {NativeBridge} from './bridge.mjs';

const args=process.argv.slice(2),command=args.shift()||'help';
function option(name,fallback){const i=args.indexOf(name);return i<0?fallback:args[i+1];}
const root=path.resolve(option('--root',process.cwd())),stateDir=path.resolve(option('--state',path.join(root,'.native-codex-bridge')));
const bridge=new NativeBridge({workspaceRoot:root,stateDir,allowWrites:args.includes('--allow-workspace-write'),binary:option('--codex','codex'),timeoutMs:Number(option('--timeout-ms','300000'))});
try {
  if(command==='doctor') {
    const result=await bridge.doctor();console.log(JSON.stringify(result,null,2));
    const output=option('--output',null);if(output){await mkdir(path.dirname(path.resolve(output)),{recursive:true});await writeFile(output,JSON.stringify(result,null,2));}
  } else if(command==='run') {
    const input=option('--input',null);let text;
    if(input)text=await readFile(input,'utf8');else {const chunks=[];let size=0;for await(const b of process.stdin){size+=b.length;if(size>400000)throw new Error('Input is too large');chunks.push(b);}text=Buffer.concat(chunks).toString();}
    const result=await bridge.run(JSON.parse(text),{onEvent:event=>console.log(JSON.stringify(event))});
    console.log(JSON.stringify({type:'receipt',receipt:result}));
    if(result.status!=='completed')process.exitCode=2;
  } else {
    console.log('yuki雪站 原生 Codex 桥接程序\n  node cli.mjs doctor [--output evidence.json]\n  node cli.mjs run --root /project --input task.json [--state /receipts] [--allow-workspace-write]\nInput: {id, instruction, workspace?, mode?, contextKey?, projectId?, threadName?, context?, model?, requiredCapabilities?}\nOutput: JSONL events and a terminal receipt. Approval requests are returned, never auto-accepted by the CLI.');
  }
} catch(e) {console.error(JSON.stringify({error:e.message}));process.exitCode=1;}

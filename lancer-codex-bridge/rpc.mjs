import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {EventEmitter} from 'node:events';

export class RpcError extends Error {
  constructor(error) { super(error.message || 'Codex request failed'); this.code=error.code; this.data=error.data; }
}

// The process uses the installed Codex binary and its own login. No credentials
// are read, copied or supplied by the bridge.
export class CodexRpc extends EventEmitter {
  constructor({binary='codex', args=['app-server','--stdio'], cwd, timeoutMs=45000, spawnProcess=spawn}={}) {
    super(); this.options={binary,args,cwd,timeoutMs,spawnProcess};this.pending=new Map();this.nextId=1;this.closed=false;
  }
  async start() {
    const {binary,args,cwd,spawnProcess}=this.options;
    this.child=spawnProcess(binary,args,{cwd,stdio:['pipe','pipe','pipe'],shell:false});
    this.child.on('error',e=>this.fail(e));
    this.child.on('exit',(code,signal)=>{this.closed=true;this.fail(new Error(`Codex process ended (${code ?? signal})`));this.emit('closed',{code,signal});});
    this.child.stderr.on('data',b=>this.emit('diagnostic',b.toString()));
    this.lines=createInterface({input:this.child.stdout,crlfDelay:Infinity});
    this.lines.on('line',line=>{
      if(!line.trim())return;
      let msg;try{msg=JSON.parse(line);}catch{this.emit('diagnostic','Ignored a non-JSON line from Codex stdout.');return;}
      this.receive(msg);
    });
    const info=await this.request('initialize',{clientInfo:{name:'lancer_codex_bridge',title:'yuki雪站 原生 Codex 桥接程序',version:'0.1.0'},capabilities:{experimentalApi:true}});
    this.notify('initialized',{});return info;
  }
  receive(msg) {
    if(msg.method && Object.hasOwn(msg,'id')) {this.emit('serverRequest',msg);return;}
    if(msg.method) {this.emit('notification',msg);return;}
    const p=this.pending.get(msg.id);if(!p)return;
    this.pending.delete(msg.id);clearTimeout(p.timer);
    if(msg.error)p.reject(new RpcError(msg.error));else p.resolve(msg.result);
  }
  send(value) {
    if(this.closed || !this.child?.stdin.writable)throw new Error('Codex connection is closed');
    this.child.stdin.write(JSON.stringify(value)+'\n');
  }
  request(method,params={},timeoutMs=this.options.timeoutMs) {
    const id=this.nextId++;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error(`Codex request timed out: ${method}`));},timeoutMs);
      this.pending.set(id,{resolve,reject,timer});
      try{this.send({id,method,params});}catch(e){clearTimeout(timer);this.pending.delete(id);reject(e);}
    });
  }
  notify(method,params={}) {this.send({method,params});}
  respond(id,result) {this.send({id,result});}
  reject(id,message='Unsupported client request') {this.send({id,error:{code:-32601,message}});}
  fail(error) {for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(error);}this.pending.clear();}
  async close() {
    if(!this.child || this.closed)return;
    this.child.stdin.end();
    await Promise.race([new Promise(r=>this.child.once('exit',r)),new Promise(r=>setTimeout(r,800))]);
    if(!this.closed)this.child.kill('SIGTERM');
  }
}

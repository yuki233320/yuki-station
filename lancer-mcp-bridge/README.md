# yuki雪站 MCP stdio adapter

Optional tool executor using the official MCP client and Microsoft's Playwright MCP server. It does not require a Codex account. A configured API model can request tools through the Python host; this component owns the actual tool connection.

Requires Node.js 20+ and npm. Install the locked packages, and install the isolated test browser when browser tools are wanted:

```sh
npm ci --ignore-scripts
node node_modules/playwright/cli.js install chromium --only-shell
node --test test/*.test.mjs
node --test test/browser.integration.mjs
```

The browser integration test uses a temporary local test website and an isolated headless browser. It signs into that fixture, submits a real form, checks the website's received data, tests an invalid selector, and saves results under `evidence/`. It does not use an existing browser profile or real login. This verifies the adapter and browser server, not the model's task planning.

## Host protocol

Run `node adapter.mjs`. Send one JSON object per stdin line. stdout contains only JSONL responses and notifications; server diagnostics go to stderr. Every request has a string or numeric `id`. A response is `{id,result}` or `{id,error:{message,code}}`.

```json
{"id":1,"method":"connect","workspace":"/absolute/task/workspace","server":{"command":"node","args":["/absolute/lancer-mcp-bridge/node_modules/@playwright/mcp/cli.js","--config","/absolute/lancer-mcp-bridge/browser.config.json"]},"timeoutMs":60000}
{"id":2,"method":"list"}
{"id":3,"method":"call","name":"browser_navigate","arguments":{"url":"https://example.com"},"timeoutMs":60000}
{"id":4,"method":"close"}
```

`connect` returns `serverInfo`, `capabilities`, `serverPid`, and the complete `tools` list. The host owns the command and arguments; models only choose tool names and arguments from the discovered tools. `server.cwd` defaults to the task workspace. Optional `server.env` values come from trusted host configuration, never from a model action. The SDK does not inherit the full host environment.

One adapter process owns one MCP session, retaining browser state across calls. `roots/list` returns that task workspace. A tools change notification is forwarded as `{"event":"tools_changed"}`; `list` performs a fresh discovery, including all pages. `call` returns the MCP tool result unchanged, including `content`, `structuredContent` and `isError`. Protocol failures return an error; tool business failures remain `isError` results. Neither is silently retried.

With the pinned Playwright release, action responses link to an automatically saved snapshot file. Call `browser_snapshot` without a filename to receive the full current accessibility snapshot inline before choosing the next target. The supplied browser config explicitly selects Chromium without a Chrome channel, so the smaller installed headless shell is sufficient.

The Python host starts this adapter in its own process group. Host cancellation terminates that group, including MCP descendants; interrupted side effects remain unknown until reconciled. Normal `close` asks the SDK to close the server, replies, then exits. EOF also closes the connection.

Only stdio and the SDK's default legacy initialization are supported here. HTTP/SSE, OAuth, sampling/elicitation approval interfaces, remote tool servers, and desktop Computer Use are not implemented. Playwright tools are real browser automation, not permission to operate arbitrary authenticated sites. Website-specific permissions belong in the host's task setup.

Pinned versions are `@modelcontextprotocol/client@2.4.0` and `@playwright/mcp@0.0.83`; the lockfile also fixes the latter's Playwright alpha dependency. Official references: [MCP client](https://ts.sdk.modelcontextprotocol.io/v2/get-started/first-client.html), [stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports), [Playwright MCP](https://github.com/microsoft/playwright-mcp).

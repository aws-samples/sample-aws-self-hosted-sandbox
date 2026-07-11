// 生成一个"演示用 web 页面"并在沙盒 guest 内起 python http server 的 exec 命令。
//
// 背景:沙盒 rootfs(min-rootfs)默认没有任何 web 服务 → 打开暴露端口是 404/连不上。
// 这里一键在 guest 里写一个好看的 index.html 并起 http.server,让端口暴露 demo 有内容可看。
//
// 两个实现要点(均来自真机踩坑):
//   1. guest exec 环境 PATH 不全,python3 必须用绝对路径 /usr/local/bin/python3。
//   2. HTML 经 base64 传入,避开多行/引号在 shell 里的转义地狱(base64 是 debian 自带)。

function demoHtml(sid: string, port: number): string {
  return `<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sandbox ${sid} · :${port}</title>
<style>
  * { box-sizing: border-box; margin: 0; }
  body {
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: radial-gradient(1200px 600px at 50% -10%, #1b1f3a, #0b0d10);
    color: #e6e9ef; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .card {
    background: rgba(20,23,28,.7); border: 1px solid #2c2f52; border-radius: 20px;
    padding: 44px 52px; max-width: 560px; text-align: center;
    box-shadow: 0 20px 80px rgba(0,0,0,.5);
  }
  .pulse {
    width: 14px; height: 14px; border-radius: 50%; background: #2ecc71; margin: 0 auto 20px;
    box-shadow: 0 0 0 0 rgba(46,204,113,.7); animation: p 1.8s infinite;
  }
  @keyframes p { 70% { box-shadow: 0 0 0 16px rgba(46,204,113,0); } 100% { box-shadow: 0 0 0 0 rgba(46,204,113,0); } }
  h1 { font-size: 26px; margin-bottom: 8px; }
  .grad { background: linear-gradient(135deg,#7c5cff,#3b9dff); -webkit-background-clip: text; background-clip: text; color: transparent; }
  p.sub { color: #8b93a1; margin-bottom: 26px; }
  .kv { display: grid; grid-template-columns: auto auto; gap: 8px 16px; justify-content: center;
        font-family: ui-monospace, Menlo, monospace; font-size: 13px; text-align: left; }
  .kv dt { color: #8b93a1; }
  .kv dd { color: #e6e9ef; margin: 0; }
  .foot { margin-top: 26px; color: #5c6472; font-size: 12px; }
</style>
</head>
<body>
  <div class="card">
    <div class="pulse"></div>
    <h1>🚀 <span class="grad">It works!</span></h1>
    <p class="sub">This page is served from inside a Firecracker microVM.</p>
    <dl class="kv">
      <dt>sandbox id</dt><dd>${sid}</dd>
      <dt>port</dt><dd>${port}</dd>
      <dt>served by</dt><dd>python3 -m http.server</dd>
      <dt>reached via</dt><dd>control-plane /s/${sid}/${port}/ proxy</dd>
    </dl>
    <div class="foot">AWS Self-Hosted Sandbox Platform · demo web</div>
  </div>
</body>
</html>`;
}

// UTF-8 安全的 base64(btoa 直接对含中文串会抛错)。
function b64utf8(s: string): string {
  return Buffer.from(s, "utf-8").toString("base64");
}

/** 返回在 guest 内写好 index.html 并后台起 http.server 的 shell 命令。 */
export function demoWebCommand(sid: string, port: number): string {
  const b64 = b64utf8(demoHtml(sid, port));
  const py = "/usr/local/bin/python3";
  // 先杀掉可能已在该端口的旧 server(幂等),再起新的。
  return (
    `mkdir -p /web && echo ${b64} | base64 -d > /web/index.html && cd /web && ` +
    `(setsid ${py} -m http.server ${port} >/tmp/web-${port}.log 2>&1 &) ; ` +
    `sleep 1; echo "demo web started on :${port}"`
  );
}

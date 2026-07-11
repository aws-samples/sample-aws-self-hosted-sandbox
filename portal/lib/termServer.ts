// 交互式 Web Terminal —— 在沙盒 guest 内起一个"PTY over WebSocket"服务,自带 xterm.js 页面。
//
// 设计:不改 node-agent、不重建 rootfs。guest 的 python3 自带 pty + 完整 stdlib,足以实现一个
// 极简 WebSocket 服务(握手 + 帧编解码)驱动一个 bash PTY。浏览器经端口暴露反代
// (/s/{id}/{port}/,已支持 WebSocket 透传)连上它,即得一个真实交互式终端。
//
// 页面 /  → xterm.js 终端;WS /ws → 后端 PTY。控制面/node-agent 只做字节透传,
// 由这个 guest 服务自己做 WS 帧解析。

const TERM_PY = String.raw`
import os, pty, select, socket, struct, base64, hashlib, threading, sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7681
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # WebSocket 握手魔术串

HTML = ("""<!doctype html><html><head><meta charset=utf-8><title>sandbox terminal</title>
<link rel=stylesheet href=https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css>
<script src=https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js></script>
<style>html,body{margin:0;height:100%;background:#0b0d10}#t{height:100%;padding:6px}</style>
</head><body><div id=t></div><script>
function boot(){
  if(typeof Terminal==='undefined'){return setTimeout(boot,50);}  // 等 CDN 就绪
  var term=new Terminal({fontSize:13,theme:{background:'#0b0d10'},cursorBlink:true});
  term.open(document.getElementById('t'));
  var proto=location.protocol==='https:'?'wss':'ws';
  var base=location.pathname.replace(/[/]$/,'');
  var ws=new WebSocket(proto+'://'+location.host+base+'/ws');
  ws.binaryType='arraybuffer';
  ws.onmessage=function(e){term.write(new Uint8Array(e.data))};
  term.onData(function(d){ws.send(d)});
  ws.onopen=function(){term.focus()};
  ws.onclose=function(){term.write('\\r\\n[connection closed]\\r\\n')};
}
boot();  // 立即启动(脚本在 body 末尾,DOM 已就绪;Terminal 未定义则轮询等待)
</script></body></html>""").encode("utf-8")


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def ws_send(conn, data):
    # server->client 二进制帧(不 mask),支持 <126 / 16bit / 64bit 长度
    n = len(data)
    if n < 126:
        hdr = struct.pack("!BB", 0x82, n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x82, 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x82, 127, n)
    conn.sendall(hdr + data)


def ws_recv_frames(conn, buf):
    # 解析 client->server 帧(必 mask),返回 (payloads, remaining_buf, closed)
    out = []
    while True:
        if len(buf) < 2:
            return out, buf, False
        b0, b1 = buf[0], buf[1]
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        idx = 2
        if ln == 126:
            if len(buf) < 4: return out, buf, False
            ln = struct.unpack("!H", buf[2:4])[0]; idx = 4
        elif ln == 127:
            if len(buf) < 10: return out, buf, False
            ln = struct.unpack("!Q", buf[2:10])[0]; idx = 10
        need = idx + (4 if masked else 0) + ln
        if len(buf) < need:
            return out, buf, False
        mask = buf[idx:idx+4] if masked else b"\x00\x00\x00\x00"
        idx += 4 if masked else 0
        payload = bytearray(buf[idx:idx+ln])
        for i in range(ln):
            payload[i] ^= mask[i % 4]
        buf = buf[need:]
        if opcode == 0x8:  # close
            return out, buf, True
        if opcode in (0x1, 0x2, 0x0):
            out.append(bytes(payload))


def handle(conn):
    req = b""
    while b"\r\n\r\n" not in req:
        c = conn.recv(4096)
        if not c: conn.close(); return
        req += c
    head = req.split(b"\r\n\r\n", 1)[0].decode(errors="replace")
    lines = head.split("\r\n")
    path = lines[0].split(" ")[1] if lines else "/"
    headers = {}
    for l in lines[1:]:
        if ":" in l:
            k, v = l.split(":", 1); headers[k.strip().lower()] = v.strip()

    if headers.get("upgrade", "").lower() != "websocket":
        # 普通 HTTP → 返回 xterm 页面
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                     b"Content-Length: " + str(len(HTML)).encode() + b"\r\n\r\n" + HTML)
        conn.close(); return

    # WebSocket 握手
    key = headers.get("sec-websocket-key", "")
    resp = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Accept: " + ws_accept(key) + "\r\n\r\n")
    conn.sendall(resp.encode())

    # 起 PTY 跑 bash
    pid, master = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.execv("/bin/bash", ["/bin/bash"])
        os._exit(1)

    buf = b""
    conn.setblocking(False)
    try:
        while True:
            r, _, _ = select.select([conn, master], [], [], 60)
            if conn in r:
                try:
                    d = conn.recv(65536)
                except BlockingIOError:
                    d = b""
                if d == b"":
                    # 可能是非阻塞暂时无数据;用 exception 区分不便,这里靠对端关闭时 recv 返回 b""
                    try:
                        peek = conn.recv(1, socket.MSG_PEEK)
                        if peek == b"": break
                    except BlockingIOError:
                        pass
                else:
                    buf += d
                    frames, buf, closed = ws_recv_frames(conn, buf)
                    for f in frames:
                        os.write(master, f)
                    if closed: break
            if master in r:
                try:
                    out = os.read(master, 65536)
                except OSError:
                    break
                if not out: break
                ws_send(conn, out)
    finally:
        try: os.close(master)
        except OSError: pass
        try: os.kill(pid, 9)
        except OSError: pass
        conn.close()


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    s.listen(8)
    print("term-server on", PORT, flush=True)
    while True:
        try:
            conn, _ = s.accept()
        except Exception:
            continue
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


main()
`.trim();

/** 返回在 guest 内写入 term-server.py 并后台启动的 shell 命令(端口默认 7681)。 */
export function termServerCommand(port = 7681): string {
  const b64 = Buffer.from(TERM_PY, "utf-8").toString("base64");
  const py = "/usr/local/bin/python3";
  return (
    `echo ${b64} | base64 -d > /tmp/term-server.py && ` +
    `(setsid ${py} /tmp/term-server.py ${port} >/tmp/term-${port}.log 2>&1 &) ; ` +
    `sleep 1; echo "terminal server on :${port}"`
  );
}

export const TERMINAL_PORT = 7681;

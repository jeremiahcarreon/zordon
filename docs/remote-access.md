# Remote access

Zordon listens on `127.0.0.1:8765` by default and nothing outside the machine can
reach it. To use it from a phone there are three routes, in the order to try them.
Whichever you pick, the browser needs the session token once; it is printed on first
run and again by `zordon token show`.

| Route | Command | For | Address | Microphone works on a phone |
| --- | --- | --- | --- | --- |
| Public tunnel | `zordon serve --tunnel` | Anyone, anywhere, no network setup | `https://<random>.trycloudflare.com`, changes each run | Yes (HTTPS) |
| Tailscale | `zordon serve --bind tailscale` | People who already run Tailscale | `http://100.x.y.z:8765`, stable | Only with HTTPS in front (Tailscale Serve) |
| LAN | `zordon serve --bind 0.0.0.0` | Same Wi-Fi, no phone microphone needed, or a desktop browser | `http://<lan ip>:8765` | No (plain HTTP) |

The microphone column matters: browsers only expose `getUserMedia` to a secure
context, which means HTTPS or `localhost`. A phone opening `http://192.168.1.20:8765`
or `http://100.101.102.103:8765` can see the transcript and use the text box and
the buttons, but Safari and Chrome will not grant microphone access and audio
playback may also be restricted. For voice from a phone, use the tunnel or put
HTTPS in front of the Tailscale address.

## 1. Public tunnel: `zordon serve --tunnel`

`--tunnel` starts a cloudflared quick tunnel as a child process:

```
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8765
```

cloudflared connects outward to Cloudflare, so there is no port forwarding, no
account and no domain. It logs to stderr; Zordon reads those lines until the first
match of `https://[a-z0-9-]+\.trycloudflare\.com`, then prints the URL and a QR code
in the terminal and sends it to connected clients as a `tunnel` message, so the
session picker shows the same QR. Scan it, type the token once, done. The tunnel
reaches the server over loopback, so the bind address stays `127.0.0.1`.

If `cloudflared` is not on `PATH`, `zordon serve --tunnel` downloads the latest
release into `~/.zordon/bin/` on first use (Linux amd64/arm64, macOS as a `.tgz`),
about 40 MB; `zordon doctor --download --tunnel` fetches it ahead of time.

What the tunnel gives you and does not:

* The URL is random and changes every run. Quick tunnels have no uptime guarantee,
  allow 200 in-flight requests, and do not support Server-Sent Events (Zordon uses a
  WebSocket, which is not listed as unsupported and works in practice).
* `--tunnel ngrok` (or `[tunnel] provider = "ngrok"` in `config.toml`) runs
  `ngrok http 8765` instead and reads the public URL from ngrok's local API at
  `http://127.0.0.1:4040/api/tunnels`. ngrok needs its own account and binary.
* If QUIC (UDP 7844) is blocked on your network, cloudflared's `--protocol http2`
  is the usual fallback; set it through `[tunnel]` once that option exists, or run
  cloudflared yourself against `127.0.0.1:8765`.

### What `--tunnel` enforces

A public URL changes the threat model, so three things the local modes only
recommend become requirements. Zordon turns them on itself; they cannot be turned
off while `--tunnel` is active.

| Requirement | Behaviour |
| --- | --- |
| Token required | `--tunnel` does not refuse to start without `server.token`, but `/auth` then refuses every login, so the public URL is unusable until `zordon token rotate` sets one; the startup log says so. |
| Rate limit on failed logins | 5 failed token attempts per minute per client IP, then `429` with a `Retry-After` header for the rest of the minute. The right token is also refused while the IP is limited. Every failure is logged. Behind the tunnel the TCP peer is always `127.0.0.1`, so the client IP is read from `CF-Connecting-IP` (or `X-Forwarded-For`); those headers are trusted whenever the TCP peer is loopback (tunnel mode or not), never from any other peer. |
| Idle disconnect | A WebSocket with no inbound message for 30 minutes (`server.idle_disconnect_minutes`) is closed with code 1000 and reason `idle`. Neither protocol-level pings nor the client's own `ping` keepalive count as activity; only audio, text, commands, calls and flush acks do. |
| Secure cookie | The session cookie is set with `Secure` (in addition to `HttpOnly` and `SameSite=Lax`), so it is never sent over plain HTTP. Without `--tunnel` it is also `Secure` when the request arrived over HTTPS, including `X-Forwarded-Proto: https` from a proxy on loopback. |

### A stable URL

A quick tunnel's hostname is disposable by design. For a URL that stays the same,
Cloudflare offers *named* tunnels on a domain you own (a free Cloudflare account and
a DNS record pointing at the tunnel). That setup lives in Cloudflare's dashboard and
`cloudflared tunnel login` / `cloudflared tunnel create`, not in Zordon; run the
named tunnel yourself against `http://127.0.0.1:8765` and start Zordon without
`--tunnel`. Note that without `--tunnel` the rate limit and idle disconnect are the
configured values rather than forced, and the cookie is `Secure` only when the
request arrived over HTTPS (your proxy must send `X-Forwarded-Proto: https` and
connect from loopback); keep `server.token` set and consider lowering
`server.idle_disconnect_minutes`.

## 2. Tailscale: `zordon serve --bind tailscale`

If the machine and the phone are both on your tailnet, bind to the machine's
Tailscale address. `--bind tailscale` runs `tailscale ip -4` and binds the first
address it prints; if the `tailscale` CLI is not on `PATH`, Zordon exits with
status 3. Pass the address yourself with `--bind 100.x.y.z` in that case. Either way
the address is not loopback, so `server.token` must be set, and the server refuses
to start otherwise.

The phone then opens `http://100.x.y.z:8765`, logs in with the token, and gets
the transcript, text input and buttons, but **not the microphone**, because the
page is plain HTTP. Two ways to fix that:

* **Tailscale Serve.** Tailscale can terminate HTTPS for you with a certificate for
  your machine's `*.ts.net` name and forward to a local port. Run Zordon on
  `127.0.0.1:8765` (the default), point `tailscale serve` at that port, and open
  the `https://<machine>.<tailnet>.ts.net` URL on the phone. See Tailscale's
  documentation for the exact `tailscale serve` syntax for your version. The
  server itself still sees plain HTTP on loopback; it marks the cookie `Secure`
  because the proxy connects from loopback and sends `X-Forwarded-Proto: https`.
* **Use the tunnel for voice**, and the Tailscale address for a laptop browser.

Tailscale traffic is encrypted end to end and the address is stable, so there is
no rate-limit or idle-disconnect requirement; the configured values apply.

## 3. LAN: `zordon serve --bind 0.0.0.0`

Binds every interface. Requires `server.token`; the server refuses to start without
one. Fine for a laptop on the same Wi-Fi, where the desktop browser treats
`http://<lan ip>` as insecure but you can still read the transcript, type, and
answer prompts. It is not a voice route for phones, for the microphone reason
above, and it exposes the login page to everything on the network, so prefer the
other two.

## Phone notes

* **Audio does not play until you tap the Talk button.** iOS and Android both
  require a user gesture before a page may produce sound. The Talk button creates
  the `AudioContext`, plays a silent buffer to unlock output, and requests the
  microphone, all inside that tap. Until then the transcript updates but nothing is
  heard.
* **Microphone needs HTTPS.** See above. `localhost` counts as secure, which is why
  everything works in a browser on the machine itself.
* **iOS Safari 14.5 or later, Android Chrome 66 or later.** The capture path uses
  an `AudioWorklet`. iOS ignores a requested sample rate; the worklet downsamples
  whatever the device gives (usually 48 kHz) to 16 kHz.
* **Backgrounding.** When the app goes to the background the page stops sending
  audio and keeps the socket; on return, tap Talk again if playback does not
  resume (iOS may report the audio context as `interrupted` after a call or Siri).
  iOS reportedly suspends page networking after roughly 30 s in the background, so the
  WebSocket may drop; the client reconnects with its cookie, no new login.
* **Phone calls and headsets** interrupt the audio context; the next tap resumes it.

## The token

```
zordon token show
```

prints `server.token` from `~/.zordon/config.toml`. The browser POSTs it once to
`/auth`, receives an `HttpOnly` session cookie (kept in memory on the server, so
restarting Zordon logs every client out), and every WebSocket upgrade checks the
cookie; without it the upgrade is refused with HTTP 403. To rotate the token, see
`security.md`.

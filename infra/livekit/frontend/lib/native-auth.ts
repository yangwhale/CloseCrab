import { createHmac, timingSafeEqual } from 'node:crypto';

/**
 * 原生 app 入口的验签。
 *
 * ## 为什么需要这一层
 *
 * 整个前端坐在 GCLB + IAP 后面，浏览器靠登录 cookie 过关，所以 `/api/token`
 * 一直是裸的（见那个路由里 ALLOW_INSECURE_TOKEN 的注释）。但 iOS 原生 app
 * 没有浏览器 cookie —— IAP 会把它的 token 请求和 WebSocket 握手一起弹到登录页。
 *
 * 解法是在 url-map 上开一条 `/native/*` 走非 IAP 的后端（跟已有的 `/assets/*`、
 * `/feishu/*` 同一个套路）。这条路一开，`/api/token` 就等于**对公网裸奔** ——
 * 谁都能换到一张进任意白名单房间的 token。所以这一层不是「顺手加的安全”，
 * 它是那条路能不能开的前提条件。
 *
 * ## 怎么判断「这是从 native 进来的」
 *
 * 看 `X-CC-Entry`，由 hk-jmp 的 Caddy 打。客户端伪造不了：Caddy 在两条入口上
 * 都**显式**处理了这个头 —— native 那条 set 成 `native`，IAP 那条 unset。
 * 它是唯一的入口，没有第三条路能带着这个头进来。
 *
 * 反过来说：**改 Caddy 的时候不能只加 native 那条而忘了 IAP 那条的 unset**，
 * 否则从浏览器那条路带一个自己写的 `X-CC-Entry: native` 进来，就绕过了 IAP
 * 的保护还顺带触发了这里的验签分支。
 *
 * ## 签的是什么
 *
 * `HMAC-SHA256(secret, "<scope>:<unix 秒>")`，十六进制小写。
 * scope 对 `/api/token` 是房间名，对 `/api/rooms` 是字面量 `rooms`。
 * 把 scope 签进去是为了让一张签名只能用在它申请的那个房间上 ——
 * 否则抓到一次 `?room=bunny` 的请求就能改成 `?room=jarvis` 重放。
 *
 * 时间戳只收 ±300 秒。手机和服务器的钟都走 NTP，五分钟足够宽松。
 */

const SECRET = process.env.CC_NATIVE_SECRET;
const SKEW_SECONDS = 300;

export type GateResult = { ok: true } | { ok: false; status: number; message: string };

export function isNativeEntry(req: Request): boolean {
  return req.headers.get('x-cc-entry') === 'native';
}

/**
 * 浏览器那条路直接放行（IAP 已经挡过了）；native 那条路必须验签。
 */
export function checkNativeAuth(req: Request, scope: string): GateResult {
  if (!isNativeEntry(req)) return { ok: true };

  // 没配密钥就把 native 入口整条关掉。**不要**退化成放行 ——
  // 「忘了配密钥」会伪装成「功能正常」，而代价是一个对公网敞开的发 token 接口。
  if (!SECRET) {
    return { ok: false, status: 503, message: 'CC_NATIVE_SECRET 未配置，native 入口关闭' };
  }

  const ts = req.headers.get('x-cc-ts');
  const sig = req.headers.get('x-cc-sig');
  if (!ts || !sig) return { ok: false, status: 401, message: '缺少 X-CC-Ts / X-CC-Sig' };

  const tsNum = Number(ts);
  if (!Number.isFinite(tsNum)) return { ok: false, status: 401, message: 'X-CC-Ts 不是数字' };
  if (Math.abs(Date.now() / 1000 - tsNum) > SKEW_SECONDS) {
    return { ok: false, status: 401, message: '时间戳超出 ±300 秒，检查设备时钟' };
  }

  const expected = createHmac('sha256', SECRET).update(`${scope}:${ts}`).digest('hex');
  // 比较前先看长度：timingSafeEqual 长度不等会直接抛，而不是返回 false。
  if (sig.length !== expected.length) return { ok: false, status: 403, message: '签名不匹配' };
  if (!timingSafeEqual(Buffer.from(sig), Buffer.from(expected))) {
    return { ok: false, status: 403, message: '签名不匹配' };
  }

  return { ok: true };
}

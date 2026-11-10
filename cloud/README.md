# Cloud dashboard

The Windows host connects out to a Cloudflare Worker. A signed-in browser can
view printer status and recent camera snapshots, and the installed web app can
receive background alerts. Printer settings, access codes, and printing stay
on the printer host. The cloud dashboard is for monitoring.

Only the owner's Google account can open it. The Worker signs you in with
Google itself, so you do not need Cloudflare Zero Trust or a payment card.

## One-time setup

1. In this folder, run `npm ci`, `npx wrangler login`, and `npm run deploy`.
   Save the `https://…workers.dev` address Wrangler shows.
2. Create a Google sign-in client (free, no card):
   1. In [Google Cloud Console](https://console.cloud.google.com), create a project.
   2. **APIs & Services → OAuth consent screen**: choose **External**, fill in
      the app name and your email. Leave it in **Testing** and add your Gmail
      address as a **test user**.
   3. **APIs & Services → Credentials → Create credentials → OAuth client ID**,
      type **Web application**. Under **Authorized redirect URIs**, add
      `https://<your-worker-address>/auth/callback`, for example
      `https://os3dpms.you.workers.dev/auth/callback`.
   4. Copy the **Client ID** and **Client secret**.
3. Run `npm run keys` once. Run `npx wrangler secret put NAME` separately for each
   of the following, pasting the corresponding value into the terminal prompt:

   | Name | Value |
   | --- | --- |
   | `OWNER_EMAIL` | Your Gmail address. Only this account can sign in. |
   | `GOOGLE_CLIENT_ID` | The client ID from step 2 |
   | `GOOGLE_CLIENT_SECRET` | The client secret from step 2 |
   | `CONNECTOR_KEY` | The connection key from `npm run keys` |
   | `VAPID_PUBLIC_KEY` | The public push key from `npm run keys` |
   | `VAPID_PRIVATE_KEY` | The private push key from `npm run keys` |

4. Open the Worker address, click **Sign in with Google**, and pick your
   account. You stay signed in on that device for 30 days. **Settings → Sign
   out** signs you out.
5. On the printer PC, open **Setup → Cloud dashboard**, paste the Worker
   address and connection key, then click **Connect**. Keep the host running.
   Test the cloud dashboard from your phone over mobile data.

Keep the connection key, client secret, and private push key in a password
manager, never in this folder. If you replace the push keys, subscribe again
from each device. A change to the host key requires updating it in the host's
Setup page. Replacing the Google client secret signs out every device.

Turn on 2-Step Verification for the owner Google account: whoever controls it
can see your printers.

### How sign-in is protected

- Google checks your password and 2-step code; the Worker never sees them.
- The sign-in uses OpenID Connect with PKCE, a one-time `state`, and a `nonce`.
  The Worker accepts only a verified email that matches `OWNER_EMAIL`.
- The session is an HMAC-signed, `HttpOnly`, `Secure`, `SameSite=Lax` cookie.
  Page scripts cannot read it, and it cannot be forged without the client secret.
- The printer host never uses Google. It connects to `/connector` and
  `/connector/live` with the connection key.
- The browser's live camera link (`/live`) needs the same sign-in and must come
  from the dashboard's own page (`Origin` check), so another website cannot open
  it with the owner's cookie.

### Cloudflare Access (optional)

If you already use Cloudflare Zero Trust, the Worker also accepts Access. Add a
self-hosted application for the Worker hostname with an **Allow** policy for
your email, a second application for the paths `/connector` and
`/connector/live` with a **Bypass** policy, and set the `ACCESS_AUD` secret to the
main application's Audience tag.

## Phone alerts

- **iPhone or iPad** on iOS/iPadOS 16.4 or later. Open the dashboard in Safari,
  choose **Share → Add to Home Screen**, then open it from the Home Screen.
- **Android**. Open it in Chrome and choose **Install app** or **Add to Home screen**.

In the dashboard, tap **Enable alerts** and choose **Allow**. A test alert is sent
right away. If the browser shows no question, click the bell or lock icon at the
left of the address bar and allow notifications. Choose which print events send
alerts in **Setup → Phone notifications** on the printer PC.

## Live cameras

While a camera card is on screen, the printer host sends that camera's newest
picture over one WebSocket (`/connector/live`, with the connection key), about
once a second, and the browser receives it over another (`/live`, signed in, same
site only). The browser asks for the next picture only after drawing the last one,
so a slow phone skips pictures instead of falling behind. A short gap keeps the
last picture on screen. If a WebSocket cannot connect, both sides fall back to one
request per picture.

WebSockets also keep the request count low: messages on an open WebSocket count
as a small fraction of a request, so Cloudflare's free quotas go much further than
with one request per picture. The host needs to remain online and able to reach
the printers.

After `npm run deploy`, Cloudflare can keep the running relay on the old code for
up to five minutes. Open live links then drop once and reconnect by themselves.

- [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect)
- [Cloudflare Durable Objects free quotas](https://developers.cloudflare.com/durable-objects/platform/pricing/)
- [Apple Home Screen web push](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)

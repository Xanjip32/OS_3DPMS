# Cloud dashboard

The Windows host connects out to a Cloudflare Worker. A signed-in browser can
view printer status and recent camera snapshots, and the installed web app can
receive background alerts. Printer settings, access codes, and printing stay
on the printer host. The cloud dashboard is for monitoring.

## One-time Cloudflare setup

1. In this folder, run `npm ci`, `npx wrangler login`, and `npm run deploy`.
   Save the `https://…workers.dev` address Wrangler shows.
2. Enable the **Zero Trust Free** plan in the Cloudflare dashboard if prompted.
   Cloudflare may ask for billing details during free-plan onboarding.
   In **Cloudflare Zero Trust → Access → Applications**, add a **Self-hosted**
   application for that exact hostname. Add an **Allow** policy for your email
   address. Enable an identity provider such as One-time PIN. Copy the
   application's **Audience (AUD) tag**.
3. Add a separate self-hosted application for the same hostname with the path
   `/connector`. Give that path a **Bypass** policy. The Worker checks the
   printer host's connection key on that route. Leave browser sign-in required
   on the main application. Use a hostname-based application for this setup.
4. Run `npm run keys` once. Run `npx wrangler secret put NAME` separately for each
   of the following, pasting the corresponding value into the terminal prompt:

   | Name | Value |
   | --- | --- |
   | `OWNER_EMAIL` | The exact email address in your Allow policy |
   | `ACCESS_AUD` | The main application’s Audience tag |
   | `CONNECTOR_KEY` | The connection key from `npm run keys` |
   | `VAPID_PUBLIC_KEY` | The public push key from `npm run keys` |
   | `VAPID_PRIVATE_KEY` | The private push key from `npm run keys` |

5. On the printer PC, open **Setup → Cloud dashboard**, paste the Worker
   address and connection key, then click **Connect**. Keep the host running.
   Test the cloud dashboard from your phone over mobile data.

Keep the connection key and private push key in a password manager. If you
replace the push keys, subscribe again from each device. A change to the host
key requires updating it in the host's Setup page.

## Phone alerts

- **iPhone or iPad** on iOS/iPadOS 16.4 or later. Open the dashboard in Safari,
  choose **Share → Add to Home Screen**, then open it from the Home Screen.
- **Android**. Open it in Chrome and choose **Install app** or **Add to Home screen**.

In the dashboard, tap **Enable alerts**, accept the notification prompt, then
tap **Test alerts**. Choose which print events send alerts in **Setup → Phone
notifications** on the printer PC.

The dashboard refreshes camera snapshots while you're viewing them. Cloudflare's
free quotas apply. The host needs to remain online and able to reach the printers.

- [Cloudflare Access for Workers](https://developers.cloudflare.com/workers/configuration/cloudflare-access/)
- [Cloudflare Durable Objects free quotas](https://developers.cloudflare.com/durable-objects/platform/pricing/)
- [Apple Home Screen web push](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)

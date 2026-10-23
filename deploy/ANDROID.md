# VerquoAI as an app: Android (Play Store), Mac, iPhone

The app is the terminal itself, installed as a Progressive Web App. On Android
it is packaged for the Play Store as a **Trusted Web Activity** (TWA): Chrome's
engine, full screen, no browser bar. Every feature of the web terminal is in the
app, and every server deploy updates the app at once - a new Play release is only
needed when the name, icon or shortcuts change.

What the app adds on top of the web page (`static/sw.js`, `static/js/pwa.js`,
`src/notify/push.py`):

- install on the home screen / Dock, app shortcuts (Market Flow, Day Trade, Positions, Orders);
- push alerts even when the app is closed: fills and closes, risk (failed exits, no stop at the
  broker, kill switch, rejected orders), Day Trade signals (off by default) - chosen per device
  under **Alerts** in the header;
- an offline screen instead of a browser error, and an instant reconnect of prices when the
  phone wakes;
- static files served from the device (the terminal opens in a fraction of a second);
- Android back button closes pop-ups and steps back through tabs.

Security: the TWA is Chrome itself (sandbox, Safe Browsing, auto-updates, no JavaScript
bridge to native code). Pages, prices, orders and the API are never cached on the device.
Alerts are end-to-end encrypted, go only to the browser vendors' push services, never show
amounts on the lock screen, and are removed when the user signs out.

## 1. Deploy the server part

```bash
sudo -u verquo git -C /opt/verquoai/app pull --ff-only
sudo -u verquo /opt/verquoai/venv/bin/pip install -r /opt/verquoai/app/requirements.txt   # adds pywebpush
systemctl restart verquoai
```

The push key is created on first use at `/opt/verquoai/app/data/push/vapid_private.pem`
(mode 600). **Back it up** with the rest of `data/`: a new key makes every device
re-subscribe (the app does that by itself on its next start).

Optional in `.env`: `PUSH_CONTACT_EMAIL` and `PRIVACY_CONTACT_EMAIL` (both fall back to
`ADMIN_EMAIL`).

Check: `https://verquoai.com/manifest.webmanifest`, `https://verquoai.com/sw.js`,
`https://verquoai.com/privacy`.

## 2. Install it now (no store)

- **Android**: open `https://verquoai.com/app` in Chrome → menu → *Install app*.
- **Mac**: Chrome / Edge → the install icon in the address bar; Safari (macOS 14+) → File → *Add to Dock*.
- **iPhone**: Safari → Share → *Add to Home Screen* (alerts work from iOS 16.4).

Then **Alerts** in the header → choose categories → *Save* → *Send a test*.

## 3. Build the Play Store bundle

Pick one.

**A. PWABuilder (nothing to install).** Go to <https://www.pwabuilder.com>, enter
`https://verquoai.com/app`, *Package for stores* → *Android* → *Google Play*:
package ID `com.verquoai.app`, app name `VerquoAI Trading Terminal`, launcher name
`VerquoAI`, notification delegation on. Download the zip: it holds the `.aab` to upload,
the **signing key** (keep it and its passwords safe - every update must be signed with it),
and an `assetlinks.json` (not needed: the server publishes its own, step 4).

**B. Bubblewrap on this Mac.** It downloads its own Java and Android SDK (~1.5 GB) the
first time.

```bash
npm i -g @bubblewrap/cli
cd android                       # holds twa-manifest.json (package, colours, shortcuts)
bubblewrap init --manifest https://verquoai.com/manifest.webmanifest   # accept the values from twa-manifest.json
bubblewrap build                 # creates app-release-bundle.aab and, the first time, verquoai-upload.keystore
```

The keystore and bundles are git-ignored. Store the keystore and its passwords outside the
repo (a password manager).

## 4. Play Console

1. Create the app (`com.verquoai.app`), keep **Play App Signing** on, and upload the `.aab`
   to the **Internal testing** track first.
2. *Setup → App integrity → App signing*: copy the **SHA-256** of the *app signing key*
   (and of the *upload key*). Put both in `/opt/verquoai/app/.env`:
   ```
   ANDROID_PACKAGE_NAME=com.verquoai.app
   ANDROID_CERT_SHA256=AA:BB:...:FF,11:22:...:99
   ```
   `systemctl restart verquoai`, then check
   `https://verquoai.com/.well-known/assetlinks.json` and Google's tester:
   <https://developers.google.com/digital-asset-links/tools/generator>.
   Without this the app opens with a browser bar.
3. Store listing: icon `static/img/icon-512.png`, screenshots `static/img/screen-*.png`
   (or take your own from a live account), feature graphic 1024×500, privacy policy
   `https://verquoai.com/privacy`.
4. App content: *Data safety* (collected: email, user ID, financial info - trading
   activity; encrypted in transit; deletion on request), *Financial features* declaration
   (trading / investment tools; not a broker), ads: none, target audience 18+.
5. Install from the internal track on a phone, confirm: full screen with no address bar,
   login, the broker login and back, alerts, back button. Then promote to production.

## Updates

- Terminal changes: deploy the server - the app is updated on its next start.
- Name, icon, colours or shortcuts: change `android/twa-manifest.json` (raise
  `appVersionCode`), rebuild the bundle with the same signing key, upload it.

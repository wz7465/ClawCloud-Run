import os
import time
import re
import base64
import requests
import pyotp
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright


LOGIN_ENTRY_URL = "https://console.run.claw.cloud/login"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"

TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))


# ============================================================
# Telegram
# ============================================================

class Telegram:

    def __init__(self):
        self.token = os.environ.get("TG_BOT_TOKEN")
        self.chat_id = os.environ.get("TG_CHAT_ID")
        self.ok = bool(self.token and self.chat_id)

    def send(self, msg):

        if not self.ok:
            return

        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": msg,
                    "parse_mode": "HTML"
                },
                timeout=20
            )
        except:
            pass


# ============================================================
# Secret 更新
# ============================================================

class SecretUpdater:

    def __init__(self):
        self.token = os.environ.get("REPO_TOKEN")
        self.repo = os.environ.get("GITHUB_REPOSITORY")
        self.ok = bool(self.token and self.repo)

    def update(self, name, value):

        if not self.ok:
            return False

        try:

            from nacl import encoding, public

            headers = {
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github+json"
            }

            r = requests.get(
                f"https://api.github.com/repos/{self.repo}/actions/secrets/public-key",
                headers=headers
            )

            if r.status_code != 200:
                return False

            data = r.json()

            pk = public.PublicKey(data["key"].encode(), encoding.Base64Encoder())

            sealed = public.SealedBox(pk)

            encrypted = sealed.encrypt(value.encode())

            encrypted_value = base64.b64encode(encrypted).decode()

            r = requests.put(
                f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                headers=headers,
                json={
                    "encrypted_value": encrypted_value,
                    "key_id": data["key_id"]
                }
            )

            return r.status_code in [201, 204]

        except:
            return False


# ============================================================
# 主类
# ============================================================

class AutoLogin:

    def __init__(self):

        self.username = os.environ.get("GH_USERNAME")
        self.password = os.environ.get("GH_PASSWORD")
        self.session = os.environ.get("GH_SESSION", "").strip()

        self.tg = Telegram()
        self.secret = SecretUpdater()

        self.region = None


    def log(self, msg):
        print(msg)


# ============================================================
# Passkey bypass
# ============================================================

    def bypass_passkey(self, page):

        try:

            if "passkey" not in page.content().lower():
                return

            selectors = [

                'button:has-text("Try another way")',
                'button:has-text("More options")',
                'button:has-text("Use authenticator app")',
                'button:has-text("Use your password")'

            ]

            for sel in selectors:

                btn = page.locator(sel).first

                if btn.is_visible(timeout=2000):

                    btn.click()

                    time.sleep(2)

        except:
            pass


# ============================================================
# OTP 提交
# ============================================================

    def submit_otp(self, page, code):

        selectors = [

            'input[data-target="two-factor-authentication.totpCode"]',
            'input[autocomplete="one-time-code"]',
            'input[name="app_otp"]',
            'input[name="otp"]'

        ]

        for sel in selectors:

            try:

                el = page.locator(sel).first

                if el.is_visible(timeout=3000):

                    el.fill(code)

                    page.keyboard.press("Enter")

                    page.wait_for_timeout(4000)

                    if "two-factor" not in page.url:
                        return True

            except:
                pass

        return False


# ============================================================
# 自动 TOTP
# ============================================================

    def handle_2fa(self, page):

        secret = os.environ.get("GH_2FA_SECRET")

        if not secret:
            return False

        totp = pyotp.TOTP(secret.replace(" ", "").strip())

        for _ in range(3):

            code = totp.now()

            self.log(f"TOTP: {code}")

            if self.submit_otp(page, code):

                return True

            time.sleep(5)

        return False


# ============================================================
# OAuth
# ============================================================

    def oauth(self, page):

        try:

            btn = page.locator('button[name="authorize"], button:has-text("Authorize")').first

            if btn.is_visible(timeout=3000):

                btn.click()

                page.wait_for_timeout(3000)

        except:
            pass


# ============================================================
# 登录流程
# ============================================================

    def run(self):

        if not self.username or not self.password:

            self.tg.send("❌ 缺少 GitHub 凭据")

            return


        self.tg.send("🚀 开始 ClawCloud 自动登录")


        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox"]
            )

            context = browser.new_context()

            page = context.new_page()

            page.goto(SIGNIN_URL)

            page.wait_for_timeout(3000)


            page.locator('button:has-text("GitHub")').first.click()

            page.wait_for_timeout(4000)


            if "github.com/login" in page.url:

                page.locator('input[name="login"]').fill(self.username)

                page.locator('input[name="password"]').fill(self.password)

                page.locator('input[type="submit"]').first.click()

                page.wait_for_timeout(4000)


                self.bypass_passkey(page)


                if "two-factor" in page.url:

                    if not self.handle_2fa(page):

                        self.tg.send("❌ GitHub 2FA 失败")

                        return


            if "oauth" in page.url:

                self.oauth(page)


            for _ in range(60):

                url = page.url

                if "claw.cloud" in url and "signin" not in url:

                    parsed = urlparse(url)

                    self.region = parsed.netloc

                    break

                time.sleep(1)


            if not self.region:

                self.tg.send("❌ 登录失败：未跳转到 ClawCloud")

                return


            cookie = None

            for c in context.cookies():

                if c["name"] == "user_session":

                    cookie = c["value"]


            if cookie:

                if self.secret.update("GH_SESSION", cookie):

                    self.tg.send("🔑 GH_SESSION 已自动更新")


            self.tg.send(f"✅ 登录成功\nRegion: {self.region}")

            browser.close()


# ============================================================

if __name__ == "__main__":
    AutoLogin().run()

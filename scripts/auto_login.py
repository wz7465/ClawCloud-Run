# ============================================================
# ClawCloud 自动登录脚本（稳定修复版）
# ============================================================

import os
import time
import re
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

    def send(self, text):

        if not self.ok:
            return

        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": text},
                timeout=20
            )
        except:
            pass

    def wait_code(self, timeout=120):

        if not self.ok:
            return None

        start = time.time()

        pattern = re.compile(r"/code\s+(\d+)")

        while time.time() - start < timeout:

            try:

                r = requests.get(
                    f"https://api.telegram.org/bot{self.token}/getUpdates",
                    timeout=20
                )

                data = r.json()

                for i in data.get("result", []):

                    msg = i.get("message", {})
                    text = msg.get("text", "")

                    m = pattern.search(text)

                    if m:
                        return m.group(1)

            except:
                pass

            time.sleep(5)

        return None


# ============================================================
# AutoLogin
# ============================================================

class AutoLogin:

    def __init__(self):

        self.username = os.environ.get("GH_USERNAME")
        self.password = os.environ.get("GH_PASSWORD")
        self.secret = os.environ.get("GH_2FA_SECRET", "").replace(" ", "").strip()

        self.tg = Telegram()

        self.logs = []

        self.region = None


    def log(self, text):

        print(text)
        self.logs.append(text)


# ============================================================
# Passkey bypass
# ============================================================

    def try_bypass_passkey(self, page):

        try:

            html = page.content().lower()

            if "passkey" not in html:
                return False

            selectors = [

                'button:has-text("More options")',
                'button:has-text("Try another way")',
                'button:has-text("Use authenticator app")',
                'button:has-text("Use your password")'

            ]

            for sel in selectors:

                btn = page.locator(sel).first

                if btn.is_visible(timeout=2000):

                    btn.click()
                    time.sleep(2)

            return True

        except:
            return False


# ============================================================
# TOTP submit
# ============================================================

    def submit_otp(self, page, code):

        selectors = [

            'input[data-target="two-factor-authentication.totpCode"]',
            'input[autocomplete="one-time-code"]',
            'input[name="app_otp"]',
            'input[name="otp"]',
            'input[inputmode="numeric"]'

        ]

        for sel in selectors:

            try:

                el = page.locator(sel).first

                if el.is_visible(timeout=3000):

                    el.fill(code)

                    time.sleep(1)

                    page.keyboard.press("Enter")

                    page.wait_for_timeout(4000)

                    if "two-factor" not in page.url:
                        return True

            except:
                pass

        return False


# ============================================================
# Handle 2FA
# ============================================================

    def handle_2fa(self, page):

        self.log("需要输入 GitHub 2FA")

        if self.secret:

            try:

                totp = pyotp.TOTP(self.secret)

                for _ in range(3):

                    code = totp.now()

                    self.log(f"TOTP: {code}")

                    if self.submit_otp(page, code):

                        self.log("TOTP 成功")
                        return True

                    time.sleep(5)

            except Exception as e:

                self.log(str(e))


        self.tg.send("发送 /code 123456")

        code = self.tg.wait_code(TWO_FACTOR_WAIT)

        if not code:
            return False

        return self.submit_otp(page, code)


# ============================================================
# OAuth authorize
# ============================================================

    def oauth(self, page):

        if "github.com/login/oauth/authorize" in page.url:

            btn = page.locator('button[name="authorize"]').first

            if btn.is_visible():

                btn.click()
                page.wait_for_timeout(3000)


# ============================================================
# Wait redirect
# ============================================================

    def wait_redirect(self, page):

        for _ in range(60):

            url = page.url

            if "claw.cloud" in url and "signin" not in url:

                parsed = urlparse(url)
                self.region = parsed.netloc

                return True

            if "oauth" in url:
                self.oauth(page)

            time.sleep(1)

        return False


# ============================================================
# Main run
# ============================================================

    def run(self):

        if not self.username or not self.password:

            print("缺少 GitHub 凭据")
            return


        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled"
                ]
            )

            context = browser.new_context(
                viewport={"width": 1920, "height": 1080}
            )

            page = context.new_page()

            page.goto(SIGNIN_URL)

            page.wait_for_timeout(3000)


            page.locator('button:has-text("GitHub")').first.click()

            page.wait_for_timeout(3000)


            if "github.com" in page.url:

                page.locator('input[name="login"]').fill(self.username)
                page.locator('input[name="password"]').fill(self.password)

                page.locator('input[type="submit"]').first.click()

                page.wait_for_timeout(3000)


                self.try_bypass_passkey(page)


                if "two-factor" in page.url or "sessions/two-factor" in page.url:

                    if not self.handle_2fa(page):

                        print("2FA 失败")
                        return


            if not self.wait_redirect(page):

                print("重定向失败")
                return


            print("登录成功")
            print("Region:", self.region)

            browser.close()


# ============================================================
# Entry
# ============================================================

if __name__ == "__main__":
    AutoLogin().run()

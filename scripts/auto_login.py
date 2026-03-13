# ============================================================
#  ClawCloud 自动登录脚本（优化重写版）
#  - 自动 GitHub 登录
#  - 自动 TOTP（GH_2FA_SECRET）
#  - fallback Telegram 验证
#  - 自动区域检测
#  - 自动更新 GH_SESSION Secret
#  - 同步 Playwright（sync_playwright）
# ============================================================

import os
import re
import time
import base64
import requests
import pyotp
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# -----------------------------
# 配置
# -----------------------------
LOGIN_ENTRY_URL = "https://console.run.claw.cloud/login"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"

DEVICE_VERIFY_WAIT = 30
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))

# ============================================================
# Telegram 工具
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
                data={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=20
            )
        except:
            pass

    def photo(self, path, caption=""):
        if not self.ok or not os.path.exists(path):
            return
        try:
            with open(path, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption[:1024]},
                    files={"photo": f},
                    timeout=30
                )
        except:
            pass

    def flush_updates(self):
        if not self.ok:
            return 0
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"timeout": 0},
                timeout=10
            )
            data = r.json()
            if data.get("ok") and data.get("result"):
                return data["result"][-1]["update_id"] + 1
        except:
            pass
        return 0

    def wait_code(self, timeout=120):
        if not self.ok:
            return None

        offset = self.flush_updates()
        deadline = time.time() + timeout
        pattern = re.compile(r"^/code\s+(\d{6,8})$")

        while time.time() < deadline:
            try:
                r = requests.get(
                    f"https://api.telegram.org/bot{self.token}/getUpdates",
                    params={"timeout": 20, "offset": offset},
                    timeout=30
                )
                data = r.json()
                if not data.get("ok"):
                    continue

                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    chat = msg.get("chat") or {}

                    if str(chat.get("id")) != str(self.chat_id):
                        continue

                    text = (msg.get("text") or "").strip()
                    m = pattern.match(text)
                    if m:
                        return m.group(1)

            except:
                pass

        return None

# ============================================================
# GitHub Secret 自动更新
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
                "Accept": "application/vnd.github.v3+json"
            }

            # 获取公钥
            r = requests.get(
                f"https://api.github.com/repos/{self.repo}/actions/secrets/public-key",
                headers=headers,
                timeout=20
            )
            if r.status_code != 200:
                return False

            key_data = r.json()
            pk = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
            encrypted = public.SealedBox(pk).encrypt(value.encode())

            # 更新 Secret
            r = requests.put(
                f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                headers=headers,
                json={
                    "encrypted_value": base64.b64encode(encrypted).decode(),
                    "key_id": key_data["key_id"]
                },
                timeout=20
            )
            return r.status_code in [201, 204]

        except Exception:
            return False
# ============================================================
# AutoLogin 主类（核心逻辑）
# ============================================================

class AutoLogin:
    def __init__(self):
        # GitHub 凭据
        self.username = os.environ.get("GH_USERNAME")
        self.password = os.environ.get("GH_PASSWORD")
        self.gh_session = os.environ.get("GH_SESSION", "").strip()

        # 工具
        self.tg = Telegram()
        self.secret = SecretUpdater()

        # 运行状态
        self.logs = []
        self.shots = []
        self.n = 0

        # 区域信息
        self.detected_region = None
        self.region_base_url = None

    # -----------------------------
    # 日志系统
    # -----------------------------
    def log(self, msg, level="INFO"):
        icons = {
            "INFO": "ℹ️",
            "SUCCESS": "✅",
            "ERROR": "❌",
            "WARN": "⚠️",
            "STEP": "🔹"
        }
        line = f"{icons.get(level, '•')} {msg}"
        print(line)
        self.logs.append(line)

    # -----------------------------
    # 截图工具
    # -----------------------------
    def shot(self, page, name):
        self.n += 1
        filename = f"{self.n:02d}_{name}.png"
        try:
            page.screenshot(path=filename)
            self.shots.append(filename)
        except:
            pass
        return filename

    # -----------------------------
    # 点击工具（多个 selector 轮询）
    # -----------------------------
    def click(self, page, selectors, desc=""):
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    el.click()
                    self.log(f"已点击: {desc}", "SUCCESS")
                    return True
            except:
                pass
        return False

    # -----------------------------
    # 区域检测
    # -----------------------------
    def detect_region(self, url):
        """
        从 URL 中检测区域信息，例如:
        https://ap-southeast-1.console.claw.cloud/... → ap-southeast-1
        """
        try:
            parsed = urlparse(url)
            host = parsed.netloc

            # 典型区域格式：{region}.console.claw.cloud
            if host.endswith(".console.claw.cloud"):
                region = host.replace(".console.claw.cloud", "")
                if region and region != "console":
                    self.detected_region = region
                    self.region_base_url = f"https://{host}"
                    self.log(f"检测到区域: {region}", "SUCCESS")
                    return region

            # fallback：使用当前域名
            self.region_base_url = f"{parsed.scheme}://{parsed.netloc}"
            self.log(f"未检测到区域，使用当前域名: {self.region_base_url}", "INFO")
            return None

        except Exception as e:
            self.log(f"区域检测异常: {e}", "WARN")
            return None

    # -----------------------------
    # 获取基础 URL（区域优先）
    # -----------------------------
    def get_base_url(self):
        return self.region_base_url or LOGIN_ENTRY_URL

    # -----------------------------
    # 提取 GitHub Session Cookie
    # -----------------------------
    def get_session(self, context):
        try:
            for c in context.cookies():
                if c["name"] == "user_session" and "github" in c.get("domain", ""):
                    return c["value"]
        except:
            pass
        return None

    # -----------------------------
    # 保存 Cookie（并自动更新 Secret）
    # -----------------------------
    def save_cookie(self, value):
        if not value:
            return

        self.log(f"新 Cookie: {value[:15]}...{value[-8:]}", "SUCCESS")

        if self.secret.update("GH_SESSION", value):
            self.log("已自动更新 GH_SESSION", "SUCCESS")
            self.tg.send("🔑 <b>Cookie 已自动更新</b>")
        else:
            self.tg.send(
                f"🔑 <b>新 Cookie</b>\n\n"
                f"<tg-spoiler>{value}</tg-spoiler>"
            )
            self.log("已通过 Telegram 发送 Cookie", "SUCCESS")
# ============================================================
    # GitHub 登录（核心逻辑）
    # ============================================================

    def login_github(self, page, context):
        self.log("开始 GitHub 登录流程", "STEP")
        self.shot(page, "github_login_page")

        # 输入用户名密码
        try:
            page.locator('input[name="login"]').fill(self.username)
            page.locator('input[name="password"]').fill(self.password)
            self.log("已输入 GitHub 凭据", "SUCCESS")
        except Exception as e:
            self.log(f"输入凭据失败: {e}", "ERROR")
            return False

        self.shot(page, "github_credentials_filled")

        # 点击登录
        try:
            page.locator('input[type="submit"], button[type="submit"]').first.click()
        except:
            pass

        time.sleep(2)
        page.wait_for_load_state("networkidle", timeout=30000)
        self.shot(page, "github_after_submit")

        # 设备验证
        if "verified-device" in page.url or "device-verification" in page.url:
            if not self.wait_device(page):
                return False
            time.sleep(2)
            page.wait_for_load_state("networkidle", timeout=30000)
            
    def bypass_passkey(self, page):
    """自动跳过 GitHub Passkey 登录界面"""
    self.log("检测是否为 Passkey 页面", "INFO")

    selectors = [
        'button:has-text("Use a different verification method")',
        'button:has-text("Try another way")',
        'a:has-text("Use a different verification method")',
        'a:has-text("Try another way")'
    ]

    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                self.log("检测到 Passkey 页面，正在跳过", "WARN")
                el.click()
                time.sleep(2)
                page.waitforload_state("networkidle", timeout=30000)
                self.shot(page, "bypass_passkey")
                return True
        except:
            pass

    return False
         
        # 先尝试跳过 Passkey
        self.bypass_passkey(page)
         
        # 两步验证
        if "two-factor" in page.url:
            self.log("检测到 GitHub 两步验证", "WARN")
            self.shot(page, "github_2fa")

            # GitHub Mobile Approve
            if "two-factor/mobile" in page.url:
                if not self.wait_two_factor_mobile(page):
                    return False
                time.sleep(2)
                page.wait_for_load_state("networkidle", timeout=30000)

            else:
                # TOTP / 恢复码
                if not self.handle_2fa_code_input(page):
                    return False
                time.sleep(2)
                page.wait_for_load_state("networkidle", timeout=30000)

        # 检查错误
        try:
            err = page.locator(".flash-error").first
            if err.is_visible(timeout=2000):
                self.log(f"GitHub 错误: {err.inner_text()}", "ERROR")
                return False
        except:
            pass

        self.log("GitHub 登录成功", "SUCCESS")
        return True

    # ============================================================
    # 自动 TOTP + fallback Telegram
    # ============================================================

    def handle_2fa_code_input(self, page):
        """自动 TOTP（GH_2FA_SECRET）→ fallback Telegram"""

        self.log("需要输入 GitHub TOTP 验证码", "WARN")
        shot = self.shot(page, "github_2fa_code")

        # -----------------------------
        # 自动 TOTP（优先）
        # -----------------------------
        secret = os.environ.get("GH_2FA_SECRET")

        if secret:
            try:
                totp = pyotp.TOTP(secret.replace(" ", ""))
                otp_code = totp.now()
                self.log(f"自动生成 TOTP: {otp_code}", "SUCCESS")

                if self._submit_otp(page, otp_code):
                    self.log("自动 TOTP 验证成功", "SUCCESS")
                    self.tg.send("🔐 自动 TOTP 验证成功")
                    return True

                self.log("自动 TOTP 验证失败，切换 Telegram", "WARN")

            except Exception as e:
                self.log(f"TOTP 计算失败: {e}", "ERROR")

        else:
            self.log("未配置 GH_2FA_SECRET，无法自动 TOTP", "WARN")

        # -----------------------------
        # fallback：Telegram 输入验证码
        # -----------------------------
        self.tg.send(
            f"🔐 <b>需要验证码登录</b>\n\n"
            f"请发送：<code>/code 123456</code>\n"
            f"等待 {TWO_FACTOR_WAIT} 秒"
        )
        if shot:
            self.tg.photo(shot, "两步验证页面")

        code = self.tg.wait_code(timeout=TWO_FACTOR_WAIT)

        if not code:
            self.log("等待 Telegram 验证码超时", "ERROR")
            self.tg.send("❌ 验证码超时")
            return False

        self.log("收到 Telegram 验证码，正在提交", "SUCCESS")
        self.tg.send("🔐 正在提交验证码")

        if self._submit_otp(page, code):
            self.log("Telegram 验证码验证成功", "SUCCESS")
            self.tg.send("✅ 验证成功")
            return True

        self.log("验证码错误", "ERROR")
        self.tg.send("❌ 验证码错误")
        return False

    # -----------------------------
    # 提交 OTP（自动识别输入框 + 自动提交）
    # -----------------------------
    def _submit_otp(self, page, code):
        selectors = [
            'input[autocomplete="one-time-code"]',
            'input[name="app_otp"]',
            'input[name="otp"]',
            'input#app_totp',
            'input#otp',
            'input[inputmode="numeric"]'
        ]

        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=2000):
                    el.fill(code)
                    time.sleep(1)

                    # 点击 Verify
                    verify_btns = [
                        'button:has-text("Verify")',
                        'button[type="submit"]',
                        'input[type="submit"]'
                    ]
                    submitted = False

                    for btn_sel in verify_btns:
                        try:
                            btn = page.locator(btn_sel).first
                            if btn.is_visible(timeout=1000):
                                btn.click()
                                submitted = True
                                break
                        except:
                            pass

                    if not submitted:
                        page.keyboard.press("Enter")

                    time.sleep(3)
                    page.wait_for_load_state("networkidle", timeout=30000)
                    self.shot(page, "otp_submitted")

                    # 判断是否通过
                    if "github.com/sessions/two-factor/" not in page.url:
                        return True
                    return False

            except:
                pass

        return False
# ============================================================
    # OAuth 授权
    # ============================================================

    def oauth(self, page):
        if "github.com/login/oauth/authorize" in page.url:
            self.log("处理 OAuth 授权", "STEP")
            self.shot(page, "oauth_page")

            self.click(
                page,
                ['button[name="authorize"]', 'button:has-text("Authorize")'],
                "Authorize"
            )

            time.sleep(2)
            page.wait_for_load_state("networkidle", timeout=30000)

    # ============================================================
    # 等待重定向到 ClawCloud（并自动检测区域）
    # ============================================================

    def wait_redirect(self, page, wait=60):
        self.log("等待重定向到 ClawCloud...", "STEP")

        for i in range(wait):
            url = page.url

            # 已跳转到 ClawCloud
            if "claw.cloud" in url and "signin" not in url.lower():
                self.log("重定向成功", "SUCCESS")
                self.detect_region(url)
                return True

            # 仍在 OAuth 页面
            if "github.com/login/oauth/authorize" in url:
                self.oauth(page)

            time.sleep(1)
            if i % 10 == 0:
                self.log(f"  等待... ({i}/{wait} 秒)")

        self.log("重定向超时", "ERROR")
        return False

    # ============================================================
    # 保活（访问区域控制台）
    # ============================================================

    def keepalive(self, page):
        self.log("开始保活流程", "STEP")

        base = self.get_base_url()
        self.log(f"使用区域 URL: {base}", "INFO")

        pages = [
            (f"{base}/", "控制台首页"),
            (f"{base}/apps", "应用列表")
        ]

        for url, name in pages:
            try:
                page.goto(url, timeout=30000)
                page.wait_for_load_state("networkidle", timeout=15000)
                self.log(f"已访问: {name}", "SUCCESS")

                # 再次检测区域（以防跳转）
                self.detect_region(page.url)

                time.sleep(1)

            except Exception as e:
                self.log(f"访问 {name} 失败: {e}", "WARN")

        self.shot(page, "keepalive_done")

    # ============================================================
    # Telegram 通知
    # ============================================================

    def notify(self, ok, err=""):
        if not self.tg.ok:
            return

        region_info = (
            f"\n<b>区域:</b> {self.detected_region}"
            if self.detected_region else ""
        )

        msg = (
            f"<b>🤖 ClawCloud 自动登录</b>\n\n"
            f"<b>状态:</b> {'✅ 成功' if ok else '❌ 失败'}\n"
            f"<b>用户:</b> {self.username}{region_info}\n"
            f"<b>时间:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        if err:
            msg += f"\n<b>错误:</b> {err}"

        msg += "\n\n<b>日志:</b>\n" + "\n".join(self.logs[-6:])

        self.tg.send(msg)

        # 发送截图
        if self.shots:
            if not ok:
                for s in self.shots[-3:]:
                    self.tg.photo(s, s)
            else:
                self.tg.photo(self.shots[-1], "完成")

    # ============================================================
    # 主流程
    # ============================================================

    def run(self):
        print("\n" + "=" * 50)
        print("🚀 ClawCloud 自动登录（优化重写版）")
        print("=" * 50 + "\n")

        self.log(f"用户名: {self.username}")
        self.log(f"Session: {'有' if self.gh_session else '无'}")
        self.log(f"密码: {'有' if self.password else '无'}")
        self.log(f"登录入口: {LOGIN_ENTRY_URL}")

        if not self.username or not self.password:
            self.log("缺少 GitHub 凭据", "ERROR")
            self.notify(False, "凭据未配置")
            return

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox"]
            )
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            )
            page = context.new_page()

            try:
                # 预加载 Cookie
                if self.gh_session:
                    try:
                        context.add_cookies([
                            {
                                "name": "user_session",
                                "value": self.gh_session,
                                "domain": "github.com",
                                "path": "/"
                            },
                            {
                                "name": "logged_in",
                                "value": "yes",
                                "domain": "github.com",
                                "path": "/"
                            }
                        ])
                        self.log("已加载 GitHub Session Cookie", "SUCCESS")
                    except:
                        self.log("加载 Cookie 失败", "WARN")

                # Step 1: 打开 ClawCloud 登录页
                self.log("步骤 1：打开 ClawCloud 登录页", "STEP")
                page.goto(SIGNIN_URL, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=60000)
                time.sleep(1)
                self.shot(page, "clawcloud_login_page")

                # Step 2: 点击 GitHub 登录
                self.log("步骤 2：点击 GitHub 登录", "STEP")
                if not self.click(
                    page,
                    [
                        'button:has-text("GitHub")',
                        'a:has-text("GitHub")',
                        '[data-provider="github"]'
                    ],
                    "GitHub 登录按钮"
                ):
                    self.log("找不到 GitHub 登录按钮", "ERROR")
                    self.notify(False, "找不到 GitHub 登录按钮")
                    return

                time.sleep(2)
                page.wait_for_load_state("networkidle", timeout=60000)
                self.shot(page, "after_click_github")

                # 如果已经登录（直接跳转）
                if (
                    "signin" not in page.url.lower()
                    and "claw.cloud" in page.url
                    and "github.com" not in page.url
                ):
                    self.log("已自动登录，无需 GitHub 认证", "SUCCESS")
                    self.detect_region(page.url)
                    self.keepalive(page)

                    new = self.get_session(context)
                    if new:
                        self.save_cookie(new)

                    self.notify(True)
                    return

                # Step 3: GitHub 登录
                self.log("步骤 3：GitHub 登录", "STEP")
                if "github.com" in page.url:
                    if not self.login_github(page, context):
                        self.shot(page, "github_login_failed")
                        self.notify(False, "GitHub 登录失败")
                        return

                # Step 4: 等待重定向
                self.log("步骤 4：等待重定向", "STEP")
                if not self.wait_redirect(page):
                    self.shot(page, "redirect_failed")
                    self.notify(False, "重定向失败")
                    return

                self.shot(page, "redirect_success")

                # Step 5: 保活
                self.log("步骤 5：保活", "STEP")
                self.keepalive(page)

                # Step 6: 保存 Cookie
                new = self.get_session(context)
                if new:
                    self.save_cookie(new)

                self.notify(True)
                self.log("登录流程完成", "SUCCESS")

            except Exception as e:
                self.log(f"运行异常: {e}", "ERROR")
                self.notify(False, str(e))

            finally:
                browser.close()


# ============================================================
# 程序入口
# ============================================================

if __name__ == "__main__":
    AutoLogin().run()

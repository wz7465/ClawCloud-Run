import fs from "fs";

const token = process.env.TG_BOT_TOKEN;
const chatId = process.env.TG_CHAT_ID;
const apiUrl = `https://api.telegram.org/bot${token}/sendMessage`;

// 读取 sites.json
let sites = JSON.parse(fs.readFileSync("sites.json", "utf8"));

// 获取今天零点（UTC）
function getTodayZeroUTC() {
  const now = new Date();
  const utc = Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate(),
    0, 0, 0
  );
  return Math.floor(utc / 1000);
}

const todayZero = getTodayZeroUTC();
const oneDay = 86400;

async function sendTelegramMessage(text) {
  const res = await fetch(apiUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text })
  });
  return res.ok;
}

(async () => {
  let updated = false;

  for (const site of sites) {
    const due = site.lastLogin + site.intervalDays * oneDay;

    if (now >= due) {
      console.log(`${site.name} 到达登录时间`);

      const ok = await sendTelegramMessage(`登录：${site.url}`);

      if (ok) {
        console.log(`${site.name} 登录任务已发送`);
        site.lastLogin = todayZero;   // 记录当天 UTC 零点
        updated = true;
      }
    } else {
      console.log(`${site.name} 未到时间`);
    }
  }

  if (updated) {
    fs.writeFileSync("sites.json", JSON.stringify(sites, null, 2));
  }

  process.exit(0);
})();
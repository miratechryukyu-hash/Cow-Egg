/**
 * LINE Webhook: 獣医師 ↔ 現場 のメッセージを相互転送
 *
 * セットアップ:
 * 1. スクリプトプロパティに CHANNEL_ACCESS_TOKEN と SPREADSHEET_ID を設定
 * 2. デプロイ → ウェブアプリ → アクセス: 全員
 * 3. LINE Developers の Webhook URL にデプロイURLを設定
 */

const SETTINGS_SHEET = "通知設定";

function doPost(e) {
  const body = JSON.parse(e.postData.contents);
  (body.events || []).forEach(handleEvent);
  return ContentService.createTextOutput("OK");
}

function handleEvent(event) {
  if (event.type !== "message" || !event.message || event.message.type !== "text") {
    return;
  }

  const fromUserId = event.source.userId;
  const text = event.message.text;
  const settings = loadSettings();
  const vetId = settings.LINE_User_ID || "";
  const farmId = settings["現場_LINE_User_ID"] || "";

  let targetId = "";
  let senderLabel = "";

  if (fromUserId === vetId && farmId) {
    targetId = farmId;
    senderLabel = "獣医師";
  } else if (fromUserId === farmId && vetId) {
    targetId = vetId;
    senderLabel = "現場";
  }

  if (targetId) {
    pushMessage(
      targetId,
      `【${senderLabel}からの連絡】\n${text}\n\n※返信はこのLINEにメッセージを送ってください。`
    );
    replyMessage(event.replyToken, "メッセージを相手に転送しました。");
    logMessage(senderLabel, text);
    return;
  }

  replyMessage(
    event.replyToken,
    "登録されていないアカウントです。管理者に User ID の登録を依頼してください。"
  );
}

function loadSettings() {
  const spreadsheetId = PropertiesService.getScriptProperties().getProperty("SPREADSHEET_ID");
  const sheet = SpreadsheetApp.openById(spreadsheetId).getSheetByName(SETTINGS_SHEET);
  const values = sheet.getDataRange().getValues();
  const settings = {};

  values.forEach((row) => {
    if (row[0]) {
      settings[String(row[0]).trim()] = String(row[1] || "").trim();
    }
  });

  return settings;
}

function logMessage(senderLabel, text) {
  const spreadsheetId = PropertiesService.getScriptProperties().getProperty("SPREADSHEET_ID");
  const spreadsheet = SpreadsheetApp.openById(spreadsheetId);
  let sheet = spreadsheet.getSheetByName("LINEやり取り");

  if (!sheet) {
    sheet = spreadsheet.insertSheet("LINEやり取り");
    sheet.appendRow(["日時", "送信者", "メッセージ"]);
  }

  const now = Utilities.formatDate(new Date(), "Asia/Tokyo", "yyyy-MM-dd HH:mm");
  sheet.appendRow([now, senderLabel, text]);
}

function pushMessage(userId, text) {
  const token = PropertiesService.getScriptProperties().getProperty("CHANNEL_ACCESS_TOKEN");
  UrlFetchApp.fetch("https://api.line.me/v2/bot/message/push", {
    method: "post",
    headers: {
      Authorization: "Bearer " + token,
      "Content-Type": "application/json",
    },
    payload: JSON.stringify({
      to: userId,
      messages: [{ type: "text", text: text }],
    }),
    muteHttpExceptions: true,
  });
}

function replyMessage(replyToken, text) {
  const token = PropertiesService.getScriptProperties().getProperty("CHANNEL_ACCESS_TOKEN");
  UrlFetchApp.fetch("https://api.line.me/v2/bot/message/reply", {
    method: "post",
    headers: {
      Authorization: "Bearer " + token,
      "Content-Type": "application/json",
    },
    payload: JSON.stringify({
      replyToken: replyToken,
      messages: [{ type: "text", text: text }],
    }),
    muteHttpExceptions: true,
  });
}

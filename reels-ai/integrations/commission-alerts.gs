/**
 * Ooz Studio Ink: commission request -> Telegram alert.
 *
 * Runs inside your commission Google Form, so alerts are instant and don't depend on the bot.
 * Setup (once):
 *   1. Open the commission form in Google Forms -> the 3-dot menu -> "Apps Script".
 *   2. Replace everything in the editor with this file and click Save.
 *   3. Project Settings (gear) -> Script Properties -> add:
 *        TELEGRAM_BOT_TOKEN = your bot token (the same one saved on GitHub)
 *        TELEGRAM_CHAT_ID   = the number the bot replies with when you send it /id
 *   4. Back in the editor, choose the function "setup" and click Run. Approve the permissions.
 *   5. Choose "sendTest" and Run. You should get a test message in Telegram.
 */

function onCommissionSubmit(e) {
  var lines = ['🖋️ New commission request'];
  e.response.getItemResponses().forEach(function (r) {
    var answer = r.getResponse();
    if (Array.isArray(answer)) answer = answer.join(', ');
    if (answer === '' || answer === null) return;
    lines.push('• ' + r.getItem().getTitle() + ': ' + answer);
  });
  var email = e.response.getRespondentEmail();
  if (email) lines.push('• Email: ' + email);
  lines.push('Received ' + Utilities.formatDate(e.response.getTimestamp(), Session.getScriptTimeZone(), 'EEE MMM d, h:mm a'));
  send_(lines.join('\n'));
}

function setup() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'onCommissionSubmit') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('onCommissionSubmit').forForm(FormApp.getActiveForm()).onFormSubmit().create();
}

function sendTest() {
  send_('✅ Commission alerts are connected. New form submissions will show up here.');
}

function send_(text) {
  var props = PropertiesService.getScriptProperties();
  var token = (props.getProperty('TELEGRAM_BOT_TOKEN') || '').replace(/\s+/g, '');
  var chat = (props.getProperty('TELEGRAM_CHAT_ID') || '').replace(/\s+/g, '');
  if (!token || !chat) throw new Error('Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Project Settings -> Script Properties');
  UrlFetchApp.fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({ chat_id: chat, text: text.slice(0, 4000), disable_web_page_preview: true })
  });
}

/**
 * Free Sheets bridge - no Google Cloud project, no billing.
 * Open the "SEO" spreadsheet -> Extensions -> Apps Script -> paste this -> set SECRET below ->
 * Deploy -> New deployment -> Web app -> Execute as: Me, Who has access: Anyone -> copy the /exec URL.
 */
const SECRET = 'CHANGE_ME_LONG_RANDOM_STRING';   // must equal APPS_SCRIPT_SECRET in .env

function doPost(e) {
  const lock = LockService.getScriptLock(); lock.waitLock(30000);
  try {
    const p = JSON.parse(e.postData.contents);
    if (p.secret !== SECRET) return out({ok: false, error: 'bad secret'});
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    if (p.action === 'ensure_tab') { tab(ss, p.tab, p.header); return out({ok: true}); }
    if (p.action === 'rename_tab') {
      const old = ss.getSheetByName(p.old);
      if (old && !ss.getSheetByName(p.tab)) old.setName(p.tab); else tab(ss, p.tab, p.header);
      return out({ok: true});
    }
    if (p.action === 'delete_tab') {
      const s = ss.getSheetByName(p.tab); if (s && ss.getSheets().length > 1) ss.deleteSheet(s);
      return out({ok: true});
    }
    if (p.action === 'upsert_rows') {
      const s = tab(ss, p.tab, p.header);
      const last = s.getLastRow();
      const ids = last > 0 ? s.getRange(1, 1, last, 1).getValues().map(r => String(r[0])) : [];
      p.rows.forEach(r => {
        const i = ids.indexOf(String(r[0]));
        if (i >= 0) s.getRange(i + 1, 1, 1, r.length).setValues([r]);
        else { s.appendRow(r); ids.push(String(r[0])); }
      });
      return out({ok: true, rows: p.rows.length});
    }
    return out({ok: false, error: 'unknown action'});
  } catch (err) { return out({ok: false, error: String(err)}); }
  finally { lock.releaseLock(); }
}
function tab(ss, name, header) {
  let s = ss.getSheetByName(name);
  if (!s) { s = ss.insertSheet(name); s.appendRow(header); s.setFrozenRows(1); }
  return s;
}
function out(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }

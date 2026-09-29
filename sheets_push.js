/* Pushes sheet_data.json into the existing Google Sheet, in place (same URL).
   Auth: the seo-digest service account, which must have Editor on the sheet.
   Usage: node sheets_push.js <sheetId> <path-to-sheet_data.json>

   FORMATTING CONTRACT (Abhishek formatted this sheet by hand - don't invent a look):
   before touching anything we SNAPSHOT the live formatting - the two Daily header rows,
   the first data row, the last date row, the Average row, the JIRA header + one JIRA data
   row, and every column width - then re-apply that snapshot to the new shape after writing.
   So inserting, updating or deleting rows never leaves an unformatted row behind, and any
   change he makes in the sheet is carried forward by the next push instead of reverted.
   The DEFAULT_* specs below are only a seed for a tab that doesn't exist yet.            */
const crypto = require('crypto');
// service-account key: GOOGLE_SA_JSON env var (GitHub Actions), else the key file on the laptop
const SA = process.env.GOOGLE_SA_JSON ? JSON.parse(process.env.GOOGLE_SA_JSON)
                                      : require('C:/Users/abhis/Downloads/claude-code-499105-947645f8bec8.json');
const [SHEET, DATA] = process.argv.slice(2);
const d = JSON.parse(require('fs').readFileSync(DATA, 'utf8'));
const NCOL = 4;                       // Failed | Total | Fail % | JIRA created
const WIDTH = 1 + NCOL * d.labels.length;
const JW = d.jira_header.length;
const b64 = o => Buffer.from(typeof o === 'string' ? o : JSON.stringify(o)).toString('base64url');

// --- seed look, used only when a tab has to be created from scratch ---------------------
const BLUE = { red: 0.13, green: 0.447, blue: 0.839 };   // #2172D6
const PINK = { red: 1, green: 0.6, blue: 1 };            // #FF99FF
const WHITE = { red: 1, green: 1, blue: 1 };
const SOLID = { style: 'SOLID', color: { red: 0, green: 0, blue: 0 } };
const PCT = { type: 'PERCENT', pattern: '0.0%' };
const DEFAULT_WIDTHS = [154, 60, 60, 60, 82, 60, 66, 60, 82, 60, 60, 44, 82];
const DEFAULT_JIRA_WIDTHS = [73, 79, 137, 69, 65, 708, 79, 98];
const brd = (t, b, l, r) => {
  const o = {};
  if (t) o.top = SOLID; if (b) o.bottom = SOLID; if (l) o.left = SOLID; if (r) o.right = SOLID;
  return Object.keys(o).length ? o : undefined;
};
const headFmt = bg => ({ backgroundColor: bg, horizontalAlignment: 'CENTER', verticalAlignment: 'MIDDLE',
  borders: brd(1, 1, 1, 1), textFormat: { bold: true, foregroundColor: WHITE } });
// project blocks alternate pink / blue, the way he set them
const bandOf = i => (i % 2 === 0 ? PINK : BLUE);
const defaultHeader = () => {
  const r1 = [headFmt(BLUE)], r2 = [headFmt(BLUE)];
  d.labels.forEach((_, i) => { for (let k = 0; k < NCOL; k++) { r1.push(headFmt(bandOf(i))); r2.push(headFmt(bandOf(i))); } });
  return [r1, r2];
};
const defaultDataRow = ({ top = false, bottom = false, bold = false } = {}) => {
  const row = [];
  for (let c = 0; c < WIDTH; c++) {
    const isFirstOfBlock = c > 0 && (c - 1) % NCOL === 0;
    const f = { horizontalAlignment: bold && c === 0 ? 'LEFT' : 'CENTER',
                borders: bold ? undefined : brd(top || c === 0, bottom || c === 0, c === 0 || isFirstOfBlock, c === WIDTH - 1) };
    if (c > 0 && (c - 1) % NCOL === 2) f.numberFormat = PCT;
    if (bold) f.textFormat = { bold: true };
    row.push(f);
  }
  return row;
};
const defaultJiraHeader = () => Array.from({ length: JW }, () => headFmt(BLUE));
const defaultJiraRow = () => Array.from({ length: JW }, (_, c) =>
  c === 1 ? { horizontalAlignment: 'RIGHT', numberFormat: { type: 'DATE', pattern: 'yyyy-mm-dd' } }
          : { horizontalAlignment: 'LEFT' });

// --- api plumbing -----------------------------------------------------------------------
async function token() {
  const now = Math.floor(Date.now() / 1000);
  const body = b64({ alg: 'RS256', typ: 'JWT' }) + '.' + b64({
    iss: SA.client_email, scope: 'https://www.googleapis.com/auth/spreadsheets',
    aud: 'https://oauth2.googleapis.com/token', iat: now, exp: now + 3600 });
  const sig = crypto.createSign('RSA-SHA256').update(body).sign(SA.private_key, 'base64url');
  const r = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer', assertion: body + '.' + sig }) });
  const j = await r.json();
  if (!j.access_token) throw new Error('auth failed: ' + JSON.stringify(j).slice(0, 200));
  return j.access_token;
}

const api = (tok, path, method, body) =>
  fetch(`https://sheets.googleapis.com/v4/spreadsheets/${SHEET}${path}`, {
    method, headers: { Authorization: 'Bearer ' + tok, 'Content-Type': 'application/json' },
    body: body && JSON.stringify(body),
  }).then(async r => {
    const t = await r.text();
    if (!r.ok) throw new Error(`${r.status} ${path}: ${t.slice(0, 300)}`);
    return t ? JSON.parse(t) : {};
  });

const A1 = n => { let s = ''; for (n = n - 1; n >= 0; n = Math.floor(n / 26) - 1) s = String.fromCharCode(65 + n % 26) + s; return s; };
const cells = (sheetId, row, formats) => ({ updateCells: {
  range: { sheetId, startRowIndex: row, endRowIndex: row + 1, startColumnIndex: 0, endColumnIndex: formats.length },
  rows: [{ values: formats.map(f => ({ userEnteredFormat: f || {} })) }], fields: 'userEnteredFormat' } });
// one repeatCell per column, because borders and number formats differ column by column
const band = (sheetId, from, to, formats) => formats.map((f, c) => ({ repeatCell: {
  range: { sheetId, startRowIndex: from, endRowIndex: to, startColumnIndex: c, endColumnIndex: c + 1 },
  cell: { userEnteredFormat: f || {} }, fields: 'userEnteredFormat' } })).filter(() => to > from);
const widths = (sheetId, px) => px.map((p, i) => ({ updateDimensionProperties: {
  range: { sheetId, dimension: 'COLUMNS', startIndex: i, endIndex: i + 1 }, properties: { pixelSize: p }, fields: 'pixelSize' } }));

(async () => {
  const tok = await token();
  const meta = await api(tok, '?fields=sheets.properties', 'GET');
  const ids = Object.fromEntries(meta.sheets.map(s => [s.properties.title, s.properties.sheetId]));

  const want = ['Daily', 'JIRA tickets'];
  const missing = want.filter(t => !(t in ids));
  const fresh = new Set(missing);
  if (missing.length) {
    const res = await api(tok, ':batchUpdate', 'POST', { requests: missing.map(title => ({ addSheet: { properties: { title } } })) });
    res.replies.forEach(r => { ids[r.addSheet.properties.title] = r.addSheet.properties.sheetId; });
  }
  const daily = ids['Daily'], jira = ids['JIRA tickets'];

  // ---- 1. snapshot his formatting BEFORE we touch anything ------------------------------
  let colA = [];
  if (!fresh.has('Daily')) colA = (await api(tok, '/values/Daily!A1:A500', 'GET')).values || [];
  const oldAvg = colA.findIndex(r => (r[0] || '').startsWith('Average')) + 1;   // 1-based, 0 if absent
  const oldLast = oldAvg > 3 ? oldAvg - 1 : 0;                                  // last date row

  const ranges = [];
  if (!fresh.has('Daily')) {
    ranges.push(`Daily!A1:${A1(WIDTH)}4`);   // rows 1-2 header, 3 = first data row, 4 = a middle row
    if (oldAvg) ranges.push(`Daily!A${oldLast}:${A1(WIDTH)}${oldAvg}`);
  }
  if (!fresh.has('JIRA tickets')) ranges.push(`'JIRA tickets'!A1:${A1(JW)}2`);
  let snap = { sheets: [] };
  if (ranges.length) {
    const q = ranges.map(r => 'ranges=' + encodeURIComponent(r)).join('&');
    snap = await api(tok, `?includeGridData=true&${q}&fields=` + encodeURIComponent(
      'sheets(properties.title,properties.sheetId,data(startRow,rowData(values(userEnteredFormat,effectiveFormat)),columnMetadata(pixelSize)))'), 'GET');
  }
  const blocks = t => (snap.sheets || []).filter(s => s.properties.title === t).flatMap(s => s.data || []);
  // a snapshot row is only usable if it spans the full current width (column layout may have changed)
  const rowAt = (blks, absRow, width) => {
    for (const b of blks) {
      const i = absRow - 1 - (b.startRow || 0);
      const v = ((b.rowData || [])[i] || {}).values;
      // userEnteredFormat is what he actually set; effectiveFormat covers a cell that only
      // inherits its look, so we never clear a cell just because it had no explicit format.
      if (v && v.length >= width) return v.slice(0, width).map(c => c.userEnteredFormat || c.effectiveFormat || {});
    }
    return null;
  };
  const colPx = (blks, n, dflt) => {
    for (const b of blks) {
      const m = b.columnMetadata || [];
      if (m.length >= n && m.every(c => c.pixelSize)) return m.slice(0, n).map(c => c.pixelSize);
    }
    return dflt;
  };
  const db = blocks('Daily'), jb = blocks('JIRA tickets');
  const dflt = defaultHeader();
  const fHead1 = rowAt(db, 1, WIDTH) || dflt[0];
  const fHead2 = rowAt(db, 2, WIDTH) || dflt[1];
  // Row 3 carries the top edge of the box (it sits under the header) and the last date row
  // carries the bottom edge, so neither can stand in for the middle rows - row 4 is the
  // only honest template for those. Copying row 3 downwards rules the whole table.
  const fFirst = rowAt(db, 3, WIDTH) || defaultDataRow({ top: true });
  const fMid   = rowAt(db, 4, WIDTH) || defaultDataRow();
  const fLast  = (oldLast && rowAt(db, oldLast, WIDTH)) || defaultDataRow({ bottom: true });
  const fAvg   = (oldAvg && rowAt(db, oldAvg, WIDTH)) || defaultDataRow({ bold: true });
  const fJHead = rowAt(jb, 1, JW) || defaultJiraHeader();
  const fJData = rowAt(jb, 2, JW) || defaultJiraRow();
  const pxD = colPx(db, WIDTH, DEFAULT_WIDTHS.slice(0, WIDTH));
  const pxJ = colPx(jb, JW, DEFAULT_JIRA_WIDTHS.slice(0, JW));

  // ---- 2. clear stale merges, then write values -----------------------------------------
  await api(tok, ':batchUpdate', 'POST', { requests: [
    { unmergeCells: { range: { sheetId: daily, startRowIndex: 0, endRowIndex: 2, startColumnIndex: 0, endColumnIndex: WIDTH } } },
  ] });

  const top = ['Date'], sub = [''];
  d.labels.forEach(l => { top.push(l, '', '', ''); sub.push('Failed', 'Total', 'Fail %', 'JIRA created'); });
  await api(tok, '/values/Daily!A1:ZZ2000:clear', 'POST', {});
  await api(tok, `/values/Daily!A1?valueInputOption=RAW`, 'PUT', { values: [top, sub, ...d.daily] });
  await api(tok, '/values/' + encodeURIComponent("'JIRA tickets'!A1:ZZ2000") + ':clear', 'POST', {});
  // USER_ENTERED so the =HYPERLINK() cells become real links (RAW would store them as text)
  await api(tok, `/values/${encodeURIComponent("'JIRA tickets'!A1")}?valueInputOption=USER_ENTERED`, 'PUT',
            { values: [d.jira_header, ...d.jira] });

  // ---- 3. re-apply the snapshot to the NEW shape ----------------------------------------
  const nRows = d.daily.length, lastRow = 2 + nRows;      // 1-based row of the Average line
  const hasAvg = (d.daily[nRows - 1] || [])[0] === undefined ? false : String(d.daily[nRows - 1][0]).startsWith('Average');
  const lastDate = hasAvg ? lastRow - 1 : lastRow;        // 1-based row of the final date line

  const reqs = [
    { mergeCells: { mergeType: 'MERGE_COLUMNS', range: { sheetId: daily, startRowIndex: 0, endRowIndex: 2, startColumnIndex: 0, endColumnIndex: 1 } } },
    ...d.labels.map((_, i) => ({ mergeCells: { mergeType: 'MERGE_ROWS',
      range: { sheetId: daily, startRowIndex: 0, endRowIndex: 1, startColumnIndex: 1 + i * NCOL, endColumnIndex: 1 + i * NCOL + NCOL } } })),
    cells(daily, 0, fHead1),
    cells(daily, 1, fHead2),
    cells(daily, 2, fFirst),                              // first date row: top edge of the box
    ...band(daily, 3, lastDate - 1, fMid),                // the middle date rows
    cells(daily, lastDate - 1, fLast),                    // last date row: bottom edge
    ...(hasAvg ? [cells(daily, lastRow - 1, fAvg)] : []),
    // wipe formatting below the data, so a shorter range leaves no orphan styling behind
    { repeatCell: { range: { sheetId: daily, startRowIndex: lastRow, endRowIndex: 2000, startColumnIndex: 0, endColumnIndex: WIDTH },
                    cell: {}, fields: 'userEnteredFormat' } },
    { updateSheetProperties: { properties: { sheetId: daily, gridProperties: { frozenRowCount: 2, frozenColumnCount: 1 } }, fields: 'gridProperties(frozenRowCount,frozenColumnCount)' } },
    ...widths(daily, pxD),

    cells(jira, 0, fJHead),
    ...band(jira, 1, 1 + d.jira.length, fJData),
    { repeatCell: { range: { sheetId: jira, startRowIndex: 1 + d.jira.length, endRowIndex: 2000, startColumnIndex: 0, endColumnIndex: JW },
                    cell: {}, fields: 'userEnteredFormat' } },
    { updateSheetProperties: { properties: { sheetId: jira, gridProperties: { frozenRowCount: 1 } }, fields: 'gridProperties.frozenRowCount' } },
    ...widths(jira, pxJ),
  ];
  await api(tok, ':batchUpdate', 'POST', { requests: reqs });
  const src = fresh.size ? 'seed defaults' : 'snapshot of existing formatting';
  console.log(`pushed: Daily ${nRows} rows x ${WIDTH} cols, JIRA tickets ${d.jira.length} rows (format: ${src})`);
  if (process.argv.includes('--verify')) {   // read back: every date label and the JIRA row count must match
    const v = await api(tok, `/values:batchGet?ranges=Daily!A1:A2000&ranges=${encodeURIComponent("'JIRA tickets'!A1:A2000")}`, 'GET');
    const got = (v.valueRanges[0].values || []).slice(2).map(r => r[0]);
    const want = d.daily.map(r => String(r[0]));
    const jrows = (v.valueRanges[1].values || []).length - 1;
    if (JSON.stringify(got) !== JSON.stringify(want) || jrows !== d.jira.length)
      throw new Error(`verify failed: Daily ${got.length}/${want.length} rows (top "${got[0]}"), JIRA ${jrows}/${d.jira.length}`);
    console.log(`verified: top row "${got[0]}", ${want.length} Daily rows, ${jrows} JIRA rows`);
  }
})().catch(e => { console.error('FAILED:', e.message); process.exit(1); });

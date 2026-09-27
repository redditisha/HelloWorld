// Minimal Google Sheets v4 REST client (service-account auth).
import { GoogleAuth } from "google-auth-library";

const API = "https://sheets.googleapis.com/v4/spreadsheets";

export async function openSpreadsheet(spreadsheetId, credentialsJson) {
  const auth = new GoogleAuth({
    credentials: JSON.parse(credentialsJson),
    scopes: ["https://www.googleapis.com/auth/spreadsheets"],
  });
  const client = await auth.getClient();

  async function call(method, path, body, attempt = 0) {
    const { token } = await client.getAccessToken();
    const res = await fetch(`${API}/${spreadsheetId}${path}`, {
      method,
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    // Back off on quota (429) and transient server errors.
    if ((res.status === 429 || res.status >= 500) && attempt < 5) {
      await new Promise((r) => setTimeout(r, 2000 * 2 ** attempt));
      return call(method, path, body, attempt + 1);
    }
    if (!res.ok) throw new Error(`Sheets API ${res.status}: ${(await res.text()).slice(0, 300)}`);
    return res.json();
  }

  const q = (s) => `'${s.replace(/'/g, "''")}'`;

  return {
    /** Tab title -> { sheetId, rowCount } */
    async tabs() {
      const d = await call("GET", "?fields=sheets.properties(sheetId,title,gridProperties.rowCount)");
      return new Map(
        (d.sheets || []).map((s) => [
          s.properties.title,
          { sheetId: s.properties.sheetId, rowCount: s.properties.gridProperties?.rowCount ?? 0 },
        ])
      );
    },

    /** Read several A1 ranges in one request -> array of row arrays. */
    async read(ranges) {
      if (!ranges.length) return [];
      const params = ranges.map((r) => `ranges=${encodeURIComponent(r)}`).join("&");
      const d = await call("GET", `/values:batchGet?${params}&majorDimension=ROWS`);
      return (d.valueRanges || []).map((v) => v.values || []);
    },

    /** Structural changes (add/delete/resize tabs) in one request. */
    async batchUpdate(requests) {
      if (!requests.length) return null;
      return call("POST", ":batchUpdate", { requests });
    },

    /** Write values; RAW so a headline starting with "=" is never a formula. */
    async write(data) {
      if (!data.length) return null;
      return call("POST", "/values:batchUpdate", { valueInputOption: "RAW", data });
    },

    /**
     * Append rows after a tab's last row. Google applies appends atomically and
     * INSERT_ROWS grows the grid by exactly rows.length, so two collectors
     * running at once (GitHub + a manual run) can't overwrite each other.
     */
    async append(tab, lastCol, rows) {
      if (!rows.length) return null;
      const range = encodeURIComponent(`${q(tab)}!A1:${lastCol}`);
      return call("POST", `/values/${range}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS`, {
        majorDimension: "ROWS",
        values: rows,
      });
    },

    range: (tab, a1) => `${q(tab)}!${a1}`,
  };
}

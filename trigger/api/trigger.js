// Vercel cron -> starts the "BugBug sheet daily" GitHub workflow.
// GitHub's own schedule can be delayed or dropped under load, so this is the main trigger and
// GitHub's schedule is the backup. Running the workflow twice is harmless (it just refreshes).
const REPO = 'abhishekagarwal360/bugbug-report';
const WORKFLOW = 'nightly.yml';
// seconds before each attempt: 6 tries over ~5 min. On 30 Sep GitHub's API returned 500s for ~2 min
// while its status page showed green, so a short retry window is not enough.
const WAITS = [0, 15, 30, 60, 90, 90];

const sleep = s => new Promise(r => setTimeout(r, s * 1000));
const gh = (path, init = {}) => fetch('https://api.github.com' + path, {
  ...init,
  headers: {
    Authorization: `Bearer ${process.env.GH_TOKEN}`,
    Accept: 'application/vnd.github+json',
    'Content-Type': 'application/json',
    'User-Agent': 'bugbug-trigger',
  },
});

module.exports = async (req, res) => {
  // Vercel sends "Authorization: Bearer <CRON_SECRET>" on cron calls; anyone else is turned away
  if (req.headers.authorization !== `Bearer ${process.env.CRON_SECRET}`)
    return res.status(401).json({ error: 'unauthorized' });
  const slot = String(req.query.slot || 'manual').replace(/[^a-z0-9-]/gi, '');

  let expires = '', last = '';
  for (const wait of WAITS) {
    await sleep(wait);
    try {
      // the token's expiry date rides along, so the workflow can warn 14 days before it lapses
      const probe = await gh(`/repos/${REPO}`);
      expires = probe.headers.get('github-authentication-token-expiration') || '';
      const r = await gh(`/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
        method: 'POST',
        body: JSON.stringify({ ref: 'main', inputs: { source: `vercel-cron-${slot}`, token_expires: expires } }),
      });
      if (r.status === 204) {
        console.log(`dispatched (${slot}); token expires: ${expires || 'never'}`);
        return res.status(200).json({ ok: true, slot, token_expires: expires || null });
      }
      last = `${r.status} ${(await r.text()).slice(0, 200)}`;
    } catch (e) {
      last = String(e);
    }
    console.log(`attempt failed: ${last}`);
  }
  console.error(`gave up after ${WAITS.length} attempts: ${last}`);
  return res.status(502).json({ ok: false, slot, error: last });
};
